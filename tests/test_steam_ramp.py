"""按制品的分段升温：分段目标、到点停够再进段、冲过头退回。"""

from __future__ import annotations

import pytest

from rubbercure.errors import SteamError
from rubbercure.steam.profile import RampProfile, RampProfileBook, RampStage
from rubbercure.steam.ramp import RampPhase, RampRunner


def make_profile() -> RampProfile:
    return RampProfile(
        product_code="RB-X",
        stages=(
            RampStage(target_kpa=200.0, hold_s=30.0, tolerance_kpa=10.0),
            RampStage(target_kpa=400.0, hold_s=40.0, tolerance_kpa=10.0),
            RampStage(target_kpa=600.0, hold_s=0.0, tolerance_kpa=10.0),
        ),
    )


def test_stage_band_and_above():
    stage = RampStage(target_kpa=200.0, hold_s=30.0, tolerance_kpa=10.0)
    assert stage.band() == (190.0, 210.0)
    assert stage.is_in_band(195.0)
    assert not stage.is_in_band(211.0)
    assert stage.is_above(211.0)
    assert not stage.is_above(210.0)


def test_profile_rejects_non_increasing_targets():
    with pytest.raises(SteamError):
        RampProfile(
            product_code="RB-BAD",
            stages=(
                RampStage(200.0, 30.0, 10.0),
                RampStage(180.0, 30.0, 10.0),
            ),
        )


def test_profile_rejects_empty_and_zero_middle_hold():
    with pytest.raises(SteamError):
        RampProfile(product_code="RB-EMPTY", stages=())
    with pytest.raises(SteamError):
        RampProfile(
            product_code="RB-HOLD",
            stages=(
                RampStage(200.0, 0.0, 10.0),
                RampStage(400.0, 0.0, 10.0),
            ),
        )


def test_profile_book_resolves_and_reports_unknown():
    book = RampProfileBook()
    book.register(make_profile())
    assert book.has("RB-X")
    assert book.known_products() == ["RB-X"]
    assert book.resolve("RB-X").final_target_kpa == 600.0
    with pytest.raises(SteamError):
        book.resolve("RB-MISSING")


def test_runner_requires_start():
    runner = RampRunner(make_profile(), clock=None, bus=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        runner.update(100.0, now=0.0)


def drive(runner, values, clock):
    return [runner.update(value, clock.now()) for value in values]


def test_runner_advances_only_after_full_hold(platform):
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    # 升到第一段容差带，进入保温。
    summary = runner.update(200.0, platform.clock.now())
    assert summary["phase"] == "holding"
    assert summary["hold_remaining_s"] == 30.0
    # 只停了 20s 不能进下一段。
    platform.clock.advance(20.0)
    summary = runner.update(200.0, platform.clock.now())
    assert summary["phase"] == "holding"
    assert summary["stage_index"] == 0
    # 停够 30s 才进第二段。
    platform.clock.advance(10.0)
    summary = runner.update(200.0, platform.clock.now())
    assert summary["phase"] == "ramping"
    assert summary["stage_index"] == 1


def test_runner_leaving_band_resets_hold_timer(platform):
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    runner.update(200.0, platform.clock.now())
    platform.clock.advance(25.0)
    runner.update(200.0, platform.clock.now())
    # 保温中途跌出下沿：回 ramping，已停的时间作废。
    summary = runner.update(150.0, platform.clock.now())
    assert summary["phase"] == "ramping"
    runner.update(200.0, platform.clock.now())
    assert runner.phase is RampPhase.HOLDING
    assert runner.band_since == platform.clock.now()


def test_runner_overshoot_retreats_and_recovers(platform):
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    summary = runner.update(250.0, platform.clock.now())
    assert summary["phase"] == "retreating"
    assert runner.retreats == 1
    # 退回命令压到本段下沿以下。
    assert summary["command_kpa"] == 190.0
    # 压力没退回到设定值之前，不能重新逼近。
    summary = runner.update(230.0, platform.clock.now())
    assert summary["phase"] == "retreating"
    summary = runner.update(200.0, platform.clock.now())
    assert summary["phase"] == "ramping"
    # 重新到点后要重新停满时间。
    summary = runner.update(200.0, platform.clock.now())
    assert summary["phase"] == "holding"
    assert summary["hold_remaining_s"] == 30.0


def test_runner_hold_overshoot_invalidates_hold(platform):
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    runner.update(200.0, platform.clock.now())
    platform.clock.advance(29.0)
    runner.update(200.0, platform.clock.now())
    # 到点前最后一刻冲过头：退回，保温计时清零重来。
    summary = runner.update(240.0, platform.clock.now())
    assert summary["phase"] == "retreating"
    runner.update(200.0, platform.clock.now())
    runner.update(200.0, platform.clock.now())
    assert runner.phase is RampPhase.HOLDING
    assert runner.band_since == platform.clock.now()


def test_runner_final_stage_completes(platform):
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    runner.update(200.0, platform.clock.now())
    platform.clock.advance(30.0)
    runner.update(200.0, platform.clock.now())  # -> stage 1
    runner.update(400.0, platform.clock.now())
    platform.clock.advance(40.0)
    runner.update(400.0, platform.clock.now())  # -> stage 2
    summary = runner.update(600.0, platform.clock.now())
    assert summary["phase"] == "complete"
    assert summary["done"] is True
    assert runner.is_done()


def test_overshot_events_are_published(platform):
    seen: list[str] = []
    unsubscribe = platform.bus.subscribe("steam", lambda event: seen.append(event.action))
    runner = RampRunner(make_profile(), platform.clock, platform.bus)
    runner.start()
    runner.update(250.0, platform.clock.now())
    runner.update(200.0, platform.clock.now())
    assert "ramp_overshot" in seen
    assert "ramp_recovered" in seen
    unsubscribe()


def test_default_config_contains_thin_and_thick_profiles(platform):
    codes = {ramp.product_code for ramp in platform.config.ramps}
    assert {"RB-215", "RB-330"} <= codes
    snapshot = platform.ramp_book.snapshot()
    rb215 = snapshot["RB-215"]
    rb330 = snapshot["RB-330"]
    # 厚件分段更细、末段压力更高。
    assert rb330["stage_count"] > rb215["stage_count"]
    assert rb330["final_target_kpa"] > rb215["final_target_kpa"]
    for entry in snapshot.values():
        for index in range(1, entry["stage_count"]):
            assert (
                entry["stages"][index]["target_kpa"] > entry["stages"][index - 1]["target_kpa"]
            )


def test_steam_unit_drives_full_profile_with_holds(platform):
    unit = platform.press("press-01")
    unit.steam.open_steam(640.0)
    started = platform.run_ramp("press-01", "RB-215")
    assert started["ramp"]["active"] is True
    result = platform.drive_ramp("press-01")
    assert result["completed"] is True
    assert result["final_stage"] == 2
    # 每段都有"到点"和"进下一段/完成"的里程碑。
    phases = [(m["phase"], m["stage_index"]) for m in result["milestones"]]
    assert ("reached", 0) in phases
    assert ("reached", 1) in phases
    assert ("reached", 2) in phases
    assert ("complete", 2) in phases


def test_ramp_through_unit_retreats_when_pressure_overshoots(platform):
    unit = platform.press("press-01")
    unit.steam.open_steam(640.0)
    # 人为把母管压力顶到第一段上沿以上，模拟阀门惯性冲过头。
    unit.steam.header.charge(960.0, 40.0)
    platform.run_ramp("press-01", "RB-215")
    result = unit.steam.advance(1.0)
    assert result["ramp"]["phase"] == "retreating"
    driven = platform.drive_ramp("press-01")
    assert driven["completed"] is True
    assert driven["retreats"] >= 1


def test_start_ramp_rejects_target_outside_pressure_window(platform):
    unit = platform.press("press-01")
    unit.steam.open_steam(640.0)
    bad = RampProfile(
        product_code="RB-HIGH",
        stages=(RampStage(target_kpa=900.0, hold_s=0.0, tolerance_kpa=10.0),),
    )
    with pytest.raises(SteamError):
        unit.steam.start_ramp(bad)


def test_close_steam_aborts_active_ramp(platform):
    unit = platform.press("press-01")
    unit.steam.open_steam(640.0)
    platform.run_ramp("press-01", "RB-215")
    assert unit.steam.ramp() is not None
    unit.steam.close_steam()
    assert unit.steam.ramp() is None
    assert unit.steam.ramp_status() == {"active": False}


def test_cure_sequence_starts_configured_ramp(platform):
    report = platform.run_cure("mold-RAMP", product_code="RB-215", cavity_count=2)
    assert report["ramp"]["active"] is True
    assert report["ramp"]["product_code"] == "RB-215"
    assert report["ramp"]["phase"] == "ramping"
