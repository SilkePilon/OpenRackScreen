from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from ors_daemon.clock import ClockError, FakeClock, in_window, seconds_until_boundary, system_clock
from ors_schema.daemon import NightWindow

AMS = ZoneInfo("Europe/Amsterdam")
WRAPS = NightWindow(start="23:00", end="07:00")
SAME_DAY = NightWindow(start="01:00", end="06:00")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, tzinfo=AMS)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(23, 0), True),
        (at(23, 30), True),
        (at(2, 0), True),
        (at(6, 59), True),
        (at(7, 0), False),
        (at(12, 0), False),
        (at(22, 59), False),
    ],
)
def test_a_window_that_wraps_midnight(now, expected):
    assert in_window(now, WRAPS) is expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [(at(0, 59), False), (at(1, 0), True), (at(5, 59), True), (at(6, 0), False)],
)
def test_a_window_inside_one_day(now, expected):
    assert in_window(now, SAME_DAY) is expected


def test_a_disabled_window_is_never_night():
    assert in_window(at(2, 0), NightWindow(enabled=False)) is False


def test_a_zero_length_window_is_never_night():
    assert in_window(at(3, 0), NightWindow(start="03:00", end="03:00")) is False


@pytest.mark.parametrize(
    ("now", "expected_seconds"),
    [
        (at(22, 0), 3600),
        (at(23, 30), 27000),
        (at(6, 59), 60),
        (at(7, 0), 57600),
    ],
)
def test_seconds_until_the_next_boundary(now, expected_seconds):
    assert seconds_until_boundary(now, WRAPS) == pytest.approx(expected_seconds)


def test_a_disabled_window_has_no_boundary():
    assert seconds_until_boundary(at(2, 0), NightWindow(enabled=False)) == float("inf")


def test_fake_clock_advances_only_when_told():
    clock = FakeClock(at(12, 0))
    assert clock() == at(12, 0)
    clock.advance(90)
    assert clock() == at(12, 0) + timedelta(seconds=90)


def test_system_clock_is_timezone_aware_and_rejects_a_bad_zone():
    assert system_clock("Europe/Amsterdam")().tzinfo is not None
    with pytest.raises(ClockError):
        system_clock("Mars/Olympus_Mons")
