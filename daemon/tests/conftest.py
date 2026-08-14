"""Scaffolding two test files need and neither can import from the other.

`--import-mode=importlib` puts no test directory on `sys.path`, so a helper
defined in `test_snapshot.py` is not reachable from `test_supervisor.py` -- and
a second copy of a synchronisation primitive is the copy that drifts. A fixture
is the supported way for both to have one.
"""

from __future__ import annotations

import threading

import pytest
from ors_daemon.snapshot import SnapshotStore

WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""


class ObservableCondition(threading.Condition):
    """A condition that reports, from inside the lock, each time a thread parks.

    The handshake is exact rather than approximate, which is why nothing that
    uses it sleeps to wait for a thread to get going: `wait()` gives the store
    lock up only once its caller is genuinely parked, so a test that has seen
    the semaphore and then reaches for that lock -- through `put`, through
    `wake`, or through `with condition` -- cannot possibly get there first.

    Which matters most for the tests that are about a *stop*: whether a worker
    is parked or merely about to be is exactly the difference between a wait
    that has to be cut short and one that ends by itself, so a test that guessed
    would pass against the very defect it exists for.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parked = threading.Semaphore(0)

    def wait(self, timeout: float | None = None) -> bool:
        self.parked.release()
        return super().wait(timeout)

    def await_parks(self, count: int) -> None:
        for _ in range(count):
            assert self.parked.acquire(timeout=WAIT), "a worker never parked in wait()"


def watch(store: SnapshotStore) -> ObservableCondition:
    """Make one store's parking observable. Returns the condition to watch.

    Reaching past the public surface is the point: no public method can answer
    "is a worker parked in there", and every test that hangs off this is about
    the difference.
    """
    condition = ObservableCondition()
    store._condition = condition
    return condition


@pytest.fixture
def watched_store():
    """A `SnapshotStore` whose condition variable the test can watch."""

    def build() -> tuple[SnapshotStore, ObservableCondition]:
        store = SnapshotStore()
        return store, watch(store)

    return build


@pytest.fixture
def watch_parks():
    """Make an already-built store's parking observable. See `watch`.

    The supervisor builds its own store before any test can reach it, so that
    one is instrumented rather than constructed.
    """
    return watch
