import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.config import resolve_screens
from ors_daemon.displays import DisplayError
from ors_daemon.screen import _NIGHT_PARK_CHUNK as NIGHT_PARK_CHUNK
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import _MAX_RESTARTS as MAX_RESTARTS
from ors_daemon.supervisor import SHUTDOWN_BUDGET, Supervisor, _Panel, _Slot
from ors_schema.daemon import DaemonConfig, NightWindow, ScreenConfig
from PIL import Image

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 8, 11, 23, 30, tzinfo=UTC)
NIGHT_WINDOW = NightWindow(start="23:00", end="07:00")
UNTIL_MORNING = 7.5 * 3600
"""Seconds from `NIGHT` to the end of `NIGHT_WINDOW`."""
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""

LONG_AGO = -3600.0
"""A heartbeat no watchdog timeout can call fresh.

Relative to nothing, deliberately: `time.monotonic()` is seconds since boot on
Linux, so a test subtracting an hour from *it* would read as fresh on a machine
that booted ten minutes ago -- which is exactly what a CI runner is.
"""


class RecordingDisplay:
    """A panel that records what it was asked to do, and in what order."""

    def __init__(self) -> None:
        self.images: list[Image.Image] = []
        self.calls: list[str] = []
        self.sleeps = 0
        self.closed = 0
        self.on_sleep: Callable[[], None] | None = None

    def show(self, image: Image.Image) -> None:
        self.calls.append("show")
        self.images.append(image)

    def sleep(self) -> None:
        self.calls.append("sleep")
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep()

    def wake(self) -> None:
        self.calls.append("wake")

    def close(self) -> None:
        self.calls.append("close")
        self.closed += 1


class WedgingDisplay(RecordingDisplay):
    """A panel whose first write does not return, which is what a wedged worker is.

    Blocking inside `show` is the only way to hold a worker still: `heartbeat` is
    stamped at the top of `tick`, so a worker parked here has stamped exactly
    once and cannot stamp again -- which is what lets a test stale that heartbeat
    and know the value it wrote is the value the watchdog will read.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.released = threading.Event()

    def show(self, image: Image.Image) -> None:
        self.entered.set()
        self.released.wait(WAIT)
        super().show(image)


class RecordingPoller:
    """A poller that starts and joins without ever touching a network."""

    def __init__(self, config: Any, url_provider: Callable[[], str] | None) -> None:
        self.config = config
        self.url_provider = url_provider
        self.started = 0
        self.joined = 0

    def start(self) -> None:
        self.started += 1

    def join(self, timeout: float | None = None) -> None:
        self.joined += 1


class FakeTunnel:
    """A tunnel that opens nothing: no kubectl, no subprocess, no local port."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.base_url = f"http://localhost:{config.local_port}"
        self.started = 0
        self.shutdowns = 0
        self.joined = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self, timeout: float | None = None) -> None:
        self.shutdowns += 1

    def join(self, timeout: float | None = None) -> None:
        self.joined += 1


class FakeMonotonic:
    """A monotonic clock a test can spend a shutdown budget on without waiting.

    Started away from nought so that "how much has been spent" is a subtraction
    rather than a reading, the same distinction `LONG_AGO` draws.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Wedged:
    """A thread that never notices the stop event, and spends every second it is given.

    Which is what a wedged one does: a worker halfway through an SPI write and a
    `kubectl` stuck dialling the API server both sit there until whoever is
    joining them gives up. Burning exactly the timeout it was granted is what
    turns "the shutdown is bounded" into something a test can add up, without any
    of it being real time.
    """

    def __init__(self, clock: FakeMonotonic, name: str) -> None:
        self._clock = clock
        self.screen_name = name
        self.granted: list[float] = []

    def join(self, timeout: float | None = None) -> None:
        self._spend(timeout)

    def shutdown(self, timeout: float | None = None) -> None:
        # A tunnel's teardown is not a wait on a thread -- it signals `kubectl`
        # and waits for it to die -- so it spends the deadline too, and a
        # shutdown that only bounded the joins would not bound this.
        self._spend(timeout)

    def is_alive(self) -> bool:
        return True

    def _spend(self, timeout: float | None) -> None:
        assert timeout is not None, "a shutdown with no deadline is not a shutdown"
        self.granted.append(timeout)
        self._clock.advance(timeout)


def integration_dict(name: str = "prom", local_port: int | None = None) -> dict[str, Any]:
    integration: dict[str, Any] = {
        "name": name,
        "type": "prometheus",
        "url": "http://p:9090",
        "fields": {"cpu": {"query": "up"}},
    }
    if local_port is not None:
        integration["tunnel"] = {
            "kubeconfig": "/tmp/kc",
            "namespace": "monitoring",
            "remote_port": 9090,
            "local_port": local_port,
        }
    return integration


def config_dict(tmp_path: Path, screens: int = 2) -> dict[str, Any]:
    return {
        "version": 1,
        "timezone": "UTC",
        "night": {"enabled": False},
        "integrations": [integration_dict()],
        "screens": [
            {
                "name": f"S{n}",
                "position": n,
                "display": {"backend": "virtual", "out_dir": str(tmp_path / "panels")},
                "template": "ring-gauge",
                "params": {
                    "title": f"S{n}",
                    "value": "{{prom.cpu}}",
                    "big": "{{prom.cpu | round:0}}%",
                },
            }
            for n in range(1, screens + 1)
        ],
    }


def build(
    config: DaemonConfig,
    tmp_path: Path,
    display: RecordingDisplay | None = None,
    status_path: Path | None = None,
    poller_factory: Any = None,
    tunnel_factory: Any = None,
    shutdown_clock: Any = None,
) -> tuple[Supervisor, SnapshotStore, dict[str, RecordingDisplay]]:
    store = SnapshotStore()
    displays: dict[str, RecordingDisplay] = {}

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        displays[name] = display if display is not None else RecordingDisplay()
        return displays[name]

    extra: dict[str, Any] = {} if shutdown_clock is None else {"shutdown_clock": shutdown_clock}
    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=status_path if status_path is not None else tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=poller_factory or (lambda integration_config, url_provider: None),
        tunnel_factory=tunnel_factory,
        **extra,
    )
    return supervisor, store, displays


def make(
    tmp_path: Path,
    screens: int = 2,
    display: RecordingDisplay | None = None,
    status_path: Path | None = None,
    poller_factory: Any = None,
    shutdown_clock: Any = None,
) -> tuple[Supervisor, SnapshotStore, dict[str, RecordingDisplay]]:
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens))
    return build(
        config,
        tmp_path,
        display=display,
        status_path=status_path,
        poller_factory=poller_factory,
        shutdown_clock=shutdown_clock,
    )


def test_start_creates_one_worker_per_enabled_screen(tmp_path: Path) -> None:
    supervisor, _, displays = make(tmp_path, screens=3)
    supervisor.start()
    try:
        assert len(supervisor.workers) == 3
        assert set(displays) == {"S1", "S2", "S3"}
    finally:
        supervisor.stop()


def test_a_tick_writes_the_status_file(tmp_path: Path) -> None:
    supervisor, store, _ = make(tmp_path)
    store.register("prom")
    supervisor.start()
    try:
        supervisor.tick()
        payload = json.loads((tmp_path / "status.json").read_text())
        assert len(payload["screens"]) == 2
        assert payload["integrations"][0]["name"] == "prom"
    finally:
        supervisor.stop()


def test_the_watchdog_restarts_a_worker_whose_heartbeat_went_stale(tmp_path: Path) -> None:
    supervisor, _, _ = make(tmp_path, screens=1)
    supervisor.start()
    try:
        original = supervisor.workers[0]
        original.heartbeat = LONG_AGO
        supervisor.tick()
        assert supervisor.workers[0] is not original
        assert supervisor.workers[0].screen_name == "S1"
    finally:
        supervisor.stop()


def test_the_watchdog_leaves_a_worker_whose_heartbeat_is_moving_alone(tmp_path: Path) -> None:
    supervisor, _, _ = make(tmp_path, screens=1)
    supervisor.start()
    try:
        original = supervisor.workers[0]
        original.heartbeat = time.monotonic()
        supervisor.tick()
        assert supervisor.workers[0] is original
    finally:
        supervisor.stop()


def test_a_worker_that_has_not_stamped_a_heartbeat_yet_is_not_called_wedged(
    tmp_path: Path,
) -> None:
    """Nought is "has not ticked", not "has not ticked since the epoch".

    Without the distinction every worker is a minute-old corpse the instant it
    starts, because `time.monotonic()` is measured from boot and nought is
    therefore always further back than any timeout.
    """
    display = WedgingDisplay()
    supervisor, _, _ = make(tmp_path, screens=1, display=display)
    supervisor.start()
    try:
        assert display.entered.wait(WAIT), "the worker never reached the panel"
        original = supervisor.workers[0]
        original.heartbeat = 0.0
        supervisor.tick()
        assert supervisor.workers[0] is original
    finally:
        display.released.set()
        supervisor.stop()


def test_the_watchdog_stops_restarting_a_screen_that_keeps_wedging(tmp_path: Path) -> None:
    """A restart a timeout, forever, is a thread leak dressed as a recovery."""
    display = WedgingDisplay()
    supervisor, _, _ = make(tmp_path, screens=1, display=display)
    supervisor.start()
    try:
        seen = []
        for _ in range(MAX_RESTARTS + 1):
            assert display.entered.wait(WAIT), "the worker never reached the panel"
            display.entered.clear()
            worker = supervisor.workers[0]
            seen.append(worker)
            worker.heartbeat = LONG_AGO
            supervisor.tick()

        assert len({id(worker) for worker in seen}) == MAX_RESTARTS + 1
        # The cap has been reached, so this one is on its own from here.
        abandoned = supervisor.workers[0]
        abandoned.heartbeat = LONG_AGO
        supervisor.tick()
        assert supervisor.workers[0] is abandoned
    finally:
        display.released.set()
        supervisor.stop()


def test_a_restart_hands_the_replacement_the_panel_that_is_already_open(tmp_path: Path) -> None:
    """Not a second one: `build_display` on a GC9A01 re-runs the init sequence on
    a bus the wedged worker may be halfway through a write on."""
    supervisor, _, displays = make(tmp_path, screens=1)
    supervisor.start()
    try:
        original = supervisor.workers[0]
        panel = original._display
        original.heartbeat = LONG_AGO
        supervisor.tick()

        replacement = supervisor.workers[0]._display
        assert replacement is not panel
        assert replacement.backend is panel.backend is displays["S1"]
    finally:
        supervisor.stop()


def test_a_replaced_worker_can_no_longer_reach_its_panel(tmp_path: Path) -> None:
    """Two threads writing one SPI bus do not race for the last frame -- a `show`
    is several sequential commands, so interleaving corrupts both."""
    supervisor, _, _ = make(tmp_path, screens=1)
    supervisor.start()
    try:
        original = supervisor.workers[0]
        panel = original._display
        original.heartbeat = LONG_AGO
        supervisor.tick()

        assert panel.live is False
    finally:
        supervisor.stop()


def test_a_revoked_panel_forwards_nothing() -> None:
    display = RecordingDisplay()
    panel = _Panel(display)
    panel.revoke()

    panel.show(Image.new("RGB", (240, 240)))
    panel.sleep()
    panel.wake()
    panel.close()

    assert display.calls == []


def test_a_worker_closing_its_panel_ends_its_lease_rather_than_the_panel() -> None:
    """`ScreenWorker.run` closes its display on the way out, and it must not:
    the supervisor still has to put that panel to sleep afterwards, and on a
    GC9A01 a `close` has already torn the SPI device down."""
    display = RecordingDisplay()
    panel = _Panel(display)

    panel.close()

    assert display.closed == 0
    assert panel.live is False


def test_stop_sleeps_and_closes_every_panel(tmp_path: Path) -> None:
    supervisor, _, displays = make(tmp_path)
    supervisor.start()
    supervisor.stop()

    for display in displays.values():
        assert display.sleeps == 1
        assert display.closed == 1


def test_a_panel_is_slept_and_closed_only_once_its_worker_has_stopped(tmp_path: Path) -> None:
    """The ordering the whole shutdown exists for.

    A worker still turning when its backend is slept is two threads on one bus,
    and a `sleep` after a `close` is a command written to a device that has been
    torn down -- which on the rack is four panels left lit forever.
    """
    supervisor, _, displays = make(tmp_path)
    supervisor.start()
    still_running: list[str] = []
    for display in displays.values():
        display.on_sleep = lambda: still_running.extend(
            worker.name for worker in supervisor.workers if worker.is_alive()
        )

    supervisor.stop()

    assert still_running == []
    for display in displays.values():
        assert display.calls[-2:] == ["sleep", "close"]
        assert "show" not in display.calls[display.calls.index("sleep") :]


def test_stop_releases_a_worker_parked_on_the_snapshot(tmp_path: Path) -> None:
    """Otherwise shutdown costs a floor per worker, and the join can lose the race."""
    supervisor, store, _ = make(tmp_path)
    supervisor.start()
    supervisor.stop()

    assert store.wait_for_change(version=store.read().version, timeout=0.0) is True


def wedged_rack(
    tmp_path: Path, clock: FakeMonotonic, screens: int, tunnels: int = 1, pollers: int = 1
) -> tuple[Supervisor, list[RecordingDisplay]]:
    """A rack whose every thread is wedged, without one of them being real.

    The slots are assembled rather than started, because what is under test is
    the arithmetic of `stop` and not the threads: a real worker would leave the
    moment the stop event was set, which is the case that never had a problem.
    """
    supervisor, _, _ = make(tmp_path, screens=screens, shutdown_clock=clock)
    displays: list[RecordingDisplay] = []
    for screen in supervisor._screens:
        display = RecordingDisplay()
        displays.append(display)
        supervisor._slots.append(
            _Slot(
                screen=screen,
                panel=_Panel(display),
                worker=Wedged(clock, screen.config.name),  # type: ignore[arg-type]
            )
        )
    supervisor.pollers = [Wedged(clock, f"poller-{n}") for n in range(pollers)]  # type: ignore[misc]
    supervisor.tunnels = [Wedged(clock, f"tunnel-{n}") for n in range(tunnels)]  # type: ignore[misc]
    return supervisor, displays


@pytest.mark.parametrize("screens", [4, 16])
def test_a_wedged_rack_is_shut_down_inside_one_budget_however_many_panels_it_has(
    tmp_path: Path, screens: int
) -> None:
    """The bound the shipped unit file's `TimeoutStopSec` is derived from.

    Four wedged workers, a wedged poller and a wedged tunnel used to cost five
    seconds each and ten more for the tunnel's own SIGTERM-then-SIGKILL wait:
    ~40s against a `TimeoutStopSec=30`, past which systemd sends SIGKILL, `stop`
    never reaches the panels, and four GC9A01s stay lit until the rack is
    power-cycled. A per-thread timeout cannot be made safe by choosing a smaller
    number, because how many threads there are is a fact about the config -- so
    the deadline is shared, and sixteen panels cost exactly what four do.
    """
    clock = FakeMonotonic()
    supervisor, displays = wedged_rack(tmp_path, clock, screens=screens)
    started = clock.now

    supervisor.stop()

    assert clock.now - started <= SHUTDOWN_BUDGET
    for display in displays:
        assert display.calls == ["sleep", "close"], "and the panels are blanked regardless"


def test_a_second_stop_does_not_open_a_second_budget(tmp_path: Path) -> None:
    """One deadline per shutdown, not per call. The CLI installs `stop` as the
    SIGTERM handler *and* `run_forever` calls it from its `finally`, and an
    impatient operator sends the signal twice -- so a wedged poller nothing can
    join would otherwise cost its whole budget again on every call."""
    clock = FakeMonotonic()
    supervisor, _ = wedged_rack(tmp_path, clock, screens=4)
    started = clock.now

    supervisor.stop()
    supervisor.stop()
    supervisor.stop()

    assert clock.now - started <= SHUTDOWN_BUDGET


def test_a_wedged_thread_does_not_take_the_deadline_away_from_the_panels(
    tmp_path: Path,
) -> None:
    """The deadline covers the joins and the tunnel teardown; the blanking is
    what it is protecting, so it happens on the far side of the deadline
    expiring. A panel nobody comes back to is the outcome all of this exists to
    prevent, and it is worse than one late write on a bus about to lose power."""
    clock = FakeMonotonic()
    supervisor, displays = wedged_rack(tmp_path, clock, screens=4)

    supervisor.stop()

    granted = [
        timeout
        for thread in [*(slot.worker for slot in supervisor._slots), *supervisor.tunnels]
        for timeout in thread.granted  # type: ignore[union-attr]
    ]
    assert granted[0] == SHUTDOWN_BUDGET, "the first wedged thread may spend the lot"
    assert granted[-1] == 0.0, "and the last one is given nothing, rather than five more seconds"
    assert all(display.calls == ["sleep", "close"] for display in displays)


def test_stop_is_idempotent(tmp_path: Path) -> None:
    """The CLI's SIGTERM handler and `run_forever`'s own `finally` both call it."""
    supervisor, _, displays = make(tmp_path)
    supervisor.start()
    supervisor.stop()
    supervisor.stop()

    for display in displays.values():
        assert display.sleeps == 1
        assert display.closed == 1


def build_interrupted_by_stop(
    tmp_path: Path, on_screen: str, screens: int = 4
) -> tuple[Supervisor, dict[str, RecordingDisplay]]:
    """A supervisor whose `stop` lands while `start` is opening `on_screen`.

    Called from inside the display factory, so it runs on the very thread that
    is inside `start()` -- which is what a signal handler does, and what makes
    a re-entrant lock no protection at all.
    """
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=screens))
    displays: dict[str, RecordingDisplay] = {}
    holder: list[Supervisor] = []

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        displays[name] = RecordingDisplay()
        if name == on_screen:
            holder[0].stop()
        return displays[name]

    holder.append(
        Supervisor(
            config=config,
            screens=resolve_screens(config),
            store=SnapshotStore(),
            clock=FakeClock(NOW),
            status_path=tmp_path / "status.json",
            display_factory=display_factory,
            poller_factory=lambda integration_config, url_provider: None,
        )
    )
    return holder[0], displays


def test_a_stop_landing_mid_start_still_sleeps_every_panel_that_was_opened(
    tmp_path: Path,
) -> None:
    """The one thing shutdown exists to do, in the one window it used to miss.

    `stop` walks the slots recorded so far, and `start` then carries on opening
    backends behind it: every panel opened after that point had no slot, so it
    was never slept and never closed -- lit glass and an open serial device,
    for as long as the process lived. The slot is therefore recorded *before*
    the stopped flag is read, and `stop` shuts down every slot it has not
    already claimed rather than returning early.
    """
    supervisor, displays = build_interrupted_by_stop(tmp_path, on_screen="S2")

    supervisor.start()

    assert displays, "the test proves nothing if no panel was ever opened"
    for name, display in displays.items():
        assert (display.sleeps, display.closed) == (1, 1), name
        assert display.calls[-2:] == ["sleep", "close"], name


def test_a_stop_landing_mid_start_stops_opening_the_rest_of_the_rack(tmp_path: Path) -> None:
    """Opening a panel takes a hardware reset and a fifty-command init sequence.

    Doing that three more times, to close all three immediately, is a slower
    shutdown for nothing -- and three more chances for an SPI error on the way
    out of a daemon that is already leaving.
    """
    supervisor, displays = build_interrupted_by_stop(tmp_path, on_screen="S2")

    supervisor.start()

    assert sorted(displays) == ["S1", "S2"]


def test_a_stop_landing_mid_start_is_not_confused_by_a_second_one(tmp_path: Path) -> None:
    """An impatient operator, or systemd's SIGTERM followed by its SIGKILL
    timer being beaten by a second `systemctl stop`. Each panel is slept once,
    whichever call reaches it."""
    supervisor, displays = build_interrupted_by_stop(tmp_path, on_screen="S2")

    supervisor.start()
    supervisor.stop()
    supervisor.stop()

    for name, display in displays.items():
        assert (display.sleeps, display.closed) == (1, 1), name


def test_stop_without_a_start_does_nothing_and_says_nothing(tmp_path: Path) -> None:
    supervisor, _, displays = make(tmp_path)
    supervisor.stop()

    assert displays == {}


def test_a_screen_whose_backend_cannot_be_built_does_not_stop_the_others(tmp_path: Path) -> None:
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=2))
    store = SnapshotStore()

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        if name == "S1":
            raise RuntimeError("no such SPI bus")
        return RecordingDisplay()

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    supervisor.start()
    try:
        assert [worker.name for worker in supervisor.workers] == ["screen-S2"]
    finally:
        supervisor.stop()


def test_a_panel_whose_worker_will_not_start_is_blanked_on_the_spot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same class of bug as a signal landing mid-start, one line further along.

    Between the backend opening and the slot recording it there is a panel
    `stop` cannot reach, because `stop` walks the slots. A Pi too short of
    memory to fork answers `worker.start()` with `RuntimeError("can't start new
    thread")`, and this panel -- whose init sequence has just ended in
    DISPLAY_ON -- was left lit, with its serial device open, for as long as the
    process lived.
    """
    supervisor, _, displays = make(tmp_path, screens=3)
    started = ScreenWorker.start

    def start(self: ScreenWorker) -> None:
        if self.screen_name == "S2":
            raise RuntimeError("can't start new thread")
        started(self)

    monkeypatch.setattr(ScreenWorker, "start", start)

    with pytest.raises(RuntimeError, match="can't start new thread"):
        supervisor.run_forever(interval=0.0)

    assert displays["S2"].calls == ["sleep", "close"], "the panel it had just opened"
    assert displays["S1"].calls[-2:] == ["sleep", "close"], "and the one before it"
    # Every panel is opened before any worker draws, so S3 is already open when
    # S2's worker fails. It is in a slot, which is what makes it reachable: the
    # shutdown blanks it too, and none of the three is slept twice.
    assert displays["S3"].calls == ["sleep", "close"], "and the one after it"
    for name, display in displays.items():
        assert display.calls.count("close") == 1, f"{name} was closed more than once"


def test_a_tick_survives_a_status_path_it_cannot_write(tmp_path: Path) -> None:
    """A read-only /run or a full disk must not darken the rack."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    supervisor, _, _ = make(tmp_path, screens=1, status_path=blocked / "status.json")
    supervisor.start()
    try:
        supervisor.tick()
    finally:
        supervisor.stop()


@pytest.mark.parametrize("hostile", ["/", ".", ""])
def test_a_status_path_with_no_filename_does_not_escape_a_tick(
    tmp_path: Path, hostile: str
) -> None:
    """`--status /` used to be an infinite restart loop, not a warning.

    A path with no filename has no name to derive the temporary file from, and
    that raises `ValueError` rather than `OSError` -- so it went straight past
    the guard here, out of `run_forever` and out of `main`. Under the shipped
    unit's `Restart=always` and `StartLimitIntervalSec=0` that is a crash every
    five seconds, each one re-running the GC9A01 init sequence on four panels,
    for as long as the typo survives.
    """
    supervisor, _, displays = make(tmp_path, screens=1, status_path=Path(hostile))
    supervisor.start()
    try:
        supervisor.tick()
    finally:
        supervisor.stop()

    assert displays["S1"].calls[-2:] == ["sleep", "close"], "the rack ran, and shut down cleanly"


def test_every_integration_gets_a_poller_that_is_started_and_joined(tmp_path: Path) -> None:
    pollers: list[RecordingPoller] = []

    def poller_factory(config: Any, url_provider: Any) -> RecordingPoller:
        pollers.append(RecordingPoller(config, url_provider))
        return pollers[-1]

    supervisor, store, _ = make(tmp_path, screens=1, poller_factory=poller_factory)
    supervisor.start()
    try:
        assert [poller.config.name for poller in supervisor.pollers] == ["prom"]
        assert pollers[0].started == 1
        assert pollers[0].url_provider is None
        assert store.read().health["prom"].state.value == "connecting"
    finally:
        supervisor.stop()

    assert pollers[0].joined == 1


def test_a_tunnelled_integration_gets_a_tunnel(tmp_path: Path) -> None:
    raw = config_dict(tmp_path)
    raw["integrations"] = [integration_dict(local_port=19090)]
    config = DaemonConfig.model_validate(raw)
    tunnels: list[FakeTunnel] = []

    def tunnel_factory(tunnel_config: Any, stop: threading.Event) -> FakeTunnel:
        tunnels.append(FakeTunnel(tunnel_config))
        return tunnels[-1]

    supervisor, _, _ = build(config, tmp_path, tunnel_factory=tunnel_factory)
    supervisor.start()
    try:
        assert len(supervisor.tunnels) == 1
        assert tunnels[0].started == 1
    finally:
        supervisor.stop()

    assert tunnels[0].shutdowns == 1
    assert tunnels[0].joined == 1


def test_each_tunnelled_integration_polls_through_its_own_tunnel(tmp_path: Path) -> None:
    """A closure over a loop variable would give every integration the last tunnel."""
    raw = config_dict(tmp_path)
    raw["integrations"] = [
        integration_dict("prom", local_port=19090),
        integration_dict("other", local_port=19091),
    ]
    config = DaemonConfig.model_validate(raw)
    pollers: list[RecordingPoller] = []

    def poller_factory(integration_config: Any, url_provider: Any) -> RecordingPoller:
        pollers.append(RecordingPoller(integration_config, url_provider))
        return pollers[-1]

    supervisor, _, _ = build(
        config,
        tmp_path,
        poller_factory=poller_factory,
        tunnel_factory=lambda tunnel_config, stop: FakeTunnel(tunnel_config),
    )
    supervisor.start()
    try:
        assert len(supervisor.tunnels) == 2
        assert [poller.url_provider() for poller in pollers] == [  # type: ignore[misc]
            "http://localhost:19090",
            "http://localhost:19091",
        ]
    finally:
        supervisor.stop()


def test_the_url_provider_reads_the_tunnels_url_when_it_is_asked(tmp_path: Path) -> None:
    """At call time, not at start time: a tunnel can move underneath a poller."""
    raw = config_dict(tmp_path)
    raw["integrations"] = [integration_dict(local_port=19090)]
    config = DaemonConfig.model_validate(raw)
    pollers: list[RecordingPoller] = []

    def poller_factory(integration_config: Any, url_provider: Any) -> RecordingPoller:
        pollers.append(RecordingPoller(integration_config, url_provider))
        return pollers[-1]

    supervisor, _, _ = build(
        config,
        tmp_path,
        poller_factory=poller_factory,
        tunnel_factory=lambda tunnel_config, stop: FakeTunnel(tunnel_config),
    )
    supervisor.start()
    try:
        supervisor.tunnels[0].base_url = "http://localhost:29090"
        assert pollers[0].url_provider() == "http://localhost:29090"  # type: ignore[misc]
    finally:
        supervisor.stop()


def test_a_watchdog_timeout_a_sleeping_panel_would_trip_is_refused(tmp_path: Path) -> None:
    """A sleeping worker parks in bounded chunks so its heartbeat keeps moving.
    A timeout at or below that chunk restarts every panel, all night, on schedule.
    """
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=1))
    with pytest.raises(ValueError, match="watchdog"):
        Supervisor(
            config=config,
            screens=resolve_screens(config),
            store=SnapshotStore(),
            clock=FakeClock(NOW),
            status_path=tmp_path / "status.json",
            display_factory=lambda screen_config, name: RecordingDisplay(),
            poller_factory=lambda integration_config, url_provider: None,
            watchdog_timeout=NIGHT_PARK_CHUNK,
        )


def test_run_forever_starts_ticks_and_stops(tmp_path: Path) -> None:
    supervisor, _, displays = make(tmp_path, screens=1)
    ticks = []
    tick = supervisor.tick

    def tick_once() -> None:
        ticks.append(len(supervisor.workers))
        tick()
        supervisor._stop_event.set()

    supervisor.tick = tick_once  # type: ignore[method-assign]
    supervisor.run_forever(interval=0.0)

    assert ticks == [1], "started before the first tick, and stopped after it"
    assert (tmp_path / "status.json").exists()
    for display in displays.values():
        assert display.sleeps == 1
        assert display.closed == 1


def test_a_dead_panel_is_reported_rather_than_omitted(tmp_path: Path) -> None:
    """Four screens with two dead panels must not read as a healthy two-screen rack.

    The startup ERROR line is written once and never repeated, and on a headless
    rack this file is the diagnostic -- and the one M3 forwards verbatim.
    """
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=4))
    store = SnapshotStore()

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        if name in {"S1", "S3"}:
            raise DisplayError(f"cannot open SPI for {name}")
        return RecordingDisplay()

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    supervisor.start()
    try:
        supervisor.tick()
    finally:
        supervisor.stop()

    reported = json.loads((tmp_path / "status.json").read_text())["screens"]
    by_name = {screen["name"]: screen for screen in reported}

    assert set(by_name) == {"S1", "S2", "S3", "S4"}, "every configured screen is reported"
    assert by_name["S1"]["state"] == "unavailable"
    assert "cannot open SPI" in by_name["S1"]["error"]
    assert by_name["S2"]["state"] == "awake"
    assert by_name["S2"]["error"] is None


def test_a_start_that_fails_partway_still_shuts_down_what_it_opened(tmp_path: Path) -> None:
    """A panel left lit, or a kubectl child orphaned, is the cost of getting this wrong.

    The failure is in building a poller, which nothing guards, and it lands
    after the tunnel has already been started -- so a `start` outside the
    `try` leaves a `kubectl port-forward` child holding its local port until
    the process exits.
    """
    raw = config_dict(tmp_path, screens=1)
    raw["integrations"][0]["tunnel"] = {
        "kubeconfig": "/tmp/kubeconfig",
        "namespace": "monitoring",
        "remote_port": 9090,
        "local_port": 19090,
    }
    config = DaemonConfig.model_validate(raw)
    tunnels: list[FakeTunnel] = []

    def poller_factory(integration_config: Any, url_provider: Any) -> Any:
        raise RuntimeError("can't start new thread")

    def tunnel_factory(tunnel_config: Any, stop: threading.Event) -> FakeTunnel:
        tunnels.append(FakeTunnel(tunnel_config))
        return tunnels[-1]

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=SnapshotStore(),
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=lambda screen_config, name: RecordingDisplay(),
        poller_factory=poller_factory,
        tunnel_factory=tunnel_factory,
    )

    with pytest.raises(RuntimeError, match="can't start new thread"):
        supervisor.run_forever(interval=0.0)

    assert [tunnel.shutdowns for tunnel in tunnels] == [1], (
        "a started tunnel must not outlive a failed start, or kubectl is orphaned"
    )
    assert supervisor.workers == [], "the failure landed before any screen started"


def test_a_tick_after_stop_does_nothing(tmp_path: Path) -> None:
    """SIGTERM can land mid-tick; the resumed tick must not touch a closed rack."""
    supervisor, _, displays = make(tmp_path, screens=1)
    supervisor.start()
    supervisor.stop()
    status = tmp_path / "status.json"
    status.unlink(missing_ok=True)

    supervisor.tick()

    assert not status.exists(), "a stopped supervisor writes no status"
    assert displays["S1"].sleeps == 1, "and does not re-sleep the panel"


def test_no_worker_starts_until_every_panel_is_initialised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Panels share buses, so an early worker corrupts a late panel's init.

    A Pi 3 carries two chip selects on SPI0 and two on SPI1. Opening a GC9A01
    is a hardware reset and a fifty-command init sequence over that shared
    wire, so a worker that begins drawing while its bus-mate is still
    initialising tramples it: the second panel comes up showing unconfigured
    RAM -- a pale rectangle -- and which panel loses depends on the order the
    supervisor started them in. Observed on the rack, on a config whose only
    change was which screen held which SPI device, and it came good on some
    restarts and not others, which is what a race looks like from outside.

    Asserted on the *starts* rather than on the draws: a started worker draws
    when the scheduler says so, so an assertion about drawing would pass
    against the interleaved version roughly whenever the timing was kind. The
    property that actually matters -- nothing is started until everything is
    open -- is deterministic, and the script this replaces had it: "Init
    displays one at a time (GPIO race prevention)".
    """
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=4))
    events: list[str] = []
    started = ScreenWorker.start

    def start(self: ScreenWorker) -> None:
        events.append(f"start:{self.screen_name}")
        started(self)

    monkeypatch.setattr(ScreenWorker, "start", start)

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        events.append(f"open:{name}")
        return RecordingDisplay()

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=SnapshotStore(),
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    supervisor.start()
    try:
        opens = [i for i, e in enumerate(events) if e.startswith("open:")]
        starts = [i for i, e in enumerate(events) if e.startswith("start:")]

        assert len(opens) == 4 and len(starts) == 4
        assert max(opens) < min(starts), f"a worker started mid-bring-up: {events}"
    finally:
        supervisor.stop()
