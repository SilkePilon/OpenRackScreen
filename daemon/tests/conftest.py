"""Scaffolding two test files need and neither can import from the other.

`--import-mode=importlib` puts no test directory on `sys.path`, so a helper
defined in `test_snapshot.py` is not reachable from `test_supervisor.py` -- and
a second copy of a synchronisation primitive is the copy that drifts. A fixture
is the supported way for both to have one.
"""

from __future__ import annotations

import threading
from typing import Any

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


@pytest.fixture(autouse=True)
def _no_network_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this directory browses the LAN for a server.

    Row 4 of `_boot`'s table -- unpaired, no `--config`, no `--server` -- runs
    `discovery.discover` for real, and a test that reaches it finds whatever
    happens to be on the developer's machine, joins a multicast group out of
    whatever CI runner it is on, and then blocks in `join_a_server` until the
    per-test timeout. That is not hypothetical: it is what the first run of
    this suite did once Task 15 replaced row 4's stub. The same reasoning
    `server/tests/conftest.py::_no_mdns_announcement` is written down with
    applies here -- a guard a test has to remember is a guard that gets
    forgotten, and forgetting it is invisible on a laptop and a multicast
    packet out of CI.

    **Both names, and `discover` is the one that does the work.** Patching
    `browse` alone would not stop anything: `discover` binds it as a default
    argument at definition time, so the module attribute is not what it calls,
    and it catches `Exception` around the browse and answers `[]` -- so an
    `AssertionError` from `browse` would be swallowed into "no server found"
    and the test would pass while having tried. `browse` is patched anyway,
    for anything that reaches past `discover` to call it directly.

    `test_discovery.py` is unaffected: it imports both names directly and hands
    every `discover` call its own stub factory, which is the seam this module
    exists to have.
    """

    def refuse(*args: object, **kwargs: object) -> list[Any]:
        raise AssertionError(
            "a test browsed the network; inject a `servers` callable, or pass --server"
        )

    monkeypatch.setattr("ors_daemon.discovery.browse", refuse)
    monkeypatch.setattr("ors_daemon.discovery.discover", refuse)
