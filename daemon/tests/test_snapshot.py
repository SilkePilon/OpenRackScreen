import threading
from datetime import UTC, datetime

from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


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
