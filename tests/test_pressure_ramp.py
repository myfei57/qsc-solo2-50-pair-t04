"""分段升温压力曲线：按制品分段、到点停满、冲过头退回。

覆盖三件事：
1. 段表按制品登记、代际取最新、非法段表（空表/回退/非正参数）被拒；
2. 运行期段序——进窗口才算到点、停满才进下一段、中途冲过窗口上限要
   退回升温段并把已停时间清零，回来重新停满；
3. 平台集成——硫化时按制品挂曲线、控制周期由段序以高优先级提交段目标、
   停汽丢弃、状态随落盘恢复。
"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError, SequenceError
from rubbercure.steam.ramp import PressureRamp, PressureStage, RampBook, RampPhase, RampRunner
from rubbercure.steam.state import SteamPhase


def _ramp(product="P1"):
    book = RampBook()
    return book.register(product, [(200.0, 10.0), (500.0, 20.0)], 1.0)


def test_ramp_book_resolves_latest_generation():
    book = RampBook()
    book.register("P1", [(200.0, 10.0)], 1.0)
    second = book.register("P1", [(200.0, 10.0), (500.0, 20.0)], 2.0)
    latest = book.get("P1")
    assert latest is not None and latest.generation == second.generation == 2
    assert latest.final_target_kpa == 500.0
    assert book.get("missing") is None
    assert set(book.snapshot()) == {"P1"}


def test_pressure_ramp_rejects_non_monotonic_or_invalid_stages():
    with pytest.raises(ValueError):
        PressureRamp("X", 1, (), 0.0)
    with pytest.raises(ValueError):
        PressureRamp("X", 1, (PressureStage(200.0, 10.0), PressureStage(200.0, 5.0)), 0.0)
    with pytest.raises(ValueError):
        PressureRamp("X", 1, (PressureStage(300.0, 10.0), PressureStage(200.0, 5.0)), 0.0)
    with pytest.raises(ValueError):
        PressureStage(0.0, 10.0)
    with pytest.raises(ValueError):
        PressureStage(200.0, 0.0)


def test_stage_not_reached_until_pressure_enters_band():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    view = runner.step(100.0, 5.0)
    assert view["phase"] == "heating" and view["held_s"] == 0.0
    assert runner.phase is RampPhase.HEATING


def test_hold_must_complete_before_advancing():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    runner.step(205.0, 5.0)  # 进窗口 -> holding, held=5
    view = runner.step(205.0, 4.0)  # 9s，未停满
    assert view["stage_index"] == 0 and view["phase"] == "holding"
    view = runner.step(205.0, 2.0)  # 11s，进入第二段
    assert view["advanced"] is True
    assert view["stage_index"] == 1 and view["target_kpa"] == 500.0
    assert runner.phase is RampPhase.HEATING


def test_overshoot_during_hold_backs_off_and_clears_timer():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    runner.step(205.0, 5.0)
    assert runner.phase is RampPhase.HOLDING
    view = runner.step(230.0, 4.0)  # 冲过上限 215
    assert view["receded"] is True
    assert runner.phase is RampPhase.HEATING and view["held_s"] == 0.0
    # 回来必须重新停满 10s：之前的 5s 已作废
    runner.step(205.0, 1.0)
    for _ in range(8):
        runner.step(205.0, 1.0)
    assert runner.phase is RampPhase.HOLDING  # 9s 还没进段
    view = runner.step(205.0, 2.0)
    assert view["advanced"] is True and view["stage_index"] == 1


def test_dipping_below_band_during_hold_also_restarts_timer():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    runner.step(205.0, 8.0)
    view = runner.step(180.0, 1.0)  # 掉到下限以下
    assert view["receded"] is True and view["held_s"] == 0.0
    assert runner.phase is RampPhase.HEATING


def test_runner_completes_after_final_stage_full_hold():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    runner.step(200.0, 11.0)  # 段0停满 -> 段1
    view = runner.step(500.0, 21.0)  # 段1停满 -> 完成
    assert view["completed"] is True and view["active"] is False
    assert runner.phase is RampPhase.COMPLETE
    assert runner.is_driving() is False
    # 完成后再 step 不再驱动
    assert runner.step(500.0, 1.0) == {"active": False, "phase": "complete"}


def test_start_refuses_while_already_driving():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp())
    with pytest.raises(InterlockError):
        runner.start(_ramp())


def test_runner_snapshot_restore_roundtrip():
    runner = RampRunner(tolerance_kpa=15.0)
    runner.start(_ramp(), mold_id="m9")
    runner.step(205.0, 4.0)
    snapshot = runner.snapshot()
    assert snapshot["phase"] == "holding" and snapshot["mold_id"] == "m9"

    restored = RampRunner(tolerance_kpa=15.0)
    restored.restore(snapshot)
    again = restored.snapshot()
    assert again["phase"] == "holding"
    assert abs(again["held_s"] - 4.0) < 1e-9
    assert again["product_code"] == "P1" and again["stage_index"] == 0


def _drive_to(unit, pressure):
    unit.steam.header._pressure = pressure
    return unit.steam.advance(1.0)


def test_platform_attaches_product_ramp_and_arbiter_follows_stage(platform):
    report = platform.run_cure("mold-R", cavity_count=2)
    assert report["pressure_ramp"]["product_code"] == "RB-215"
    unit = platform.press("press-01")
    assert unit.steam.ramp_target_kpa() == 260.0
    view = unit.steam.advance(1.0)
    assert view["ramp"]["active"] is True
    assert view["winner"] == "pressure_ramp"
    assert view["target_kpa"] == 260.0


def test_platform_thick_product_uses_its_own_ramp(platform):
    report = platform.run_cure("mold-T", product_code="RB-310", cavity_count=4)
    targets = [stage["target_kpa"] for stage in report["pressure_ramp"]["stages"]]
    assert targets == [pytest.approx(v) for v in (240.0, 400.0, 560.0, 700.0)]


def test_platform_ramp_progresses_and_survives_persistence(platform):
    platform.run_cure("mold-R", cavity_count=2)
    unit = platform.press("press-01")
    _drive_to(unit, 262.0)
    for _ in range(30):
        _drive_to(unit, 262.0)
    assert unit.steam.snapshot()["ramp"]["stage_index"] == 1
    unit.steam.persist_state()
    assert unit.steam.restore()
    assert unit.steam.snapshot()["ramp"]["stage_index"] == 1


def test_platform_overshoot_backs_off_in_real_loop(platform):
    platform.run_cure("mold-R", cavity_count=2)
    unit = platform.press("press-01")
    _drive_to(unit, 262.0)
    for _ in range(30):
        _drive_to(unit, 262.0)
    # 第二段到位保温 10s，再冲过窗口上限
    _drive_to(unit, 450.0)
    for _ in range(10):
        _drive_to(unit, 450.0)
    _drive_to(unit, 470.0)
    snap = unit.steam.snapshot()["ramp"]
    assert snap["phase"] == "heating" and snap["held_s"] == 0.0


def test_close_steam_drops_ramp(platform):
    platform.run_cure("mold-R", cavity_count=2)
    unit = platform.press("press-01")
    assert unit.steam.phase is SteamPhase.HEATING
    unit.steam.close_steam()
    assert unit.steam.snapshot()["ramp"]["phase"] == "idle"
    assert unit.steam.ramp_target_kpa() == platform.config.steam.target_pressure_kpa


def test_start_pressure_ramp_requires_heating_phase(platform):
    unit = platform.press("press-01")
    with pytest.raises(SequenceError):
        unit.steam.start_pressure_ramp(_ramp(), mold_id="mold-R")
