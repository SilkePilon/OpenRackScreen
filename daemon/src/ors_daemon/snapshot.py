from __future__ import annotations

import copy
import enum
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


class Health(enum.Enum):
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class IntegrationHealth:
    state: Health = Health.CONNECTING
    reason: str | None = None
    consecutive_failures: int = 0
    last_success: datetime | None = None
    latency_ms: float | None = None
    stale: bool = False


@dataclass(frozen=True)
class Snapshot:
    data: dict[str, Any]
    version: int
    health: Mapping[str, IntegrationHealth]


class SnapshotStore:
    """The one structure daemon threads share.

    Pollers write, screen workers read and wait. `version` bumps only on new
    *data*, so a failing poll never looks like a reason to redraw.
    """

    def __init__(self, stale_after: int = 3) -> None:
        self._condition = threading.Condition()
        self._data: dict[str, Any] = {}
        self._health: dict[str, IntegrationHealth] = {}
        self._version = 0
        self._stale_after = stale_after
        self._closed = False

    def register(self, name: str) -> None:
        with self._condition:
            self._health.setdefault(name, IntegrationHealth())

    def put(self, name: str, fields: dict[str, Any], latency_ms: float, now: datetime) -> None:
        with self._condition:
            # Copied on the way in as well as out: `fields` belongs to the
            # poller, which is free to reuse or mutate it after handing it over.
            self._data[name] = copy.deepcopy(fields)
            self._version += 1
            self._health[name] = IntegrationHealth(
                state=Health.HEALTHY,
                reason=None,
                consecutive_failures=0,
                last_success=now,
                latency_ms=latency_ms,
                stale=False,
            )
            self._condition.notify_all()

    def fail(self, name: str, reason: str, now: datetime) -> None:
        """Record a failed poll. The last good data stays; the screen judges staleness.

        `now` is taken for symmetry with `put` -- a poller reports either outcome
        the same way -- but nothing here stores it: health carries the time of
        the last *success*, which a failure by definition does not move.
        """
        with self._condition:
            previous = self._health.get(name, IntegrationHealth())
            failures = previous.consecutive_failures + 1
            self._health[name] = replace(
                previous,
                # The states are defined by history, not by the last event: an
                # integration that has never succeeded is still *connecting*, however
                # many times it has failed. The distinction reaches the glass -- a
                # screen shows "WAIT / connecting" for one and "NO DATA" for the
                # other -- and calling a cold start unhealthy would show neither,
                # because there is no data yet for the normal scene to render.
                state=Health.UNHEALTHY if previous.last_success else Health.CONNECTING,
                reason=reason,
                consecutive_failures=failures,
                stale=failures >= self._stale_after,
            )
            # No notify: the version is the only thing anyone waits on, and this
            # did not move it. Waking four screens to have each re-read the same
            # version and go back to sleep is all cost and no news; they pick the
            # new health up at their heartbeat floor, which is what it is for.
            # Anyone who later wants a health change to cut a worker's wait short
            # must widen that predicate -- restoring a notify here would be inert.

    def read(self) -> Snapshot:
        """A snapshot no caller can write back through, data and version agreeing.

        The data is deep-copied because a namespace is nested -- `reduce: top`
        publishes `{"cpu_hot": {"node": ".5", "value": 71.2}}`, and a shallow
        copy would hand all four screens the same inner dict. Health needs only
        a shallow one: `IntegrationHealth` is frozen and every field it holds is
        immutable, `datetime` included.

        Measured at 9.3 us per read on the design's own example namespace (7.8
        of it the copy), so four workers reading four times a second cost 0.015%
        of an x86 core; even scaling twentyfold for the Pi 3B+ leaves it under
        0.3%. There is nothing here worth trading correctness for.
        """
        with self._condition:
            return Snapshot(
                data=copy.deepcopy(self._data),
                version=self._version,
                health=dict(self._health),
            )

    @property
    def closed(self) -> bool:
        """Whether this store is finished: no further data will arrive through it.

        Public because a waiter cannot act on `wait_for_change` alone. That call
        answers "there may be something to do", and for a closed store the honest
        answer is yes and immediately -- so a loop that treats it as its only
        signal spins at the speed of the condition variable. Measured at 0.502s
        of CPU in 0.5s of wall clock, one whole core per screen, with no backend
        call and no render behind it: nothing in the status file, the logs or the
        render counters would show it.

        So the flag itself is readable, and a loop that has nothing to draw
        without new data reads it and leaves. `close` is sticky and one-way,
        which is what makes that safe to act on: a store that has closed cannot
        re-open, so there is nothing to wait around for.
        """
        with self._condition:
            return self._closed

    def close(self) -> None:
        """Release every waiter, now and for good. Idempotent.

        Shutdown's release, and the store's only concession to it. A screen
        worker parks in `wait_for_change` and nothing it holds can cut that
        short -- the stop event is not what it is waiting on -- so without this
        a SIGTERM costs a whole heartbeat floor per panel before anything is
        put to sleep. Measured at 4.8s on a four-panel rack.

        Sticky rather than a bare `notify_all`, and that is the load-bearing
        part. The predicate below re-tests on every wake, so a notification with
        no state behind it releases nobody; and a wake that arrives *before* a
        worker reaches the condition would be missed entirely, which is the same
        four-second delay by another route. A flag that stays set answers both,
        because it is a fact rather than an event.

        It closes the waiting only: `put`, `fail` and `read` all keep working,
        because the status file is written after the threads have been joined.
        """
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def wait_for_change(self, version: int, timeout: float) -> bool:
        """Block until the version leaves `version`. True if it did, False on timeout.

        `version` is the one the caller last read, and testing it is what closes
        the lost-wakeup window: a publish landing between a worker's `read` and
        its `wait` notified an empty room, and only the version still remembers
        it happened. `wait_for` re-tests it after every wake, because the docs
        are clear that `wait` "can return after an arbitrary long time, and the
        condition which prompted the notify() call may no longer hold true" --
        a bare `wait` would report that non-event as a change and cost a render.

        A closed store answers True as well, and immediately, for as long as it
        exists -- that stickiness is what releases a worker parked here when the
        daemon is shutting down, and it cannot be traded away without giving the
        shutdown back its four-second delay. It is therefore *not* a signal a
        caller may loop on: "there may be something to do" is true here forever,
        so a loop that comes straight back gets a busy wait rather than a wait.
        `closed` is the flag such a caller reads on its way round, and the
        screen worker's loop does exactly that.
        """
        with self._condition:
            return self._condition.wait_for(
                lambda: self._version != version or self._closed, timeout=timeout
            )
