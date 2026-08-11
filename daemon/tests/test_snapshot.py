import threading
from datetime import UTC, datetime

from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""


class _ObservableCondition(threading.Condition):
    """A condition that reports, from inside the lock, each time a thread parks.

    The handshake is exact rather than approximate, which is why nothing here
    sleeps to wait for a thread to get going: `wait()` gives the store lock up
    only once its caller is genuinely parked, so a test that has seen the
    semaphore and then reaches for that lock -- through `put`, or `with
    condition` -- cannot possibly get there first.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parked = threading.Semaphore(0)

    def wait(self, timeout: float | None = None) -> bool:
        self.parked.release()
        return super().wait(timeout)


def _watched_store() -> tuple[SnapshotStore, _ObservableCondition]:
    """A store whose condition variable the test can watch workers park on.

    Reaching past the public surface is the point: whether a worker is parked or
    merely about to be is exactly the difference these tests are about, and no
    public method can answer it.
    """
    store = SnapshotStore()
    condition = _ObservableCondition()
    store._condition = condition
    return store, condition


def _await_parks(condition: _ObservableCondition, count: int) -> None:
    for _ in range(count):
        assert condition.parked.acquire(timeout=WAIT), "a worker never parked in wait()"


def test_a_registered_integration_starts_connecting_with_no_data():
    store = SnapshotStore()
    store.register("prom")

    snap = store.read()
    assert snap.version == 0
    assert snap.data == {}
    assert snap.health["prom"].state is Health.CONNECTING
    assert snap.health["prom"].stale is False


def test_a_successful_poll_publishes_data_and_bumps_the_version():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=12.5, now=NOW)

    snap = store.read()
    assert snap.data["prom"] == {"cpu": 42.0}
    assert snap.version == 1
    assert snap.health["prom"].state is Health.HEALTHY
    assert snap.health["prom"].latency_ms == 12.5
    assert snap.health["prom"].last_success == NOW


def test_read_returns_a_copy_a_caller_cannot_use_to_mutate_the_store():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)

    snap = store.read()
    snap.data["prom"]["cpu"] = 99.0
    assert store.read().data["prom"]["cpu"] == 42.0


def test_failures_accumulate_and_mark_stale_only_at_the_threshold():
    store = SnapshotStore(stale_after=3)
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)

    for expected_stale in (False, False, True):
        store.fail("prom", "timeout", now=NOW)
        health = store.read().health["prom"]
        assert health.state is Health.UNHEALTHY
        assert health.reason == "timeout"
        assert health.stale is expected_stale


def test_a_failure_leaves_the_last_good_data_in_place():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    version = store.read().version
    store.fail("prom", "timeout", now=NOW)

    snap = store.read()
    assert snap.data["prom"] == {"cpu": 42.0}
    assert snap.version == version, "a failure must not look like new data"


def test_recovery_clears_staleness_and_the_failure_count():
    store = SnapshotStore(stale_after=2)
    store.register("prom")
    store.fail("prom", "timeout", now=NOW)
    store.fail("prom", "timeout", now=NOW)
    assert store.read().health["prom"].stale is True

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    health = store.read().health["prom"]
    assert health.state is Health.HEALTHY
    assert health.stale is False
    assert health.consecutive_failures == 0


def test_wait_for_change_returns_immediately_when_already_behind():
    """The lost-wakeup shape: the version moved between a worker's read and its wait.

    The publish happens before anyone waits, so the notification it sent reached
    nobody. A store that answered only from notifications would freeze this
    panel until the next poll; the answer has to come from the version.
    """
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)

    assert store.wait_for_change(version=0, timeout=0.0) is True


def test_wait_for_change_wakes_on_a_publish_from_another_thread():
    store = SnapshotStore()
    store.register("prom")
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=5.0):
            woke.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    thread.join(timeout=5.0)

    assert woke.is_set()


def test_wait_for_change_times_out_when_nothing_happens():
    store = SnapshotStore()
    assert store.wait_for_change(version=0, timeout=0.01) is False


def test_a_failure_does_not_satisfy_a_worker_waiting_for_new_data():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    store.fail("prom", "timeout", now=NOW)

    assert store.wait_for_change(version=1, timeout=0.0) is False


def test_put_copies_the_fields_so_a_poller_may_reuse_its_dict():
    store = SnapshotStore()
    store.register("prom")
    fields = {"cpu_hot": {"node": ".5", "value": 71.2}}
    store.put("prom", fields, latency_ms=1.0, now=NOW)

    fields["cpu_hot"]["value"] = 0.0
    assert store.read().data["prom"]["cpu_hot"]["value"] == 71.2


def test_read_copies_nested_structures_all_the_way_down():
    """`reduce: top` publishes `{"cpu_hot": {"node": ".5", "value": 71.2}}`.

    One level of copying would hand every screen the same inner dict, and the
    first one to write through it would rewrite what the other three render.
    """
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu_hot": {"node": ".5", "value": 71.2}}, latency_ms=1.0, now=NOW)

    snap = store.read()
    snap.data["prom"]["cpu_hot"]["node"] = "corrupted"
    assert store.read().data["prom"]["cpu_hot"] == {"node": ".5", "value": 71.2}


def test_registering_an_integration_twice_keeps_the_health_it_has():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    store.register("prom")

    assert store.read().health["prom"].state is Health.HEALTHY


def test_a_reader_never_sees_the_data_and_the_version_disagree():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"n": 0}, latency_ms=1.0, now=NOW)
    reading, done = threading.Event(), threading.Event()
    torn: list[tuple[int, int]] = []
    reads = [0]

    def reader() -> None:
        while not done.is_set():
            snap = store.read()
            reads[0] += 1
            reading.set()
            # The n-th publish carries n-1, so any other pairing is a snapshot
            # assembled from two different versions of the store.
            if snap.data["prom"]["n"] != snap.version - 1:
                torn.append((snap.version, snap.data["prom"]["n"]))

    def writer() -> None:
        reading.wait(timeout=WAIT)  # overlap the reader rather than race it to the end
        for value in range(1, 201):
            store.put("prom", {"n": value}, latency_ms=1.0, now=NOW)
        done.set()

    threads = [threading.Thread(target=target, daemon=True) for target in (reader, writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT)

    assert torn == []
    assert reads[0] > 0, "the reader never ran, so this proved nothing"


def test_a_publish_wakes_a_worker_already_blocked_in_wait():
    store, condition = _watched_store()
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=WAIT):
            woke.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _await_parks(condition, 1)  # so this exercises the notification, not the version check

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    thread.join(timeout=WAIT)

    assert woke.is_set()


def test_one_publish_wakes_every_waiting_worker():
    store, condition = _watched_store()
    woke = [threading.Event() for _ in range(4)]  # one per panel

    def waiter(flag: threading.Event) -> None:
        if store.wait_for_change(version=0, timeout=WAIT):
            flag.set()

    threads = [threading.Thread(target=waiter, args=(flag,), daemon=True) for flag in woke]
    for thread in threads:
        thread.start()
    _await_parks(condition, len(threads))

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    for thread in threads:
        thread.join(timeout=WAIT)

    assert [flag.is_set() for flag in woke] == [True] * len(woke)


def test_a_wake_with_no_version_change_behind_it_does_not_end_the_wait():
    """A bare wake is not news, and a worker that treats it as news renders again.

    The docs are explicit that `wait()` "can return after an arbitrary long
    time, and the condition which prompted the notify() call may no longer hold
    true", so returning from one `wait()` proves nothing about the version. The
    wake is explicit here; on the Pi it can equally be the OS's own spurious one.
    """
    store, condition = _watched_store()
    outcome: list[bool] = []

    def waiter() -> None:
        outcome.append(store.wait_for_change(version=0, timeout=WAIT))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _await_parks(condition, 1)

    with condition:
        condition.notify_all()
    _await_parks(condition, 1)  # it must go back to waiting, not report a change

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    thread.join(timeout=WAIT)

    assert outcome == [True]


def test_an_integration_that_has_never_succeeded_stays_connecting_however_often_it_fails():
    store = SnapshotStore(stale_after=2)
    store.register("prom")

    for _ in range(5):
        store.fail("prom", "connection refused", now=NOW)
        assert store.read().health["prom"].state is Health.CONNECTING

    assert store.read().health["prom"].stale is True, "staleness is independent of the state"
    assert store.read().health["prom"].reason == "connection refused"


def test_a_failure_after_a_success_is_unhealthy_not_connecting():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    store.fail("prom", "timeout", now=NOW)

    assert store.read().health["prom"].state is Health.UNHEALTHY


def test_the_health_mapping_a_reader_gets_cannot_reach_back_into_the_store():
    store = SnapshotStore()
    store.register("prom")
    snap = store.read()
    snap.health.clear()

    assert "prom" in store.read().health
