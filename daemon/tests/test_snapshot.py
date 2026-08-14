import threading
from datetime import UTC, datetime

from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""


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


def test_close_releases_a_waiter_from_another_thread():
    """Shutdown's release. A screen worker spends nearly all of its life parked
    here, and nothing else can cut that wait short: the stop event it is holding
    is not what it is waiting on."""
    store = SnapshotStore()
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=5.0):
            woke.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    store.close()
    thread.join(timeout=5.0)

    assert woke.is_set()


def test_close_stays_closed_so_a_release_cannot_be_missed():
    """Sticky on purpose, and it is the whole reason this is not a notify.

    A one-shot wake races the waiter it is meant to release: a worker that had
    checked its stop event and not yet reached the condition would park for a
    full floor afterwards, which is the delay this exists to remove.
    """
    store = SnapshotStore()
    store.close()

    assert store.wait_for_change(version=0, timeout=0.0) is True
    assert store.wait_for_change(version=0, timeout=0.0) is True


def test_a_store_says_whether_it_has_been_closed():
    """Public because `wait_for_change` alone cannot be looped on: it answers
    True for a closed store immediately and forever, so a caller that treats
    that as news busy-waits. This is the flag such a caller reads instead."""
    store = SnapshotStore()
    assert store.closed is False

    store.close()
    assert store.closed is True


def test_close_leaves_the_store_readable_and_writable():
    """It ends the waiting, not the data: a status write outlives the threads."""
    store = SnapshotStore()
    store.register("prom")
    store.close()
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)

    assert store.read().data == {"prom": {"cpu": 1.0}}


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


def test_a_publish_wakes_a_worker_already_blocked_in_wait(watched_store):
    store, condition = watched_store()
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=WAIT):
            woke.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    condition.await_parks(1)  # so this exercises the notification, not the version check

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    thread.join(timeout=WAIT)

    assert woke.is_set()


def test_one_publish_wakes_every_waiting_worker(watched_store):
    store, condition = watched_store()
    woke = [threading.Event() for _ in range(4)]  # one per panel

    def waiter(flag: threading.Event) -> None:
        if store.wait_for_change(version=0, timeout=WAIT):
            flag.set()

    threads = [threading.Thread(target=waiter, args=(flag,), daemon=True) for flag in woke]
    for thread in threads:
        thread.start()
    condition.await_parks(len(threads))

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    for thread in threads:
        thread.join(timeout=WAIT)

    assert [flag.is_set() for flag in woke] == [True] * len(woke)


def test_a_wake_with_no_version_change_behind_it_does_not_end_the_wait(watched_store):
    """A bare wake is not news, and a worker that treats it as news renders again.

    The docs are explicit that `wait()` "can return after an arbitrary long
    time, and the condition which prompted the notify() call may no longer hold
    true", so returning from one `wait()` proves nothing about the version. The
    wake is explicit here; on the Pi it can equally be the OS's own spurious one.
    """
    store, condition = watched_store()
    outcome: list[bool] = []

    def waiter() -> None:
        outcome.append(store.wait_for_change(version=0, timeout=WAIT))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    condition.await_parks(1)

    with condition:
        condition.notify_all()
    condition.await_parks(1)  # it must go back to waiting, not report a change

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


# --- the stop event a worker parks on, and the wake that re-tests it ---------
#
# A screen worker's ordinary wait is `wait_for_change`, and its slot's stop
# event is what retires it. Until these existed the two had nothing to do with
# each other: `Supervisor._retire` set the event and then joined against a
# worker that was not watching it, so an apply on a rack whose integrations are
# quiet spent whatever was left of the worker's five-second floor -- measured at
# 2.008s, 3.003s (overrun) and 1.019s on three consecutive one-screen applies.


def test_a_waiter_whose_stop_event_is_already_set_does_not_park_at_all():
    """The sticky half. `close` is sticky for the same reason and says why:
    a wake that arrives before the waiter reaches the condition is missed, and a
    flag that stays set is a fact rather than an event."""
    store = SnapshotStore()
    stop = threading.Event()
    stop.set()

    assert store.wait_for_change(version=0, timeout=WAIT, stop=stop) is True


def test_a_wake_releases_a_waiter_whose_stop_event_was_set_while_it_slept(watched_store):
    """The notifying half, and the one the supervisor needs.

    Setting a `threading.Event` notifies nothing about this store, so without a
    wake behind it the flag above is only read when the wait times out -- which
    is a whole heartbeat floor, and is the defect these two exist to close.
    """
    store, condition = watched_store()
    stop = threading.Event()
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=WAIT, stop=stop):
            woke.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    condition.await_parks(1)  # so this exercises the wake, not the flag check

    stop.set()
    store.wake()
    thread.join(timeout=WAIT)

    assert woke.is_set(), "the worker sat out its floor with its stop event set"


def test_a_wake_with_nothing_behind_it_does_not_end_a_wait(watched_store):
    """`wake` is a re-test, not news. A worker released by one that changed
    nothing would redraw a panel for no reason, once per retirement, per rack."""
    store, condition = watched_store()
    stop = threading.Event()
    outcome: list[bool] = []

    def waiter() -> None:
        outcome.append(store.wait_for_change(version=0, timeout=0.2, stop=stop))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    condition.await_parks(1)

    store.wake()
    thread.join(timeout=WAIT)

    assert outcome == [False]


def test_a_waiter_that_passes_no_stop_event_is_unaffected():
    """The parameter is optional, and a caller that omits it gets M2's wait."""
    store = SnapshotStore()

    assert store.wait_for_change(version=0, timeout=0.01) is False


def test_one_wake_releases_every_worker_whose_slot_is_retiring(watched_store):
    """An apply retires several screens at once and wakes the rack once."""
    store, condition = watched_store()
    stops = [threading.Event() for _ in range(4)]  # one per panel
    woke = [threading.Event() for _ in range(4)]

    def waiter(stop: threading.Event, flag: threading.Event) -> None:
        if store.wait_for_change(version=0, timeout=WAIT, stop=stop):
            flag.set()

    threads = [
        threading.Thread(target=waiter, args=pair, daemon=True)
        for pair in zip(stops, woke, strict=True)
    ]
    for thread in threads:
        thread.start()
    condition.await_parks(len(threads))

    for stop in stops:
        stop.set()
    store.wake()
    for thread in threads:
        thread.join(timeout=WAIT)

    assert [flag.is_set() for flag in woke] == [True] * len(woke)
