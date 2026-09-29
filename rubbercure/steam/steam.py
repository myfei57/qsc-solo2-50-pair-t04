"""蒸汽加热单元。

把母管、疏水阀、主蒸汽阀、目标值仲裁器与压力调节器装成一台硫化机的蒸汽
回路，对外暴露排气、升温、泄压、停汽四个动作，以及压力闭环的一步推进。
所有顺序约束都在这个门面上执行，控制台与排产不会绕过它直接改阀位。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..config import SteamLimits
from ..errors import SteamError
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore
from .header import SteamHeader
from .pressure import PressureRegulator
from .profile import RampProfile
from .purge import PurgeToken, purge_cavity
from .ramp import RampRunner
from .sequence import close_steam, open_steam
from .setpoint import SetpointArbiter
from .state import SteamPhase, SteamStateMachine
from .trap import DrainTrap
from .valve import SteamValve
from .vent import SAFE_RESIDUAL_KPA, VentToken, vent_pressure

STATE_KEY = "steam_state"
RAMP_WRITER = "ramp_program"
RAMP_PRIORITY = 80
RAMP_BLEED_KPA_PER_S = 2.5
RAMP_PROPORTIONAL_GAIN = 0.12
# 与 SteamHeader.charge 的物理参数保持一致：每周期增益=流量*dt*0.012，
# 散热损失=0.35 kPa/s。前馈开度按"在目标压力下平衡散热所需开度"折算。
RAMP_CHARGE_GAIN_PER_FLOW = 0.012
RAMP_LOSS_KPA_PER_S = 0.35


class SteamUnit:
    """一台硫化机的蒸汽回路。"""

    def __init__(
        self,
        clock: Clock,
        bus: EventBus,
        store: JsonStore,
        limits: SteamLimits,
    ) -> None:
        self.clock = clock
        self.limits = limits
        self._bus = bus
        self._store = store
        self._state = SteamStateMachine()
        self.header = SteamHeader(bus_pressure_kpa=0.0)
        self.trap = DrainTrap(bus, rated_flow_kg_h=limits.trap_rated_flow_kg_h)
        self.valve = SteamValve(bus, rated_flow_kg_h=limits.valve_rated_flow_kg_h)
        self.arbiter = SetpointArbiter(
            min_kpa=limits.min_pressure_kpa,
            max_kpa=limits.max_pressure_kpa,
            default_kpa=limits.target_pressure_kpa,
        )
        self.regulator = PressureRegulator(
            kp=0.09, ki=0.02, output_min=0.0, output_max=100.0
        )
        self._purge_token: PurgeToken | None = None
        self._vent_token: VentToken | None = None
        self._ramp: RampRunner | None = None
        self._ramp_stage = -1
        self._ramp_phase = ""
        self._lock = threading.RLock()

    @property
    def phase(self) -> SteamPhase:
        return self._state.phase

    @property
    def pressure_kpa(self) -> float:
        return self.header.pressure_kpa

    def transition_state(self, target: SteamPhase) -> SteamPhase:
        return self._state.transition(target)

    def opening_for_target(self, target_kpa: float) -> float:
        """把压力目标折算成阀门开度。"""

        clamped = self.limits.clamp_pressure(target_kpa)
        span = max(1.0, self.limits.max_pressure_kpa - self.limits.min_pressure_kpa)
        return 100.0 * (clamped - self.limits.min_pressure_kpa) / span

    def set_valve(self, opening_pct: float) -> float:
        return self.valve.set_opening(opening_pct)

    def close_valve(self) -> float:
        return self.valve.close()

    def remember_purge(self, token: PurgeToken) -> None:
        with self._lock:
            self._purge_token = token

    def remember_vent(self, token: VentToken) -> None:
        with self._lock:
            self._vent_token = token

    def purge_token(self) -> PurgeToken | None:
        with self._lock:
            return self._purge_token

    def vent_token(self) -> VentToken | None:
        with self._lock:
            return self._vent_token

    def is_purged(self) -> bool:
        token = self.purge_token()
        return token is not None and token.is_valid()

    def is_vented(self) -> bool:
        token = self.vent_token()
        return token is not None and token.is_safe()

    def purge_cavity(self, mold_id: str, generation: int = 0) -> PurgeToken:
        """排气并返回凭据。"""

        return purge_cavity(self, mold_id, self._bus, generation=generation)

    def vent_pressure(self, mold_id: str) -> VentToken:
        """泄压并返回凭据。"""

        return vent_pressure(self, mold_id, self._bus)

    def open_steam(self, target_kpa: float) -> dict:
        """按"先疏水、后蒸汽"升温。"""

        return open_steam(self, self._bus, target_kpa)

    def close_steam(self) -> dict:
        """按"先蒸汽、后疏水"停汽。"""

        return close_steam(self, self._bus)

    def submit_setpoint(self, writer: str, target_kpa: float, priority: int) -> None:
        """由一个写入方提交压力目标值。"""

        self.arbiter.request(writer, target_kpa, priority, reason=f"writer={writer}")

    def start_ramp(self, profile: RampProfile) -> dict:
        """按制品分段配方启动升温：校验各段目标可达后从第一段开始。"""

        for stage in profile.stages:
            clamped = self.limits.clamp_pressure(stage.target_kpa)
            if abs(clamped - stage.target_kpa) > 1e-6:
                raise SteamError(
                    f"制品 {profile.product_code} 的分段目标 {stage.target_kpa} kPa "
                    f"超出母管允许窗口 ({self.limits.min_pressure_kpa}, "
                    f"{self.limits.max_pressure_kpa})",
                    component="steam",
                )
        runner = RampRunner(
            profile=profile,
            clock=self.clock,
            bus=self._bus,
            floor_kpa=self.limits.min_pressure_kpa,
            writer=RAMP_WRITER,
        )
        runner.start()
        with self._lock:
            self._ramp = runner
            self._ramp_stage = -1
            self._ramp_phase = ""
        self._bus.publish(
            Event.of(
                "steam",
                "steam",
                "ramp_started",
                subject=RAMP_WRITER,
                product_code=profile.product_code,
                stages=len(profile.stages),
            )
        )
        return runner.snapshot()

    def ramp(self) -> RampRunner | None:
        with self._lock:
            return self._ramp

    def stop_ramp(self) -> None:
        """中止分段升温（停汽/检修时调用）。"""

        with self._lock:
            self._ramp = None
            self._ramp_stage = -1
            self._ramp_phase = ""

    def ramp_status(self) -> dict:
        """分段升温状态；未启动时给出空态。"""

        runner = self.ramp()
        if runner is None:
            return {"active": False}
        return runner.snapshot()

    def advance(self, dt: float) -> dict:
        """推进一个控制周期：结算目标值、调节阀位、母管充压。"""

        runner = self.ramp()
        with self._lock:
            ramp_summary: dict | None = None
            if runner is not None:
                ramp_summary = runner.update(self.header.pressure_kpa, self.clock.now())
                self.arbiter.request(
                    RAMP_WRITER,
                    ramp_summary["command_kpa"],
                    RAMP_PRIORITY,
                    reason=f"ramp={ramp_summary['phase']}",
                )
            outcome = self.arbiter.resolve()
            if runner is not None:
                # 分段升温用纯比例阀控：前馈开度抵消散热损失，再按实测偏差修正；
                # 换段或退回瞬间清掉旧积分，退回段小幅放压，让压力退回设定值，
                # 避免在容差带两侧来回甩。
                if (
                    runner.stage_index != self._ramp_stage
                    or runner.phase.value != self._ramp_phase
                ):
                    self.regulator.reset()
                    self._ramp_stage = runner.stage_index
                    self._ramp_phase = runner.phase.value
                error_kpa = outcome.target_kpa - self.header.pressure_kpa
                # 前馈开度按"平衡散热所需开度"折算（母管模型按额定流量计增益），
                # 再叠加比例修正；否则按 0~100 的开度前馈会被当成满开进汽。
                rated_pressure_rate = (
                    self.limits.valve_rated_flow_kg_h * RAMP_CHARGE_GAIN_PER_FLOW
                )
                opening = 100.0 * RAMP_LOSS_KPA_PER_S / rated_pressure_rate
                opening += RAMP_PROPORTIONAL_GAIN * error_kpa
                self.valve.set_opening(max(0.0, min(100.0, opening)))
                if ramp_summary["phase"] == "retreating":
                    self.header.bleed(dt, RAMP_BLEED_KPA_PER_S)
                pressure = self.header.charge(self.valve.flow_kg_h(), dt)
                ramp_summary["pressure_kpa"] = round(pressure, 3)
            else:
                if self.valve.opening_pct <= 0.0 and self._state.phase is SteamPhase.HEATING:
                    opening = self.opening_for_target(outcome.target_kpa)
                    self.valve.set_opening(opening)
                bias = self.regulator.update(self.header.pressure_kpa, outcome.target_kpa, dt)
                if self._state.phase in (SteamPhase.HEATING, SteamPhase.HOLDING):
                    self.valve.set_opening(self.valve.opening_pct + bias * 0.05)
                pressure = self.header.charge(self.valve.flow_kg_h(), dt)
            return {
                "winner": outcome.winner,
                "target_kpa": round(outcome.target_kpa, 3),
                "pressure_kpa": round(pressure, 3),
                "valve_opening_pct": round(self.valve.opening_pct, 2),
                "trap_opening_pct": round(self.trap.opening_pct, 2),
                "ramp": ramp_summary,
            }

    def persist_state(self) -> str:
        """把相位与压力落盘，返回内容摘要。"""

        snapshot = self._state.snapshot()
        snapshot["pressure_kpa"] = round(self.header.pressure_kpa, 4)
        snapshot["revision"] = len(snapshot["history"])
        digest = self._store.write(STATE_KEY, snapshot)
        self._bus.publish(
            Event.of("steam", "steam", "state_durable", subject=STATE_KEY, digest=digest[:12])
        )
        return digest

    def restore(self) -> bool:
        """从磁盘恢复相位与压力。"""

        snapshot = self._store.read(STATE_KEY)
        if not snapshot:
            return False
        self._state.restore(snapshot)
        pressure = snapshot.get("pressure_kpa")
        if isinstance(pressure, (int, float)):
            self.header.relax_toward(float(pressure), 1.0)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._state.phase.value,
                "history": self._state.history(),
                "header": self.header.snapshot(),
                "valve": self.valve.snapshot(),
                "trap": self.trap.snapshot(),
                "arbiter": self.arbiter.snapshot(),
                "arbiter_decisions": self.arbiter.decisions(10),
                "regulator": self.regulator.snapshot(),
                "purged": self.is_purged(),
                "vented": self.is_vented(),
                "ramp": self.ramp_status(),
            }
