from __future__ import annotations

import threading


class Limiter:
    """A rolling-window attempt counter, keyed on whatever the caller passes.

    Extracted out of `Sessions` rather than shared from it: that class's window
    is the one `login` and `change_password` use for password guesses, and a
    second, unauthenticated endpoint filing claims needs a different number for
    a different reason. Sharing one counter would mean a rack filing claims
    could lock an admin out of logging in, so each caller gets its own
    `Limiter` with its own budget instead.
    """

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        # A `def` endpoint runs in the threadpool, so several callers really are
        # concurrent. The bookkeeping is a read, a rebuild and a write, and an
        # attempt lost between them is an attempt the limiter did not count.
        self._lock = threading.Lock()

    def too_many(self, key: str, now: float) -> bool:
        with self._lock:
            # Every key is swept, not just this one: the keys are chosen by
            # whoever can reach the endpoint, nothing else deletes them, and a
            # dict that only grows is a slow leak an unauthenticated caller
            # controls.
            self._attempts = {
                address: recent
                for address, attempts in self._attempts.items()
                if (recent := [at for at in attempts if now - at < self._window_seconds])
            }
            return len(self._attempts.get(key, ())) >= self._max_attempts

    def record(self, key: str, now: float) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        """Called when the attempt was proved legitimate, so failures already
        answered for cannot add up to a lockout for a caller who has since
        succeeded.
        """
        with self._lock:
            self._attempts.pop(key, None)

    def size(self) -> int:
        """How many keys are currently tracked. For tests: a dict keyed on an
        address nobody chose, with an unbounded list per key, is a memory leak
        an unauthenticated caller can drive if expired entries are not dropped.
        """
        return len(self._attempts)
