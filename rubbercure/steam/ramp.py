"""分段升温执行器。

把按制品登记的 :class:`~rubbercure.steam.profile.RampProfile` 跑成一条受控
升压曲线。执行器是一个小状态机：

RAMPING（向本段目标升压）
    压力进入本段容差带 → HOLDING，并记录到点时刻；
    压力直接冲过本段上沿 → RETREATING（阀门惯性/母管滞后完全可能冲过头）。
HOLDING（到点保温）
    必须在容差带内连续停够本段 ``hold_s``，才允许进下一段；
    中途冲过上沿 → RETREATING，到点计时作废，退回后重新计时；
    中途跌出下沿 → 回 RAMPING，同样重新计时。
RETREATING（冲过头退回）
    把目标压到本段下沿以下，等实测压力退回到设定值，再回 RAMPING 重新逼近。
COMPLETE（全部段走完，最后一段交由硫化计时保温）。

执行器只产出"这一周期该提交的压力目标"和状态，真正的阀位与母管充压由
:class:`~rubbercure.steam.steam.SteamUnit` 完成；这样同一段逻辑既能挂在真实
控制周期上，也能在验证环境里用可推进时钟直接驱完整条曲线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..clock import Clock
from ..eventbus import Event, EventBus
from .profile import RampProfile


class RampPhase(str, Enum):
    """分段升温执行相位。"""

    RAMPING = "ramping"
    HOLDING = "holding"
    RETREATING = "retreating"
    COMPLETE = "complete"


@dataclass
class RampRunner:
    """按分段配方推进压力曲线的状态机。"""

    profile: RampProfile
    clock: Clock
    bus: EventBus
    floor_kpa: float = 0.0
    writer: str = "ramp_program"

    stage_index: int = field(default=0, init=False)
    phase: RampPhase = field(default=RampPhase.RAMPING, init=False)
    band_since: float | None = field(default=None, init=False)
    retreats: int = field(default=0, init=False)
    started_at: float | None = field(default=None, init=False)
    last_command_kpa: float = field(default=0.0, init=False)
    last_measured_kpa: float | None = field(default=None, init=False)

    def start(self) -> None:
        """从第一段开始执行（重复调用等于回到起点重新跑）。"""

        self.stage_index = 0
        self.phase = RampPhase.RAMPING
        self.band_since = None
        self.retreats = 0
        self.started_at = self.clock.now()
        self.last_command_kpa = self.profile.stage_at(0).target_kpa
        self.last_measured_kpa = None

    def is_running(self) -> bool:
        return self.started_at is not None and self.phase is not RampPhase.COMPLETE

    def is_done(self) -> bool:
        return self.started_at is not None and self.phase is RampPhase.COMPLETE

    def _enter(self, phase: RampPhase, now: float) -> None:
        self.phase = phase
        self.band_since = now if phase is RampPhase.HOLDING else None

    def _overshot(self, stage, now: float) -> None:
        self.retreats += 1
        self._enter(RampPhase.RETREATING, now)
        self.bus.publish(
            Event.of(
                "steam",
                "steam",
                "ramp_overshot",
                subject=self.writer,
                product_code=self.profile.product_code,
                stage_index=self.stage_index,
                target_kpa=round(stage.target_kpa, 3),
                measured_kpa=round(self.last_measured_kpa or 0.0, 3),
            )
        )

    def update(self, measured_kpa: float, now: float | None = None) -> dict:
        """推进一个周期，返回本周期状态与该提交的压力目标。"""

        if self.started_at is None:
            raise RuntimeError("分段升温尚未 start，不能推进")
        moment = self.clock.now() if now is None else float(now)
        self.last_measured_kpa = float(measured_kpa)
        stage = self.profile.stage_at(self.stage_index)
        low, high = stage.band()

        if self.phase is RampPhase.RAMPING:
            if stage.is_above(measured_kpa):
                self._overshot(stage, moment)
            elif stage.is_in_band(measured_kpa):
                self._reach_stage(stage, measured_kpa, moment)
                # 末段 hold_s 可以为 0：到点即完成，交由硫化计时接管保温。
                if self.phase is RampPhase.HOLDING:
                    self._evaluate_holding(stage, moment)

        elif self.phase is RampPhase.HOLDING:
            if stage.is_above(measured_kpa):
                self._overshot(stage, moment)
            elif not stage.is_in_band(measured_kpa):
                # 保温期间跌出下沿，重新升压，到点计时作废重来。
                self._enter(RampPhase.RAMPING, moment)
            else:
                self._evaluate_holding(stage, moment)

        elif self.phase is RampPhase.RETREATING:
            if measured_kpa <= stage.target_kpa:
                self._enter(RampPhase.RAMPING, moment)
                self.bus.publish(
                    Event.of(
                        "steam",
                        "steam",
                        "ramp_recovered",
                        subject=self.writer,
                        product_code=self.profile.product_code,
                        stage_index=self.stage_index,
                        measured_kpa=round(measured_kpa, 3),
                    )
                )

        self.last_command_kpa = self._command(stage)
        return self._summary(stage, moment)

    def _reach_stage(self, stage, measured_kpa: float, now: float) -> None:
        """压力进入本段容差带：记录到点并进入保温。"""

        self._enter(RampPhase.HOLDING, now)
        self.bus.publish(
            Event.of(
                "steam",
                "steam",
                "ramp_stage_reached",
                subject=self.writer,
                product_code=self.profile.product_code,
                stage_index=self.stage_index,
                target_kpa=round(stage.target_kpa, 3),
                measured_kpa=round(measured_kpa, 3),
            )
        )

    def _evaluate_holding(self, stage, now: float) -> None:
        """保温中：在带内连续停够才进下一段（末段到点即完成）。"""

        held_s = now - float(self.band_since)
        if held_s < stage.hold_s:
            return
        self.bus.publish(
            Event.of(
                "steam",
                "steam",
                "ramp_stage_held",
                subject=self.writer,
                product_code=self.profile.product_code,
                stage_index=self.stage_index,
                hold_s=round(stage.hold_s, 3),
            )
        )
        if self.stage_index >= len(self.profile.stages) - 1:
            self.phase = RampPhase.COMPLETE
            self.band_since = None
            self.bus.publish(
                Event.of(
                    "steam",
                    "steam",
                    "ramp_complete",
                    subject=self.writer,
                    product_code=self.profile.product_code,
                    stages=len(self.profile.stages),
                    retreats=self.retreats,
                )
            )
        else:
            self.stage_index += 1
            self._enter(RampPhase.RAMPING, now)

    def _command(self, stage) -> float:
        """按相位给出本周期应提交的压力目标。"""

        if self.phase is RampPhase.RETREATING:
            # 压到本段下沿以下（但不低于母管允许下限），让阀关、压力退回来。
            return max(self.floor_kpa, stage.band()[0])
        return stage.target_kpa

    def _summary(self, stage, now: float) -> dict:
        held_s = 0.0
        if self.phase is RampPhase.HOLDING and self.band_since is not None:
            held_s = max(0.0, now - self.band_since)
        low, high = stage.band()
        return {
            "product_code": self.profile.product_code,
            "phase": self.phase.value,
            "stage_index": self.stage_index,
            "stage_count": len(self.profile.stages),
            "target_kpa": round(stage.target_kpa, 3),
            "command_kpa": round(self.last_command_kpa, 3),
            "measured_kpa": round(self.last_measured_kpa, 3),
            "band_low_kpa": round(low, 3),
            "band_high_kpa": round(high, 3),
            "in_band": stage.is_in_band(self.last_measured_kpa or 0.0),
            "hold_s": round(stage.hold_s, 3),
            "held_s": round(held_s, 3),
            "hold_remaining_s": round(max(0.0, stage.hold_s - held_s), 3),
            "retreats": self.retreats,
            "done": self.phase is RampPhase.COMPLETE,
        }

    def snapshot(self) -> dict:
        """执行状态快照（未启动时给出空态）。"""

        if self.started_at is None:
            return {
                "active": False,
                "product_code": self.profile.product_code,
                "phase": RampPhase.RAMPING.value,
                "stages": self.profile.as_dict(),
            }
        stage = self.profile.stage_at(self.stage_index)
        return {
            "active": self.is_running(),
            "product_code": self.profile.product_code,
            "phase": self.phase.value,
            "stage_index": self.stage_index,
            "stage_count": len(self.profile.stages),
            "target_kpa": round(stage.target_kpa, 3),
            "command_kpa": round(self.last_command_kpa, 3),
            "measured_kpa": (
                round(self.last_measured_kpa, 3) if self.last_measured_kpa is not None else None
            ),
            "retreats": self.retreats,
            "stages": self.profile.as_dict(),
        }
