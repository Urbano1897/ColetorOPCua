"""Exponential backoff shared by the OPC UA session and the database writer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class Backoff:
    """Delay sequence: initial, initial*multiplier, ... capped at max.

    The first failure waits `initial_s`, not zero, so a dead peer cannot
    turn into a tight reconnect loop. `reset` is called after a session
    has actually come up.
    """

    initial_s: float
    max_s: float
    multiplier: float
    _current: float | None = None

    def failure_delay(self) -> float:
        if self._current is None:
            self._current = self.initial_s
        else:
            self._current = min(self.max_s, self._current * self.multiplier)
        return self._current

    def reset(self) -> None:
        self._current = None


async def sleep_or_stop(seconds: float, stop: asyncio.Event) -> bool:
    """Sleep for `seconds`, or return early when `stop` is set.

    Returns True when the caller should shut down.
    """
    if seconds < 0:
        raise ValueError("sleep duration cannot be negative")
    if stop.is_set():
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
