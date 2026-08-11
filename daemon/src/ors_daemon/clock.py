from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ors_schema.daemon import NightWindow

Clock = Callable[[], datetime]
"""Returns the current time, always timezone-aware. Injected so no test sleeps."""


class ClockError(Exception):
    """Raised for a timezone the host cannot resolve."""


def system_clock(timezone: str) -> Clock:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ClockError(f"unknown timezone {timezone!r}: {exc}") from exc
    return lambda: datetime.now(zone)


class FakeClock:
    """A clock that moves only when a test moves it."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def in_window(now: datetime, window: NightWindow) -> bool:
    """True when `now` falls inside the window. A start after the end wraps midnight."""
    if not window.enabled:
        return False
    start, end = _parse(window.start), _parse(window.end)
    if start == end:
        return False
    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_boundary(now: datetime, window: NightWindow) -> float:
    """Seconds until the window is next entered or left. `inf` when disabled."""
    if not window.enabled:
        return float("inf")
    start, end = _parse(window.start), _parse(window.end)
    if start == end:
        return float("inf")
    target = end if in_window(now, window) else start
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return (candidate - now).total_seconds()
