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


def test_a_zero_length_window_has_no_boundary():
    # Both functions have to agree that start == end is "never night" rather than
    # "always night": a window that is never entered has no boundary to wait for.
    zero = NightWindow(start="03:00", end="03:00")
    assert in_window(at(3, 0), zero) is False
    assert seconds_until_boundary(at(3, 0), zero) == float("inf")


def test_standing_on_a_boundary_waits_a_whole_window_not_zero():
    # A worker that ticks exactly at 23:00 must size its sleep at the whole
    # night, not at 0 -- a 0 here is a busy loop for the length of the window.
    assert seconds_until_boundary(at(23, 0), WRAPS) == pytest.approx(8 * 3600)
    assert seconds_until_boundary(at(1, 0), SAME_DAY) == pytest.approx(5 * 3600)


# --- DST. Europe/Amsterdam shifts twice a year and 23:00-07:00 spans both. ---
#
# `in_window` compares wall clock, which is what the rack owner configured: at
# 02:30 the panels are dark whichever offset is in force. `seconds_until_boundary`
# sizes a real sleep, so it must answer in elapsed seconds, and on these two
# nights elapsed and wall-clock seconds differ by an hour.


def test_the_wait_across_the_spring_forward_night_is_elapsed_not_wall_seconds():
    # 2026-03-29: 02:00 CET -> 03:00 CEST, so 02:00-03:00 never happens and
    # 23:00 -> 07:00 is 7 real hours. Answering 8h wall makes a worker sleep an
    # hour past its wake time, and `in_window` is already False by then, so
    # nothing re-checks: the panels just stay dark until 08:00.
    now = datetime(2026, 3, 28, 23, 0, tzinfo=AMS)
    assert seconds_until_boundary(now, WRAPS) == pytest.approx(7 * 3600)


def test_the_wait_across_the_autumn_back_night_is_elapsed_not_wall_seconds():
    # 2026-10-25: 03:00 CEST -> 02:00 CET, so 02:00-03:00 happens twice and
    # 23:00 -> 07:00 is 9 real hours.
    now = datetime(2026, 10, 24, 23, 0, tzinfo=AMS)
    assert seconds_until_boundary(now, WRAPS) == pytest.approx(9 * 3600)


def test_the_repeated_hour_is_night_on_both_passes():
    for fold in (0, 1):
        now = datetime(2026, 10, 25, 2, 30, fold=fold, tzinfo=AMS)
        assert in_window(now, WRAPS) is True


@pytest.mark.parametrize("day", [datetime(2026, 3, 29), datetime(2026, 10, 25)])
@pytest.mark.parametrize("window", [WRAPS, SAME_DAY])
def test_the_wait_is_always_positive_across_a_transition_day(day, window):
    # A non-positive answer anywhere on a transition day is a worker spinning.
    for minutes in range(0, 24 * 60):
        for fold in (0, 1):
            now = (day + timedelta(minutes=minutes)).replace(fold=fold, tzinfo=AMS)
            assert seconds_until_boundary(now, window) > 0


def test_fake_clock_advances_only_when_told():
    clock = FakeClock(at(12, 0))
    assert clock() == at(12, 0)
    clock.advance(90)
    assert clock() == at(12, 0) + timedelta(seconds=90)


def test_system_clock_is_timezone_aware_and_rejects_a_bad_zone():
    assert system_clock("Europe/Amsterdam")().tzinfo is not None
    with pytest.raises(ClockError):
        system_clock("Mars/Olympus_Mons")
