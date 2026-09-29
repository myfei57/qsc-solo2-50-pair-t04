"""分段升温压力曲线与段序执行。

旧工艺只有一个目标压力，母管一路拉到顶，薄件厚件共用同一套，每段停多久
全靠操作工目测。分段曲线按制品型号给出逐段目标压力与每段到点后的保温
时长，规则有三条：

1. 压力进入"到点窗口"（目标 ± 容差）才算到点，到点前一直在升温段；
2. 到点后必须在窗口内停满该段时长，才允许进入下一段；
3. 保温途中冲过窗口上限，就退回升温段重新到位、重新计时——已经停了
   但没停满的时间作废，不允许带着超压把保温时间"凑"出来。

曲线与硫化时长曲线一样按制品登记、按代际取最新：配方改版只登记新一代，
投产永远拿最新段表，不会误用旧压力段。
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field
from typing import Sequence as TypingSequence

from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..mold.generation import GenerationCounter


@dataclass(frozen=True)
class PressureStage:
    """一个压力段：目标压力与到点后的保温时长。"""

    target_kpa: float
    hold_s: float

    def __post_init__(self) -> None:
        if self.target_kpa <= 0:
            raise ValueError("压力段目标必须为正")
        if self.hold_s <= 0:
            raise ValueError("压力段保温时长必须为正")

    def as_dict(self) -> dict:
        return {"target_kpa": round(self.target_kpa, 3), "hold_s": round(self.hold_s, 3)}


@dataclass(frozen=True)
class PressureRamp:
    """一份制品的分段压力曲线。"""

    product_code: str
    generation: int
    stages: tuple[PressureStage, ...]
    registered_at: float

    def __post_init__(self) -> None:
        if not self.product_code:
            raise ValueError("制品型号不能为空")
        if not self.stages:
            raise ValueError(f"制品 {self.product_code} 的压力曲线至少要有一段")
        targets = [stage.target_kpa for stage in self.stages]
        if any(later <= earlier for earlier, later in zip(targets, targets[1:])):
            raise ValueError(
                f"制品 {self.product_code} 的压力段目标必须逐段升高，不能回退或持平"
            )

    @property
    def final_target_kpa(self) -> float:
        return self.stages[-1].target_kpa

    def as_dict(self) -> dict:
        return {
            "product_code": self.product_code,
            "generation": self.generation,
            "registered_at": round(self.registered_at, 3),
            "stages": [stage.as_dict() for stage in self.stages],
        }


class RampBook:
    """按制品型号保存全代际分段压力曲线，读取取最新代际。"""

    def __init__(self, counter: GenerationCounter | None = None) -> None:
        self._counter = counter or GenerationCounter()
        self._by_product: dict[str, list[PressureRamp]] = {}
        self._lock = threading.RLock()

    def register(
        self,
        product_code: str,
        stages: TypingSequence[tuple[float, float]],
        registered_at: float,
    ) -> PressureRamp:
        """登记一份新代际分段压力曲线，段表为 (目标压力, 保温时长) 序列。"""

        with self._lock:
            generation = self._counter.next()
            ramp = PressureRamp(
                product_code=product_code,
                generation=generation,
                stages=tuple(
                    PressureStage(target_kpa=float(target), hold_s=float(hold))
                    for target, hold in stages
                ),
                registered_at=float(registered_at),
            )
            self._by_product.setdefault(product_code, []).append(ramp)
        return ramp

    def get(self, product_code: str) -> PressureRamp | None:
        """返回该制品代际最新的压力曲线；未登记返回 None。"""

        with self._lock:
            history = list(self._by_product.get(product_code, ()))
        if not history:
            return None
        return max(history, key=lambda item: item.generation)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                product_code: [ramp.as_dict() for ramp in history]
                for product_code, history in sorted(self._by_product.items())
            }


class RampPhase(str, enum.Enum):
    """分段曲线段序相位。"""

    IDLE = "idle"
    HEATING = "heating"
    HOLDING = "holding"
    COMPLETE = "complete"


@dataclass
class RampRunner:
    """一段一模的分段压力段序执行器。

    每个控制周期喂入当前母管压力，执行器给出本段目标压力并推进段序；
    蒸汽回路把该目标作为高优先级写入方提交给目标值仲裁器，阀门闭环
    照旧，执行器不直接碰阀。
    """

    tolerance_kpa: float
    bus: EventBus | None = None
    _ramp: PressureRamp | None = None
    _mold_id: str = ""
    _stage_index: int = 0
    _phase: RampPhase = RampPhase.IDLE
    _held_s: float = 0.0
    _overshoot_latched: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if self.tolerance_kpa <= 0:
            raise ValueError("到点窗口容差必须为正")

    def start(self, ramp: PressureRamp, mold_id: str = "") -> dict:
        """挂上一份曲线并从第一段升温开始；已在执行中拒绝重复挂。"""

        with self._lock:
            if self.is_driving():
                raise InterlockError(
                    f"制品 {ramp.product_code} 的分段压力曲线正在执行，不能重复挂",
                    component="steam",
                )
            self._ramp = ramp
            self._mold_id = mold_id
            self._stage_index = 0
            self._phase = RampPhase.HEATING
            self._held_s = 0.0
            self._overshoot_latched = False
            result = self._live_view(
                pressure_kpa=self._stage.target_kpa, held_s=0.0, receded=False,
                advanced=False, completed=False,
            )
        if self.bus is not None:
            self.bus.publish(
                Event.of(
                    "steam",
                    "steam",
                    "pressure_ramp_started",
                    subject=mold_id or ramp.product_code,
                    mold_id=mold_id,
                    product_code=ramp.product_code,
                    generation=ramp.generation,
                    stage_count=len(ramp.stages),
                    final_target_kpa=round(ramp.final_target_kpa, 3),
                )
            )
        return result

    def drop(self) -> None:
        """停汽后放弃当前段序（下一模重新挂）。"""

        with self._lock:
            self._ramp = None
            self._mold_id = ""
            self._stage_index = 0
            self._phase = RampPhase.IDLE
            self._held_s = 0.0
            self._overshoot_latched = False

    def is_driving(self) -> bool:
        """段序是否仍在驱动压力目标（未挂或已走完返回 False）。"""

        return self._ramp is not None and self._phase in (RampPhase.HEATING, RampPhase.HOLDING)

    @property
    def phase(self) -> RampPhase:
        return self._phase

    @property
    def _stage(self) -> PressureStage:
        assert self._ramp is not None
        return self._ramp.stages[self._stage_index]

    def current_target_kpa(self) -> float:
        """当前段目标压力；段序不驱动时抛错（调用方先判 is_driving）。"""

        if not self.is_driving():
            raise ValueError("分段压力曲线未在执行")
        return self._stage.target_kpa

    def step(self, pressure_kpa: float, dt: float) -> dict:
        """按当前母管压力推进一个周期，返回本段段序视图。"""

        if dt <= 0:
            raise ValueError("控制周期必须为正")
        with self._lock:
            if self._ramp is None or self._phase is RampPhase.COMPLETE:
                return {"active": False, "phase": self._phase.value}

            stage = self._stage
            target = stage.target_kpa
            low = target - self.tolerance_kpa
            high = target + self.tolerance_kpa
            receded = False
            advanced = False
            completed = False

            if pressure_kpa > high:
                if not self._overshoot_latched:
                    self._emit_overshoot(stage, pressure_kpa, high)
                    self._overshoot_latched = True
                receded = self._enter_heating()
            elif pressure_kpa < low:
                receded = self._enter_heating()
            elif self._phase is RampPhase.HEATING:
                self._phase = RampPhase.HOLDING
                self._held_s = 0.0
                self._emit(
                    "pressure_stage_reached",
                    stage_index=self._stage_index,
                    target_kpa=round(target, 3),
                    pressure_kpa=round(pressure_kpa, 3),
                )

            if self._phase is RampPhase.HOLDING:
                self._held_s += dt
                if low <= pressure_kpa <= high and self._held_s >= stage.hold_s:
                    if self._stage_index == len(self._ramp.stages) - 1:
                        self._phase = RampPhase.COMPLETE
                        completed = True
                        self._emit(
                            "pressure_ramp_completed",
                            product_code=self._ramp.product_code,
                            generation=self._ramp.generation,
                            final_target_kpa=round(target, 3),
                        )
                    else:
                        previous = self._stage_index
                        self._stage_index += 1
                        self._phase = RampPhase.HEATING
                        self._held_s = 0.0
                        self._overshoot_latched = False
                        advanced = True
                        self._emit(
                            "pressure_stage_advanced",
                            from_stage=previous,
                            to_stage=self._stage_index,
                            target_kpa=round(self._stage.target_kpa, 3),
                        )

            view = self._live_view(
                pressure_kpa=pressure_kpa,
                held_s=self._held_s,
                receded=receded,
                advanced=advanced,
                completed=completed,
            )
            if completed:
                view["active"] = False
            return view

    def _enter_heating(self) -> bool:
        """退回升温段；仅当原本在保温时表示"保温被冲断"。"""

        receded = self._phase is RampPhase.HOLDING
        if receded:
            self._phase = RampPhase.HEATING
            self._held_s = 0.0
        return receded

    def _emit_overshoot(self, stage: PressureStage, pressure_kpa: float, high: float) -> None:
        self._emit(
            "pressure_stage_overshot",
            stage_index=self._stage_index,
            target_kpa=round(stage.target_kpa, 3),
            pressure_kpa=round(pressure_kpa, 3),
            band_high_kpa=round(high, 3),
        )

    def _emit(self, action: str, **payload: object) -> None:
        if self.bus is None:
            return
        self.bus.publish(
            Event.of(
                "steam",
                "steam",
                action,
                subject=self._mold_id or (self._ramp.product_code if self._ramp else "ramp"),
                mold_id=self._mold_id,
                **payload,
            )
        )

    def _live_view(
        self,
        *,
        pressure_kpa: float,
        held_s: float,
        receded: bool,
        advanced: bool,
        completed: bool,
    ) -> dict:
        if self._ramp is None:
            return {"active": False, "phase": self._phase.value}
        stage = self._stage
        return {
            "active": True,
            "phase": self._phase.value,
            "product_code": self._ramp.product_code,
            "generation": self._ramp.generation,
            "mold_id": self._mold_id,
            "stage_index": self._stage_index,
            "stage_count": len(self._ramp.stages),
            "target_kpa": round(stage.target_kpa, 3),
            "hold_s": round(stage.hold_s, 3),
            "held_s": round(held_s, 3),
            "pressure_kpa": round(pressure_kpa, 3),
            "band_low_kpa": round(stage.target_kpa - self.tolerance_kpa, 3),
            "band_high_kpa": round(stage.target_kpa + self.tolerance_kpa, 3),
            "receded": receded,
            "advanced": advanced,
            "completed": completed,
        }

    def snapshot(self) -> dict:
        """完整段序快照，供状态页与落盘恢复共用。"""

        with self._lock:
            if self._ramp is None:
                return {"active": False, "phase": self._phase.value, "mold_id": ""}
            return {
                "active": self.is_driving(),
                "phase": self._phase.value,
                "product_code": self._ramp.product_code,
                "generation": self._ramp.generation,
                "mold_id": self._mold_id,
                "stage_index": self._stage_index,
                "stage_count": len(self._ramp.stages),
                "held_s": round(self._held_s, 3),
                "overshoot_latched": self._overshoot_latched,
                "final_target_kpa": round(self._ramp.final_target_kpa, 3),
                "ramp": self._ramp.as_dict(),
            }

    def restore(self, snapshot: dict) -> None:
        """从落盘快照恢复段序（相位、当前段、已保温时长）。"""

        ramp_snapshot = snapshot.get("ramp") if isinstance(snapshot, dict) else None
        if not isinstance(ramp_snapshot, dict):
            self.drop()
            return
        stages = tuple(
            PressureStage(target_kpa=float(item["target_kpa"]), hold_s=float(item["hold_s"]))
            for item in ramp_snapshot.get("stages", ())
        )
        ramp = PressureRamp(
            product_code=str(ramp_snapshot["product_code"]),
            generation=int(ramp_snapshot["generation"]),
            stages=stages,
            registered_at=float(ramp_snapshot.get("registered_at", 0.0)),
        )
        with self._lock:
            self._ramp = ramp
            self._mold_id = str(snapshot.get("mold_id", ""))
            self._stage_index = int(snapshot.get("stage_index", 0))
            self._held_s = float(snapshot.get("held_s", 0.0))
            self._overshoot_latched = bool(snapshot.get("overshoot_latched", False))
            try:
                self._phase = RampPhase(snapshot.get("phase", RampPhase.HEATING.value))
            except ValueError:
                self._phase = RampPhase.HEATING
