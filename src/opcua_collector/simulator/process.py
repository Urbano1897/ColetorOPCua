"""A fictional thermoplastic press, accelerated so a demo does not wait on cooling.

The numbers are a generic molding cell: melt around 220 °C, a shot of about
42 cm³, clamp force around 900 kN. Nothing here is a real machine. Wall-clock
time is whatever the simulator was started with (a few seconds). The cycle
time written to OPC UA is the process time, roughly 17 seconds, because that
is the number a historian is supposed to store.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

MELT_SETPOINT_C = 220.0
MOLD_SETPOINT_C = 45.0
SHOT_VOLUME_CM3 = 42.0
CLAMP_FORCE_KN = 900.0
HOLD_TIME_S = 2.0
MOLD_MOVEMENT_S = 4.6

# Share of the accelerated wall-clock period. They add up to 1.
PHASES: tuple[tuple[str, float], ...] = (
    ("mold_close", 0.12),
    ("inject", 0.18),
    ("hold", 0.15),
    ("cool", 0.40),
    ("mold_open", 0.08),
    ("eject", 0.07),
)


@dataclass(frozen=True)
class CycleSummary:
    """End-of-cycle snapshot. One of these becomes one database row."""

    cycle_number: int
    melt_temperature: float
    mold_temperature: float
    injection_pressure: float
    hold_pressure: float
    injection_time: float
    cooling_time: float
    cycle_time: float
    shot_volume: float
    clamp_force: float
    screw_cushion: float
    part_quality: str
    alarm_active: bool


@dataclass(frozen=True)
class LiveSignals:
    """Values that move while a cycle is in progress."""

    state: str
    screw_position: float
    hydraulic_pressure: float


def idle_summary() -> CycleSummary:
    """What the press publishes before the first shot has finished."""
    return CycleSummary(
        cycle_number=0,
        melt_temperature=MELT_SETPOINT_C,
        mold_temperature=MOLD_SETPOINT_C,
        injection_pressure=0.0,
        hold_pressure=0.0,
        injection_time=0.0,
        cooling_time=0.0,
        cycle_time=0.0,
        shot_volume=0.0,
        clamp_force=0.0,
        screw_cushion=0.0,
        part_quality="idle",
        alarm_active=False,
    )


def build_cycle(cycle_number: int, rng: random.Random) -> CycleSummary:
    """Build a deterministic shot for this cycle number and random stream."""
    if cycle_number < 1:
        raise ValueError("cycle_number starts at 1; cycle 0 is the idle snapshot")

    # A slow thermal drift so a chart over many shots is not white noise.
    drift = math.sin(cycle_number / 12.0)
    melt = MELT_SETPOINT_C + 1.6 * drift + rng.gauss(0, 0.35)
    mold = MOLD_SETPOINT_C + 0.5 * math.sin(cycle_number / 20.0) + rng.gauss(0, 0.12)
    injection_pressure = 108.0 + 3.0 * drift + rng.gauss(0, 1.2)
    injection_time = 1.15 + rng.gauss(0, 0.03)
    cooling_time = 9.6 + 0.35 * drift + rng.gauss(0, 0.12)
    shot_volume = SHOT_VOLUME_CM3 + rng.gauss(0, 0.15)
    clamp_force = CLAMP_FORCE_KN + rng.gauss(0, 6.0)
    screw_cushion = 5.6 + 0.12 * math.sin(cycle_number / 8.0) + rng.gauss(0, 0.08)

    # Disturbances are applied after the noise so they reliably cross the
    # quality limits. They are scheduled from the cycle number, which keeps
    # a seeded demo repeatable.
    if cycle_number % 37 == 0:
        melt -= 16.0
    if cycle_number % 53 == 0:
        injection_pressure += 45.0
    if cycle_number % 41 == 0:
        shot_volume -= 3.0
    if cycle_number % 17 == 0:
        screw_cushion = 1.2

    melt = _clamp(melt, 180.0, 260.0)
    mold = _clamp(mold, 20.0, 90.0)
    injection_pressure = _clamp(injection_pressure, 0.0, 220.0)
    hold_pressure = _clamp(injection_pressure * 0.48, 0.0, 160.0)
    injection_time = _clamp(injection_time, 0.3, 5.0)
    cooling_time = _clamp(cooling_time, 1.0, 60.0)
    cycle_time = injection_time + HOLD_TIME_S + cooling_time + MOLD_MOVEMENT_S
    shot_volume = _clamp(shot_volume, 0.0, 200.0)
    clamp_force = _clamp(clamp_force, 0.0, 5000.0)
    screw_cushion = _clamp(screw_cushion, 0.0, 40.0)
    quality = _judge(melt, injection_pressure, shot_volume, screw_cushion)
    return CycleSummary(
        cycle_number=cycle_number,
        melt_temperature=round(melt, 2),
        mold_temperature=round(mold, 2),
        injection_pressure=round(injection_pressure, 2),
        hold_pressure=round(hold_pressure, 2),
        injection_time=round(injection_time, 2),
        cooling_time=round(cooling_time, 2),
        cycle_time=round(cycle_time, 2),
        shot_volume=round(shot_volume, 2),
        clamp_force=round(clamp_force, 2),
        screw_cushion=round(screw_cushion, 2),
        part_quality=quality,
        alarm_active=quality == "scrap",
    )


def live_signals(phase: str, progress: float, summary: CycleSummary) -> LiveSignals:
    """Cosmetic in-cycle motion. `progress` is 0..1 inside the phase."""
    progress = min(max(progress, 0.0), 1.0)
    if phase == "inject":
        screw = 80.0 * (1.0 - progress)
        pressure = summary.injection_pressure * math.sin(progress * math.pi)
    elif phase == "hold":
        screw = 3.0
        pressure = summary.hold_pressure
    elif phase == "cool":
        # The screw recovers (plasticizing) while the part cools.
        screw = 3.0 + 75.0 * progress
        pressure = 6.0
    elif phase == "mold_close":
        screw = 80.0
        pressure = 1.0
    else:
        screw = 78.0
        pressure = 0.4
    return LiveSignals(
        state=phase,
        screw_position=round(screw, 2),
        hydraulic_pressure=round(max(pressure, 0.0), 2),
    )


def summary_by_identifier(summary: CycleSummary) -> dict[str, object]:
    """Map catalog identifiers to the values published at the end of a shot."""
    return {
        "Machine.MeltTemperature": summary.melt_temperature,
        "Machine.MoldTemperature": summary.mold_temperature,
        "Machine.InjectionPressure": summary.injection_pressure,
        "Machine.HoldPressure": summary.hold_pressure,
        "Machine.InjectionTime": summary.injection_time,
        "Machine.CoolingTime": summary.cooling_time,
        "Machine.CycleTime": summary.cycle_time,
        "Machine.ShotVolume": summary.shot_volume,
        "Machine.ClampForce": summary.clamp_force,
        "Machine.ScrewCushion": summary.screw_cushion,
        "Machine.PartQuality": summary.part_quality,
        "Machine.AlarmActive": summary.alarm_active,
    }


def _judge(melt: float, pressure: float, shot: float, cushion: float) -> str:
    if not 210.0 <= melt <= 230.0:
        return "scrap"
    if pressure > 145.0 or pressure < 70.0:
        return "scrap"
    if shot < SHOT_VOLUME_CM3 - 1.5:
        return "scrap"
    if cushion < 2.5 or cushion > 10.0:
        return "review"
    return "good"


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
