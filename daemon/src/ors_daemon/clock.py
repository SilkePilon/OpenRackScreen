from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ors_schema.daemon import NightWindow

Clock = Callable[[], datetime]
"""Returns the current time, always timezone-aware. Injected so no test sleeps."""


class ClockError(Exception):
    """Raised for a timezone the host cannot resolve, or a time carrying none."""


def system_clock(timezone: str) -> Clock:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ClockError(f"unknown timezone {timezone!r}: {exc}") from exc
    return lambda: datetime.now(zone)


class FakeClock:
    """A clock that moves only when a test moves it."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ClockError("FakeClock needs an aware start; a naive one is no wall time anywhere")
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move forward by `seconds` of *elapsed* time, not of wall clock.

        The distinction only shows up across a DST transition, and that is
        exactly where it matters: `seconds_until_boundary` returns elapsed
        seconds, so a test that advances by its answer must land on the
        boundary. Adding a `timedelta` to an aware datetime does naive
        arithmetic that keeps the offset fixed -- advancing 25200s across the
        spring-forward night would land an hour short of the configured wake.
        """
        zone = self._now.tzinfo
        self._now = (self._now.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(zone)


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def in_window(now: datetime, window: NightWindow) -> bool:
    """True when `now` falls inside the window. A start after the end wraps midnight.

    Compared on the wall clock, which is what the window is written in: across a
    DST shift the repeated hour reads as night on both passes, and the skipped
    hour simply never arrives. Neither needs a special case -- the panels are
    dark whenever the clock on the wall says they should be.
    """
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
    """Seconds until the window is next entered or left, as elapsed time.

    `inf` when there is no boundary to wait for: a disabled window, or one whose
    start equals its end, both of which `in_window` reads as never night.
    """
    if not window.enabled:
        return float("inf")
    start, end = _parse(window.start), _parse(window.end)
    if start == end:
        return float("inf")
    if now.utcoffset() is None:
        raise ClockError(f"need a timezone-aware time, got naive {now.isoformat()}")
    target = end if in_window(now, window) else start
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    # Two comparisons on two different clocks, deliberately. *Which* occurrence of
    # the boundary is next is a wall-clock question -- the rack owner set 07:00 and
    # means the 07:00 the clock on the wall shows. *How long until it* is a question
    # about elapsed time, because the caller spends the answer on a sleep.
    #
    # They differ by an hour on the two nights Europe/Amsterdam shifts, and
    # subtracting the aware datetimes directly answers the first question twice:
    # `candidate` carries `now`'s own tzinfo object, and datetime documents that
    # subtraction between two aware datetimes with the same tzinfo "ignores" it and
    # returns the wall-clock difference. That reads as 8h across the spring-forward
    # night when only 7h elapse, and a worker sleeping 8h wakes at 08:00 with the
    # window already closed behind it -- an hour of dark panels nothing corrects.
    # Converting to UTC forces the elapsed-time answer.
    if candidate.replace(tzinfo=None) <= now.replace(tzinfo=None):
        candidate = candidate + timedelta(days=1)
    return (candidate.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
