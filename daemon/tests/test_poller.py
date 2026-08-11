import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from ors_daemon.integrations import IntegrationError
from ors_daemon.poller import Poller
from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""


class FakeIntegration:
    """A pure fetcher. It raises; it has no opinion about what that means."""

    def __init__(
        self,
        name: str = "prom",
        results: list[Any] | None = None,
        open_results: list[Any] | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.results = list(results or [])
        self.open_results = list(open_results or [])
        self.close_error = close_error
        self.opened = 0
        self.closed = 0
        self.polls = 0
        self.on_poll: Callable[[], None] | None = None

    def open(self) -> None:
        self.opened += 1
        outcome = self.open_results.pop(0) if self.open_results else None
        if isinstance(outcome, Exception):
            raise outcome

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    def poll(self) -> dict[str, Any]:
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll()
        outcome = self.results.pop(0) if self.results else {"cpu": 1.0}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(integration: FakeIntegration, **kwargs: Any) -> tuple[Poller, SnapshotStore]:
    store = kwargs.pop("store", None) or SnapshotStore(stale_after=kwargs.pop("stale_after", 3))
    store.register(integration.name)
    poller = Poller(
        integration=integration,
        store=store,
        interval=kwargs.pop("interval", 5.0),
        stop=kwargs.pop("stop", threading.Event()),
        clock=lambda: NOW,
        **kwargs,
    )
    return poller, store


@contextmanager
def escaped_thread_errors() -> Iterator[list[BaseException | None]]:
    """Collects anything that escapes a thread's `run`, which otherwise only prints.

    A poller is a daemon thread driving a rack: an exception leaving `run` ends
    that integration until the process restarts, and nothing else notices --
    the supervisor's watchdog reads heartbeats, not thread liveness. So the
    tests that matter here assert on emptiness of this list, not on `is_alive`,
    which is False for a crashed thread and a finished one alike.
    """
    escaped: list[BaseException | None] = []
    original = threading.excepthook
    threading.excepthook = lambda args: escaped.append(args.exc_value)
    try:
        yield escaped
    finally:
        threading.excepthook = original


def test_a_successful_cycle_publishes_and_stays_on_the_configured_interval() -> None:
    poller, store = make(FakeIntegration(results=[{"cpu": 42.0}]))
    poller.poll_once()

    assert store.read().data["prom"] == {"cpu": 42.0}
    assert store.read().health["prom"].state is Health.HEALTHY
    assert poller.next_delay == 5.0


def test_a_failed_cycle_records_the_reason_without_publishing() -> None:
    """A cold start that fails is still *connecting*, per the store's own semantics.

    The brief asserted UNHEALTHY here, which no poller can produce: `fail`
    decides the state from history, and an integration that has never succeeded
    has nothing to be unhealthy about yet. The transition is the next test.
    """
    poller, store = make(FakeIntegration(results=[IntegrationError("timeout")]))
    poller.poll_once()

    snap = store.read()
    assert snap.data == {}
    assert snap.health["prom"].state is Health.CONNECTING
    assert snap.health["prom"].reason == "timeout"


def test_a_failure_after_a_success_is_unhealthy() -> None:
    poller, store = make(FakeIntegration(results=[{"cpu": 1.0}, IntegrationError("timeout")]))
    poller.poll_once()
    poller.poll_once()

    snap = store.read()
    assert snap.health["prom"].state is Health.UNHEALTHY
    assert snap.health["prom"].reason == "timeout"
    assert snap.data["prom"] == {"cpu": 1.0}, "a failure must not drop the last good data"


def test_an_unexpected_exception_is_treated_as_a_failure_not_a_crash() -> None:
    poller, store = make(FakeIntegration(results=[{"cpu": 1.0}, RuntimeError("boom")]))
    poller.poll_once()
    poller.poll_once()

    health = store.read().health["prom"]
    assert health.state is Health.UNHEALTHY
    assert "boom" in (health.reason or "")


def test_an_open_that_fails_is_a_failed_cycle_and_is_retried() -> None:
    """`open` may raise, and that means unhealthy-and-retry, not spent."""
    integration = FakeIntegration(open_results=[IntegrationError("login refused")])
    poller, store = make(integration)

    poller.poll_once()
    assert integration.polls == 0
    assert store.read().health["prom"].reason == "login refused"
    assert poller.next_delay == 5.0

    poller.poll_once()
    assert integration.opened == 2, "an open failure does not retire the integration"
    assert store.read().health["prom"].state is Health.HEALTHY


def test_backoff_doubles_on_repeated_failure_and_is_capped() -> None:
    poller, _ = make(
        FakeIntegration(results=[IntegrationError("x")] * 6), interval=5.0, backoff_cap=30.0
    )
    delays = []
    for _ in range(6):
        poller.poll_once()
        delays.append(poller.next_delay)

    assert delays == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]


def test_a_success_resets_the_backoff() -> None:
    poller, _ = make(
        FakeIntegration(results=[IntegrationError("x"), IntegrationError("x"), {"cpu": 1.0}])
    )
    poller.poll_once()
    poller.poll_once()
    poller.poll_once()

    assert poller.next_delay == 5.0


def test_the_backoff_restarts_at_the_interval_after_a_recovery() -> None:
    """The first retry after any success is prompt, whatever the last outage cost."""
    outage = [IntegrationError("x")] * 4
    poller, _ = make(
        FakeIntegration(results=[*outage, {"cpu": 1.0}, IntegrationError("x")]),
        interval=5.0,
        backoff_cap=30.0,
    )
    for _ in range(6):
        poller.poll_once()

    assert poller.next_delay == 5.0


def test_staleness_arrives_at_the_configured_threshold() -> None:
    poller, store = make(FakeIntegration(results=[IntegrationError("x")] * 3), stale_after=3)
    for _ in range(2):
        poller.poll_once()
    assert store.read().health["prom"].stale is False

    poller.poll_once()
    assert store.read().health["prom"].stale is True


def test_the_heartbeat_is_stamped_before_the_poll_not_after() -> None:
    """A poll that legitimately takes 30 s must not read as a wedged thread.

    Stamping on entry gives every cycle a full watchdog window of its own
    instead of charging it for the wait it just came out of; a thread genuinely
    stuck inside `poll` still stops advancing it and goes stale on time.
    """
    integration = FakeIntegration()
    poller, _ = make(integration)
    seen: list[float] = []
    integration.on_poll = lambda: seen.append(poller.heartbeat)

    poller.poll_once()

    assert seen and seen[0] > 0.0, "the heartbeat was not stamped before poll() was called"
    assert seen[0] == poller.heartbeat


def test_the_run_loop_polls_until_stopped_and_closes_the_integration() -> None:
    integration = FakeIntegration()
    stop = threading.Event()
    polled_twice = threading.Event()

    def sleeper(seconds: float) -> None:
        if integration.polls >= 2:
            polled_twice.set()
            stop.set()

    poller, _ = make(integration, interval=0.0, stop=stop, sleeper=sleeper)
    poller.start()
    polled_twice.wait(timeout=WAIT)
    poller.join(timeout=WAIT)

    assert integration.polls >= 2
    assert integration.closed == 1
    assert poller.heartbeat > 0


def test_a_stop_cuts_a_long_backoff_short() -> None:
    """The default sleeper waits on the stop event, so SIGTERM does not feel broken.

    With `time.sleep` in there this thread would sit out a full minute and the
    join below would time out; nothing here spends any wall clock when it is
    the event being waited on.
    """
    integration = FakeIntegration()
    polled = threading.Event()
    integration.on_poll = polled.set
    stop = threading.Event()
    poller, _ = make(integration, interval=60.0, stop=stop)

    poller.start()
    assert polled.wait(timeout=WAIT), "the poller never polled"
    stop.set()
    poller.join(timeout=WAIT)

    assert not poller.is_alive(), "a stop must end the wait, not wait the interval out"


def test_the_loop_survives_a_store_that_raises() -> None:
    class BrokenStore(SnapshotStore):
        def put(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the snapshot is on fire")

    integration = FakeIntegration()
    stop = threading.Event()
    cycles = threading.Event()

    def sleeper(seconds: float) -> None:
        if integration.polls >= 2:
            cycles.set()
            stop.set()

    poller, _ = make(integration, store=BrokenStore(), interval=0.0, stop=stop, sleeper=sleeper)

    with escaped_thread_errors() as escaped:
        poller.start()
        cycles.wait(timeout=WAIT)
        poller.join(timeout=WAIT)

    assert escaped == [], "a broken store must not take the poller down with it"
    assert integration.polls >= 2
    assert integration.closed == 1


def test_a_close_that_raises_on_the_way_out_does_not_escape_the_thread() -> None:
    integration = FakeIntegration(close_error=RuntimeError("the socket is already gone"))
    stop = threading.Event()
    polled = threading.Event()

    def sleeper(seconds: float) -> None:
        polled.set()
        stop.set()

    poller, _ = make(integration, interval=0.0, stop=stop, sleeper=sleeper)

    with escaped_thread_errors() as escaped:
        poller.start()
        polled.wait(timeout=WAIT)
        poller.join(timeout=WAIT)

    assert escaped == []
    assert integration.closed == 1


def test_an_open_that_never_succeeds_does_not_end_the_thread() -> None:
    integration = FakeIntegration(open_results=[IntegrationError("login refused")] * 3)
    stop = threading.Event()
    cycles = threading.Event()

    def sleeper(seconds: float) -> None:
        if integration.opened >= 2:
            cycles.set()
            stop.set()

    poller, store = make(integration, interval=0.0, stop=stop, sleeper=sleeper)

    with escaped_thread_errors() as escaped:
        poller.start()
        cycles.wait(timeout=WAIT)
        poller.join(timeout=WAIT)

    assert escaped == []
    assert integration.opened >= 2, "a failing open must be retried, not fatal"
    assert store.read().health["prom"].state is Health.CONNECTING
