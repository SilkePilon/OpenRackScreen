"""One rolling window, used by two endpoints with different budgets.

Extracted rather than reused in place: `Sessions.too_many_attempts` is about
password guesses and carries that window. The claim endpoint is unauthenticated
and its limit exists to stop a queue anybody can fill, which is a different
number for a different reason -- and sharing one counter would mean a rack
filing claims could lock an admin out of logging in.
"""

from __future__ import annotations

from ors_server.limiter import Limiter


def test_it_permits_up_to_the_limit():
    limiter = Limiter(max_attempts=3, window_seconds=60)
    for second in range(3):
        assert limiter.too_many("10.0.0.1", second) is False
        limiter.record("10.0.0.1", second)
    assert limiter.too_many("10.0.0.1", 3) is True


def test_the_window_rolls():
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    assert limiter.too_many("10.0.0.1", 59) is True
    assert limiter.too_many("10.0.0.1", 61) is False


def test_one_client_does_not_limit_another():
    """Otherwise a single noisy rack locks every admin out of the interface."""
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    assert limiter.too_many("10.0.0.2", 0) is False


def test_clearing_forgets_a_client():
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    limiter.clear("10.0.0.1")
    assert limiter.too_many("10.0.0.1", 0) is False


def test_expired_attempts_are_not_kept_for_ever():
    """A dict keyed on address with an unbounded list per key is a memory leak
    an unauthenticated endpoint can drive."""
    limiter = Limiter(max_attempts=100, window_seconds=10)
    for second in range(50):
        limiter.record("10.0.0.1", second)
    limiter.too_many("10.0.0.1", 1000)
    assert limiter.size() == 0
