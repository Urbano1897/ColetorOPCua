import random

import pytest

from opcua_collector.simulator.process import (
    HOLD_TIME_S,
    MOLD_MOVEMENT_S,
    PHASES,
    build_cycle,
    idle_summary,
    live_signals,
)


def test_phase_fractions_cover_one_cycle() -> None:
    assert sum(fraction for _, fraction in PHASES) == pytest.approx(1.0)


def test_cycles_are_repeatable_for_a_seed() -> None:
    first = build_cycle(4, random.Random(0))
    second = build_cycle(4, random.Random(0))
    assert first == second


def test_a_normal_shot_stays_inside_a_plausible_band() -> None:
    shot = build_cycle(1, random.Random(0))
    assert shot.part_quality == "good"
    assert shot.alarm_active is False
    assert 210.0 <= shot.melt_temperature <= 230.0
    assert 30.0 <= shot.mold_temperature <= 60.0
    assert 80.0 <= shot.injection_pressure <= 140.0
    assert shot.cycle_time == pytest.approx(
        shot.injection_time + HOLD_TIME_S + shot.cooling_time + MOLD_MOVEMENT_S,
        abs=0.03,
    )
    assert shot.cycle_time > shot.injection_time + shot.cooling_time


def test_scripted_disturbances_change_the_judgement() -> None:
    rng = random.Random(0)
    assert build_cycle(17, rng).part_quality == "review"
    assert build_cycle(37, random.Random(0)).part_quality == "scrap"
    assert build_cycle(41, random.Random(0)).part_quality == "scrap"
    assert build_cycle(53, random.Random(0)).alarm_active is True


def test_idle_snapshot_is_not_a_finished_shot() -> None:
    idle = idle_summary()
    assert idle.cycle_number == 0
    assert idle.part_quality == "idle"
    assert idle.injection_pressure == 0.0
    assert idle.melt_temperature == 220.0


def test_screw_moves_forward_during_injection() -> None:
    shot = build_cycle(1, random.Random(0))
    start = live_signals("inject", 0.0, shot)
    end = live_signals("inject", 1.0, shot)
    assert start.screw_position > end.screw_position
    assert end.hydraulic_pressure == pytest.approx(0.0, abs=0.01)
