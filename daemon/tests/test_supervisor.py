import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.config import ConfigError, resolve_screens, system_scenes
from ors_daemon.displays import DisplayError
from ors_daemon.frames import MAX_FPS, FrameStream
from ors_daemon.screen import _NIGHT_PARK_CHUNK as NIGHT_PARK_CHUNK
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import _BUS_GUARD_PER_SCREEN as BUS_GUARD_PER_SCREEN
from ors_daemon.supervisor import _MAX_RESTARTS as MAX_RESTARTS
from ors_daemon.supervisor import (
    APPLY_BUDGET,
    BUS_GUARD_BUDGET,
    IDENTIFY_BUDGET,
    SHUTDOWN_BUDGET,
    SOURCE_TEARDOWN_BUDGET,
    Supervisor,
    _Panel,
    _Reaper,
    _Slot,
    _Source,
)
from ors_render import RenderContext, render_scene
from ors_schema.daemon import (
    DaemonConfig,
    IntegrationConfig,
    NightWindow,
    ScreenConfig,
    TunnelConfig,
)
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
        self.on_show: Callable[[], None] | None = None
        """Fired once a frame has reached the glass, for a test that has to wait
        for one. `on_sleep` cannot stand in: it fires on the way down, by which
        time whatever was being observed has already happened or has not."""

    def show(self, image: Image.Image) -> None:
        self.calls.append("show")
        self.images.append(image)
        if self.on_show is not None:
            self.on_show()

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
        self.stop: threading.Event | None = None

    def start(self) -> None:
        self.started += 1

    def join(self, timeout: float | None = None) -> None:
        self.joined += 1

    def is_alive(self) -> bool:
        return False


class FakeTunnel:
    """A tunnel that opens nothing: no kubectl, no subprocess, no local port."""

    def __init__(self, config: Any, stop: threading.Event | None = None) -> None:
        self.config = config
        self.stop = stop
        self.base_url = f"http://localhost:{config.local_port}"
        self.started = 0
        self.shutdowns = 0
        self.joined = 0
        self.shutdown_timeouts: list[float | None] = []
        self.on_shutdown: Callable[[], None] | None = None

    def start(self) -> None:
        self.started += 1

    def shutdown(self, timeout: float | None = None) -> None:
        self.shutdowns += 1
        self.shutdown_timeouts.append(timeout)
        if self.on_shutdown is not None:
            self.on_shutdown()

    def join(self, timeout: float | None = None) -> None:
        self.joined += 1

    def is_alive(self) -> bool:
        return False


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
        # Nought is "has not ticked yet", so a watchdog pass over a slot holding
        # one of these leaves it alone rather than reading an attribute a real
        # worker has and this does not.
        self.heartbeat = 0.0
        # What a status write reads off a worker. A wedged screen is still a
        # screen the status file has to be able to describe -- that is most of
        # why the file exists -- so a double standing in for one answers the
        # same questions.
        self.current_scene: str | None = None
        self.last_render = None
        self.renders = 0
        self.faulted = False
        self.asleep = False

    def join(self, timeout: float | None = None) -> None:
        self._spend(timeout)

    def shutdown(self, timeout: float | None = None) -> None:
        # A tunnel's teardown is not a wait on a thread -- it signals `kubectl`
        # and waits for it to die -- so it spends the deadline too, and a
        # shutdown that only bounded the joins would not bound this.
        self._spend(timeout)

    def is_alive(self) -> bool:
        return True

    def pause(self, timeout: float) -> bool:
        """A worker inside an SPI write never comes off its panel, and spends the wait.

        Which is the case the bus guard exists for and the one a healthy rack
        hides: `pause` takes the tick lock, so a worker wedged on the wire holds
        it until the timeout runs out and then answers False.
        """
        self.granted.append(timeout)
        self._clock.advance(timeout)
        return False

    def resume(self) -> None:
        raise AssertionError("a worker that refused to pause must never be resumed")

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
    frames: Any = None,
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
        frames=frames,
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
        tunnels.append(FakeTunnel(tunnel_config, stop))
        return tunnels[-1]

    supervisor, _, _ = build(config, tmp_path, tunnel_factory=tunnel_factory)
    supervisor.start()
    try:
        assert len(supervisor.tunnels) == 1
        assert tunnels[0].started == 1
        assert tunnels[0].stop is not supervisor.stop_event, (
            "a tunnel parks on its own source's event, or retiring one integration "
            "stops the whole rack"
        )
    finally:
        supervisor.stop()

    assert tunnels[0].shutdowns == 1
    assert tunnels[0].joined == 1
    assert tunnels[0].stop.is_set(), (
        "and `stop` has to set every one of those, since the shared event no longer reaches them"
    )


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


# --- applying a pushed snapshot ---------------------------------------------
#
# The one code path in this daemon that deliberately stops a panel that is
# working. Every test below is the same question from a different side: what did
# it touch that it did not have to?


class TracedDisplay(RecordingDisplay):
    """A recording panel that writes into a rack-wide timeline as well.

    The timeline is what the ordering assertions read. Which panel was closed
    before which other one was opened is the whole question an apply has to
    answer on a bus whose devices are exclusive, and a per-panel record cannot
    express it.
    """

    def __init__(self, events: list[str], name: str) -> None:
        super().__init__()
        self._events = events
        self._name = name
        events.append(f"open:{name}")

    def sleep(self) -> None:
        self._events.append(f"sleep:{self._name}")
        super().sleep()

    def close(self) -> None:
        self._events.append(f"close:{self._name}")
        super().close()


class Rack:
    """A supervisor over virtual panels that records every one it opens.

    `opens` is a list rather than a dict keyed by name, because the question
    every test below asks is whether a *second* panel was opened for a screen
    whose name did not change -- which a dict would answer by overwriting the
    first one and losing the evidence.
    """

    def __init__(
        self,
        tmp_path: Path,
        screens: int = 2,
        on_open: Callable[[str], None] | None = None,
        night: dict[str, Any] | None = None,
        each_screen: dict[str, Any] | None = None,
        displays: list[dict[str, Any]] | None = None,
        shutdown_budget: float = SHUTDOWN_BUDGET,
        apply_budget: float = APPLY_BUDGET,
        shutdown_clock: Any = None,
        poller_factory: Any = None,
    ) -> None:
        self.events: list[str] = []
        self.opens: list[tuple[str, TracedDisplay]] = []
        self.on_open = on_open
        self.store = SnapshotStore()
        self.raw = config_dict(tmp_path, screens)
        if night is not None:
            self.raw["night"] = night
        if each_screen is not None:
            self.raw["screens"] = [{**screen, **each_screen} for screen in self.raw["screens"]]
        if displays is not None:
            self.raw["screens"] = [
                {**screen, "display": display}
                for screen, display in zip(self.raw["screens"], displays, strict=True)
            ]
        config = DaemonConfig.model_validate(self.raw)
        extra: dict[str, Any] = {} if shutdown_clock is None else {"shutdown_clock": shutdown_clock}
        self.supervisor = Supervisor(
            config=config,
            screens=resolve_screens(config),
            store=self.store,
            clock=FakeClock(NIGHT if night else NOW),
            status_path=tmp_path / "status.json",
            display_factory=self._open,
            poller_factory=poller_factory or (lambda integration_config, url_provider: None),
            shutdown_budget=shutdown_budget,
            apply_budget=apply_budget,
            **extra,
        )

    def _open(self, screen_config: ScreenConfig, name: str) -> TracedDisplay:
        display = TracedDisplay(self.events, name)
        self.opens.append((name, display))
        if self.on_open is not None:
            self.on_open(name)
        return display

    def panels(self, name: str) -> list[TracedDisplay]:
        return [display for opened, display in self.opens if opened == name]

    def worker(self, name: str) -> ScreenWorker:
        return next(worker for worker in self.supervisor.workers if worker.screen_name == name)


def edited(raw: dict[str, Any], index: int = 0, **changes: Any) -> DaemonConfig:
    """`raw` with one screen amended -- the shape of every real config edit."""
    screens = [dict(screen) for screen in raw["screens"]]
    screens[index] = {**screens[index], **changes}
    return DaemonConfig.model_validate({**raw, "screens": screens})


def with_template(raw: dict[str, Any], text: str = "hello", default: str = "d") -> DaemonConfig:
    """`raw` with every screen on one template the config itself declares.

    Which is what lets a test move a template's *body* without moving anything
    on the screen that names it -- the case a diff over `ScreenConfig` alone
    cannot see, and the one M3b's template editor produces on every save.
    """
    return DaemonConfig.model_validate(
        {
            **raw,
            "templates": {
                "house": {
                    "name": "house",
                    "params_schema": {"label": {"type": "string", "default": default}},
                    "scenes": [
                        {
                            "name": "main",
                            "elements": [
                                {"type": "text", "text": text, "cx": 0.5, "cy": 0.5, "size": 20}
                            ],
                        }
                    ],
                }
            },
            "screens": [{**screen, "template": "house", "params": {}} for screen in raw["screens"]],
        }
    )


def test_apply_swaps_the_running_config_and_restarts_the_screens(tmp_path: Path) -> None:
    """A pushed snapshot reaches the glass without restarting the process."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    try:
        before = rack.supervisor.workers[0]

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert [worker.screen_name for worker in rack.supervisor.workers] == ["RENAMED"]
        assert before.is_alive() is False, "the old worker is stopped, not orphaned"
    finally:
        rack.supervisor.stop()


def test_apply_leaves_the_previous_config_running_when_the_new_one_is_unusable(
    tmp_path: Path,
) -> None:
    """Resolution happens against the new config alone, before anything moves.

    The raise is what becomes a nack, and the nack is how the person who saved
    the edit finds out. A rack torn down first and found unservable second is
    four dark panels and a server that believes it is fine.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        with pytest.raises(ConfigError, match="no-such-template"):
            rack.supervisor.apply(edited(rack.raw, template="no-such-template"))

        assert [worker.screen_name for worker in rack.supervisor.workers] == ["S1", "S2"]
        assert rack.events == ["open:S1", "open:S2"], "no panel was touched at all"
        assert all(worker.is_alive() for worker in rack.supervisor.workers)
    finally:
        rack.supervisor.stop()


def test_a_push_that_matches_what_is_running_touches_no_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason this is a diff and not a teardown.

    The server pushes on every connect it cannot prove is redundant, and a wifi
    blip is a connect. As drafted, each one revoked every panel, joined every
    worker and reopened the lot -- a rack-wide flicker for a configuration that
    had not changed. This test fails if a single panel is closed and reopened,
    and it fails if a running worker is so much as held off its panel.
    """
    rack = Rack(tmp_path, screens=4)
    rack.supervisor.start()
    held: list[str] = []
    paused = ScreenWorker.pause

    def pause(self: ScreenWorker, timeout: float) -> bool:
        held.append(self.screen_name)
        return paused(self, timeout)

    monkeypatch.setattr(ScreenWorker, "pause", pause)
    try:
        before = list(rack.supervisor.workers)

        with caplog.at_level("INFO"):
            rack.supervisor.apply(DaemonConfig.model_validate(rack.raw))

        assert rack.events == ["open:S1", "open:S2", "open:S3", "open:S4"]
        assert rack.supervisor.workers == before, "the same threads are still drawing"
        assert all(worker.is_alive() for worker in before)
        assert all(display.sleeps == 0 for _, display in rack.opens)
        assert all(display.closed == 0 for _, display in rack.opens)
        assert held == [], "a redundant push interrupted a worker that had nothing to change"
        assert any("already running" in record.getMessage() for record in caplog.records)
    finally:
        rack.supervisor.stop()


def test_a_rack_pushed_to_over_and_over_holds_one_racks_worth_of_state(tmp_path: Path) -> None:
    """The new configuration replaces the old one; it does not accumulate beside it.

    A daemon is pushed to on every connect the server cannot prove is redundant,
    for months at a time, and it runs on a Pi 3B+ with 1 GiB shared with the GPU.
    An `apply` that appended rather than replaced would hold a `ResolvedScreen`
    -- its bound params and every scene of its template -- for each screen of
    each push, forever, and nothing else here reads that list often enough to
    notice. It is a leak that only shows up as a rack that dies in the night
    weeks after the change that caused it.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        for index in range(20):
            rack.supervisor.apply(edited(rack.raw, name=f"S{index}"))

        assert len(rack.supervisor._screens) == 2
        assert len(rack.supervisor._slots) == 2
        assert len(rack.supervisor.workers) == 2
    finally:
        rack.supervisor.stop()


def test_apply_leaves_alone_every_screen_the_edit_did_not_name(tmp_path: Path) -> None:
    """A one-screen edit costs one screen, which is what bounds the work as well
    as what keeps three panels from blinking for a change to the fourth."""
    rack = Rack(tmp_path, screens=4)
    rack.supervisor.start()
    try:
        untouched = [rack.worker(name) for name in ("S1", "S2", "S4")]

        rack.supervisor.apply(edited(rack.raw, index=2, name="RENAMED"))

        assert [worker.screen_name for worker in rack.supervisor.workers] == [
            "S1",
            "S2",
            "RENAMED",
            "S4",
        ], "and the panels keep their order"
        assert [rack.worker(name) for name in ("S1", "S2", "S4")] == untouched
        for name in ("S1", "S2", "S4"):
            display = rack.panels(name)[0]
            assert display.sleeps == 0 and display.closed == 0, name
        assert len(rack.panels("S3")) == 1, "the changed screen's panel is not reopened as S3"
    finally:
        rack.supervisor.stop()


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("its name", {"name": "RENAMED"}),
        ("its parameters", {"params": {"title": "S1", "value": "{{prom.cpu}}", "big": "0"}}),
        ("its template", {"template": "text-only", "params": {"big": "hi"}}),
        ("its rotation", {"rotation": 180}),
        ("its mirroring", {"hflip": True}),
        ("its position", {"position": 9}),
        ("its own night window", {"sleep_override": {"start": "01:00", "end": "02:00"}}),
        ("which panel it drives", {"display": {"backend": "virtual", "out_dir": "/tmp/elsewhere"}}),
    ],
)
def test_a_screen_is_restarted_when_anything_that_changes_what_it_draws_changes(
    tmp_path: Path, what: str, change: dict[str, Any]
) -> None:
    """The diff has to cover everything the worker reads, not just the name.

    Each of these is invisible from the screen's name and each one changes what
    lands on the glass or where it lands -- a rotation the panel is not bolted
    at, a parameter bound to a different query, a different SPI device
    altogether. A comparison that missed any of them would leave the old worker
    drawing the old thing while the server believed the edit had taken.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        before = rack.worker("S1")
        kept = rack.worker("S2")

        rack.supervisor.apply(edited(rack.raw, **change))

        assert before.is_alive() is False, f"a change to {what} did not restart the screen"
        assert len(rack.opens) == 3, f"a change to {what} did not reopen the panel"
        assert kept.is_alive() is True, "and its bus-mate was left alone"
    finally:
        rack.supervisor.stop()


def test_a_screen_is_restarted_when_the_racks_night_window_moves(tmp_path: Path) -> None:
    """The one comparison that cannot be made between two screens alone.

    A worker takes the night window at construction and never re-reads it, and
    the rack's window lives on `DaemonConfig` rather than on the screen -- so
    every `ScreenConfig` in a snapshot that moves lights-out an hour earlier is
    byte-identical to the one running. Without this the rack goes on sleeping at
    the old hour, and there is nothing in the config, the status file or the log
    that disagrees with it.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        before = list(rack.supervisor.workers)

        rack.supervisor.apply(
            DaemonConfig.model_validate(
                {**rack.raw, "night": {"enabled": True, "start": "22:00", "end": "06:00"}}
            )
        )

        assert [worker.is_alive() for worker in before] == [False, False]
        assert len(rack.opens) == 4, "both panels were handed to workers that know the new window"
    finally:
        rack.supervisor.stop()


def test_a_screen_with_its_own_night_window_is_left_alone_when_the_racks_moves(
    tmp_path: Path,
) -> None:
    """The other side of the same comparison: what is compared is the window the
    screen *effectively* runs on, so one that overrides the rack's is untouched
    by a change to it -- a lights-out NAS panel that never sleeps does not blink
    because somebody moved the rack's evening."""
    rack = Rack(
        tmp_path,
        screens=1,
        each_screen={"sleep_override": {"enabled": False, "start": "01:00", "end": "02:00"}},
    )
    rack.supervisor.start()
    try:
        before = list(rack.supervisor.workers)
        opened = len(rack.opens)

        rack.supervisor.apply(
            DaemonConfig.model_validate(
                {**rack.raw, "night": {"enabled": True, "start": "22:00", "end": "06:00"}}
            )
        )

        assert [worker.is_alive() for worker in before] == [True]
        assert len(rack.opens) == opened, "its panel was not touched"
    finally:
        rack.supervisor.stop()


def test_a_screen_is_restarted_when_its_templates_scenes_change(tmp_path: Path) -> None:
    """The case a diff over `ScreenConfig` alone cannot see, and the one M3b's
    template editor produces on every save: the screen names the same template,
    with the same parameters, and the template now draws something else. Without
    this the old layout stays on the glass and the server is told it landed."""
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        rack.supervisor.apply(with_template(rack.raw, text="before"))
        drawing = rack.worker("S1")
        opened = len(rack.opens)

        rack.supervisor.apply(with_template(rack.raw, text="after"))

        assert drawing.is_alive() is False
        assert len(rack.opens) == opened + 2, "both screens draw that template"
    finally:
        rack.supervisor.stop()


def test_a_screen_is_restarted_when_its_templates_parameter_defaults_change(
    tmp_path: Path,
) -> None:
    """Same template, same scenes, and a different value bound into them.

    `bind_params` is where a template's declared default meets a screen's own
    overrides, and a screen that overrides nothing takes the default whole. It is
    resolved once, at load, and handed to the worker -- so a default edited on
    the server is a number on the glass that never changes.
    """
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    try:
        rack.supervisor.apply(with_template(rack.raw, default="before"))
        drawing = rack.worker("S1")
        opened = len(rack.opens)

        rack.supervisor.apply(with_template(rack.raw, default="after"))

        assert drawing.is_alive() is False
        assert len(rack.opens) == opened + 1
    finally:
        rack.supervisor.stop()


def test_a_screen_is_restarted_when_the_integration_it_depends_on_is_renamed(
    tmp_path: Path,
) -> None:
    """`depends_on` is what decides whether a screen shows `connecting`, and it
    is derived from the integration *names*. Rename one and the screen's bindings
    resolve against nothing -- but the worker holds the old set, so it goes on
    gating its scenes on a source that no longer exists."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    try:
        before = rack.supervisor.workers[0]
        assert before._screen.depends_on == frozenset({"prom"})
        renamed = {
            **rack.raw,
            "integrations": [{**rack.raw["integrations"][0], "name": "metrics"}],
        }

        rack.supervisor.apply(DaemonConfig.model_validate(renamed))

        assert before.is_alive() is False
        assert rack.supervisor.workers[0]._screen.depends_on == frozenset()
    finally:
        rack.supervisor.stop()


def test_apply_closes_a_retired_panel_before_it_opens_its_replacement(tmp_path: Path) -> None:
    """SPI devices are exclusive: `/dev/spidev0.0` cannot be opened twice.

    So a changed screen's panel has to be given up before the replacement can
    take it, and an apply that opened first would fail on the hardware while
    passing against any backend that does not mind.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert rack.events == ["open:S1", "open:S2", "sleep:S1", "close:S1", "open:RENAMED"]
    finally:
        rack.supervisor.stop()


def test_apply_opens_every_panel_before_it_starts_any_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-phase rule M2 paid for, kept on a rack that is already running.

    A Pi 3 carries two chip selects on SPI0 and two more on SPI1, and opening a
    GC9A01 is a hardware reset and a fifty-command init sequence over that shared
    wire. A worker that begins drawing while its bus-mate is still initialising
    tramples the init: the other panel comes up showing unconfigured RAM, and
    which one loses depends on the order they were started in. The script this
    daemon replaces said it out loud -- "Init displays one at a time (GPIO race
    prevention)".
    """
    rack = Rack(tmp_path, screens=4)
    rack.supervisor.start()
    started = ScreenWorker.start
    events: list[str] = []

    def start(self: ScreenWorker) -> None:
        events.append(f"start:{self.screen_name}")
        started(self)

    try:
        rack.on_open = lambda name: events.append(f"open:{name}")
        monkeypatch.setattr(ScreenWorker, "start", start)
        raw = {
            **rack.raw,
            "screens": [
                {**screen, "name": f"N{n}"} for n, screen in enumerate(rack.raw["screens"])
            ],
        }

        rack.supervisor.apply(DaemonConfig.model_validate(raw))

        opens = [index for index, event in enumerate(events) if event.startswith("open:")]
        starts = [index for index, event in enumerate(events) if event.startswith("start:")]
        assert len(opens) == 4 and len(starts) == 4
        assert max(opens) < min(starts), f"a worker started mid-bring-up: {events}"
    finally:
        rack.supervisor.stop()


def test_a_kept_worker_is_held_off_its_panel_while_another_is_opened(tmp_path: Path) -> None:
    """The other half of the two-phase rule, and the half only an apply needs.

    On a rack coming up there is nothing drawing yet, so ordering the opens
    before the starts is enough. On a rack that is *running*, the panel being
    initialised has a bus-mate whose worker is drawing right now -- the same
    interleaving, arriving by a different road. So the kept workers are held off
    their panels for exactly as long as the opens take.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    held: list[bool] = []
    try:
        kept = rack.worker("S2")
        rack.on_open = lambda name: held.append(kept.held_off)

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert held == [True], "a panel was initialised while a bus-mate could still draw"
        assert kept.held_off is False, "and it is given its panel back afterwards"
    finally:
        rack.supervisor.stop()


def test_pause_says_whether_it_took_the_panel(tmp_path: Path) -> None:
    """It can fail, and the caller has to be able to tell. A worker wedged inside
    an SPI write never gives its panel up, and an apply that waited for one
    would spend its whole budget on a screen that is already lost."""
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=1))
    worker = ScreenWorker(
        screen=resolve_screens(config)[0],
        store=SnapshotStore(),
        display=RecordingDisplay(),
        system={},
        night=NightWindow(enabled=False),
        stop=threading.Event(),
        clock=FakeClock(NOW),
    )

    assert worker.pause(0.0) is True
    assert worker.held_off is True
    assert worker.pause(0.0) is False, "a second hold must not claim a panel it did not take"
    worker.resume()
    assert worker.held_off is False
    assert worker.pause(0.0) is True
    worker.resume()


def test_resuming_a_worker_that_never_paused_costs_nothing_but_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`Lock.release` on a lock this thread never took raises `RuntimeError`.

    From `_off_the_bus`'s `finally`, which runs after the retired panels are
    closed and the new ones opened -- so that raise escapes `apply` and the link
    nacks a snapshot that is in fact on the glass, and the kept workers that
    *did* pause are never resumed: up to three panels frozen on their last frame
    for the life of the process. One panel's pairing bug must not cost the rack.
    """
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=1))
    worker = ScreenWorker(
        screen=resolve_screens(config)[0],
        store=SnapshotStore(),
        display=RecordingDisplay(),
        system={},
        night=NightWindow(enabled=False),
        stop=threading.Event(),
        clock=FakeClock(NOW),
    )

    with caplog.at_level("WARNING", logger="ors_daemon.screen"):
        worker.resume()
        worker.pause(0.0)
        worker.resume()
        worker.resume()

    assert worker.held_off is False
    assert worker.pause(0.0) is True, "and the lock is left in a state a later pause can take"
    worker.resume()
    assert [record.getMessage() for record in caplog.records] == [
        "a screen was resumed that was never held off",
        "a screen was resumed that was never held off",
    ]


def test_apply_stops_only_the_worker_whose_screen_changed(tmp_path: Path) -> None:
    """Which is why a worker cannot wait on the supervisor's shared stop event:
    setting that one stops the whole rack, and there is no reconfiguration for
    which that is the right answer."""
    rack = Rack(tmp_path, screens=3)
    rack.supervisor.start()
    try:
        retired = rack.worker("S2")
        kept = [rack.worker("S1"), rack.worker("S3")]

        rack.supervisor.apply(edited(rack.raw, index=1, name="RENAMED"))
        retired.join(WAIT)

        assert retired.is_alive() is False
        assert all(worker.is_alive() for worker in kept)
        assert rack.supervisor._stop_event.is_set() is False, "and the rack is not shutting down"
    finally:
        rack.supervisor.stop()


def test_a_retired_worker_can_no_longer_reach_its_panel(tmp_path: Path) -> None:
    """It may be wedged, and a wedged thread cannot be killed. What can be taken
    away is its lease -- otherwise the replacement and the abandoned one are two
    threads writing one SPI device, which corrupts both frames rather than
    racing for the last one."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    try:
        panel = rack.supervisor.workers[0]._display

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert panel.live is False
    finally:
        rack.supervisor.stop()


def test_a_worker_that_will_not_stop_loses_its_panel_anyway(tmp_path: Path) -> None:
    """The case the lease exists for, and the one a healthy rack hides.

    A worker that leaves gives its own lease up on the way out, so revoking is
    invisible until the worker does *not* leave -- which is exactly when it
    matters, because the replacement is about to start writing to the device it
    is still holding. Here the join is given nothing and the thread is a fiction
    that never ends, which is what a screen wedged inside an SPI write is.
    """
    clock = FakeMonotonic()
    supervisor, _ = wedged_rack(tmp_path, clock, screens=1, tunnels=0, pollers=0)
    panel = supervisor._slots[0].panel

    supervisor.apply(edited(config_dict(tmp_path, screens=1), name="RENAMED"))
    try:
        assert panel.live is False
        assert supervisor._slots[0].panel is not panel, "and the replacement has its own lease"
    finally:
        supervisor.stop()


def test_stop_stops_a_worker_parked_for_the_night(tmp_path: Path) -> None:
    """A screen worker now waits on an event of its slot's own, so that a
    reconfigure can retire one without stopping the rack. `stop` therefore has to
    set every one of them.

    Proved on a *sleeping* rack, which is the only place it shows. Awake, a
    worker parks on the snapshot store and `stop` closes that, so it leaves
    whether or not it was asked to. Asleep it parks on its stop event in
    `_NIGHT_PARK_CHUNK` chunks -- twenty seconds at a time, which nothing else
    interrupts -- so a shutdown that set only the supervisor's event would spend
    its whole budget joining four threads that had never been told to leave, and
    then sleep the panels underneath them.
    """
    parked = threading.Event()
    rack = Rack(
        tmp_path,
        screens=1,
        night={"enabled": True, "start": "23:00", "end": "07:00"},
        shutdown_budget=0.5,
    )
    # Armed as the panel is opened, not after `start` returns: the worker is
    # already asleep by then on a fast machine, and the handshake would wait for
    # an event that had fired before anything was listening.
    rack.on_open = lambda name: setattr(rack.opens[-1][1], "on_sleep", parked.set)
    rack.supervisor.start()
    worker = rack.supervisor.workers[0]
    assert parked.wait(WAIT), "the worker never reached its night park"

    rack.supervisor.stop()

    worker.join(WAIT)
    assert worker.is_alive() is False


def test_apply_refuses_a_snapshot_once_the_daemon_is_stopping(tmp_path: Path) -> None:
    """Both endings are dark panels: overrun `SHUTDOWN_BUDGET` and systemd
    SIGKILLs before the panels are slept, or abandon the apply and leave the rack
    halfway through a repaint. Refusing is a nack, and the next connect gets the
    push again."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    rack.supervisor.stop()

    with pytest.raises(RuntimeError, match="stopping"):
        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

    assert len(rack.opens) == 1, "no panel was opened by a daemon on its way out"
    assert rack.opens[0][1].closed == 1


def test_apply_retries_a_panel_that_would_not_open_before(tmp_path: Path) -> None:
    """A ribbon reseated between two pushes is the case, and the status file is
    where the failure was reported. Nothing else in the daemon retries an open."""
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=2))
    opened: list[str] = []
    broken = {"S1"}

    def display_factory(screen_config: ScreenConfig, name: str) -> RecordingDisplay:
        if name in broken:
            raise DisplayError(f"cannot open SPI for {name}")
        opened.append(name)
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
        assert opened == ["S2"]
        broken.clear()

        supervisor.apply(config)

        assert opened == ["S2", "S1"]
        assert sorted(worker.screen_name for worker in supervisor.workers) == ["S1", "S2"]
        assert supervisor._unavailable == [], "and it is no longer reported unavailable"
    finally:
        supervisor.stop()


def test_apply_reports_the_new_configs_fingerprint(tmp_path: Path) -> None:
    """The one number the server uses to decide whether the Pi is running what it
    pushed. Computed once at construction in M2 because the config could not
    change under a running supervisor; it can now."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    try:
        supervisor = rack.supervisor
        supervisor.tick()
        before = json.loads((tmp_path / "status.json").read_text())["config_fingerprint"]

        supervisor.apply(edited(rack.raw, name="RENAMED"))
        supervisor.tick()

        after = json.loads((tmp_path / "status.json").read_text())
        assert after["config_fingerprint"] != before
        assert [screen["name"] for screen in after["screens"]] == ["RENAMED"]
    finally:
        rack.supervisor.stop()


# --- reconfiguring the integrations -----------------------------------------
#
# A push that moves an integration used to warn and carry on. That is worse than
# it sounds: `_diff` retires every screen whose `depends_on` moved, and the
# replacement worker's health gate finds no entry for the new source, so the
# panel that was showing a live reading shows `connecting` until somebody
# restarts the daemon.


def recording_pollers() -> tuple[list[RecordingPoller], Callable[[Any, Any], RecordingPoller]]:
    pollers: list[RecordingPoller] = []

    def factory(integration_config: Any, url_provider: Any) -> RecordingPoller:
        pollers.append(RecordingPoller(integration_config, url_provider))
        return pollers[-1]

    return pollers, factory


def with_integrations(raw: dict[str, Any], *integrations: dict[str, Any]) -> DaemonConfig:
    """`raw` with a different set of sources, and one screen bound to the first."""
    first = integrations[0]["name"]
    screens = [dict(screen) for screen in raw["screens"]]
    screens[0] = {
        **screens[0],
        "params": {
            "title": "S1",
            "value": f"{{{{{first}.cpu}}}}",
            "big": f"{{{{{first}.cpu | round:0}}}}%",
        },
    }
    return DaemonConfig.model_validate(
        {**raw, "integrations": list(integrations), "screens": screens}
    )


def test_apply_starts_a_poller_for_a_source_the_push_adds(tmp_path: Path) -> None:
    """Otherwise the screens that depend on it show `connecting` for ever.

    Measured on the draft, on a push that pointed one screen at a new `qb`
    source: `depends_on={'qb'}`, `store health keys=['prom']`, `pollers=0`,
    `current_scene='connecting'`. The screen had been showing a live reading a
    moment earlier -- `_diff` retires a screen whose `depends_on` moved, and the
    replacement worker's health gate finds nothing for the new name -- so a push
    that warned and carried on left a panel worse off than a push that did
    nothing at all, until someone restarted the daemon.
    """
    pollers, poller_factory = recording_pollers()
    rack = Rack(tmp_path, screens=1, poller_factory=poller_factory)
    rack.supervisor.start()
    try:
        rack.supervisor.apply(with_integrations(rack.raw, integration_dict("qb")))

        assert [poller.config.name for poller in rack.supervisor.pollers] == ["qb"]
        assert pollers[-1].config.name == "qb" and pollers[-1].started == 1
        assert "qb" in rack.store.read().health, (
            "a screen depending on a source with no health entry shows `connecting`"
        )
        worker = rack.supervisor.workers[0]
        assert worker._screen.depends_on == frozenset({"qb"})
    finally:
        rack.supervisor.stop()


def test_apply_takes_down_the_poller_and_tunnel_of_a_source_the_push_drops(
    tmp_path: Path,
) -> None:
    """And the health entry goes with it: the status file describes this rack.

    The teardown is done by the reaper rather than by the apply -- see the test
    below -- so `stop`, which joins it, is where a test can be sure it happened.
    """
    pollers, poller_factory = recording_pollers()
    tunnels: list[FakeTunnel] = []
    raw = config_dict(tmp_path, screens=1)
    raw["integrations"] = [integration_dict("prom", local_port=19090)]
    rack = Rack(tmp_path, screens=1, poller_factory=poller_factory)
    rack.raw = raw
    config = DaemonConfig.model_validate(raw)
    rack.supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=rack.store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=rack._open,
        poller_factory=poller_factory,
        tunnel_factory=lambda tunnel_config, stop: (
            tunnels.append(FakeTunnel(tunnel_config, stop)) or tunnels[-1]
        ),
    )
    rack.supervisor.start()
    rack.store.put("prom", {"cpu": 41.0}, latency_ms=3.0, now=NOW)

    rack.supervisor.apply(
        DaemonConfig.model_validate(
            {**raw, "integrations": [], "screens": [{**raw["screens"][0], "params": {"big": "x"}}]}
        )
    )
    # Recorded rather than waited for. The reaper is a thread, so "it finished"
    # is not a fact a test can assert by looking afterwards -- it would pass
    # whenever the thread happened to be quick, which is always, until the
    # machine is loaded. What makes the assertions below true is that `stop`
    # *joins* it, out of the same deadline as everything else, so that is what is
    # pinned.
    reaped = rack.supervisor._reaper
    assert reaped is not None
    joins: list[float | None] = []
    joined = reaped.join

    def record(timeout: float | None = None) -> None:
        joins.append(timeout)
        joined(timeout)

    reaped.join = record  # type: ignore[method-assign]
    rack.supervisor.stop()

    assert joins and all(timeout is not None and timeout > 0.0 for timeout in joins), (
        "a shutdown that does not wait for the reaper is one that returns mid-teardown"
    )

    assert rack.supervisor.pollers == [] and rack.supervisor.tunnels == []
    assert tunnels[0].stop.is_set(), (
        "which is what actually stops them, and costs the apply nothing"
    )
    assert pollers[0].joined == 1, "the retired poller is waited on, not abandoned"
    assert (tunnels[0].shutdowns, tunnels[0].joined) == (1, 1), "and kubectl is signalled"
    assert all(
        timeout is not None and 0.0 < timeout <= SOURCE_TEARDOWN_BUDGET
        for timeout in tunnels[0].shutdown_timeouts
    ), "a teardown with no deadline is not a teardown"
    snapshot = rack.store.read()
    assert "prom" not in snapshot.health, "a source this rack no longer has"
    assert "prom" not in snapshot.data, "and its last readings go with it, not into a screen"


def test_apply_does_not_wait_for_a_kubectl_teardown(tmp_path: Path) -> None:
    """M2 measured that teardown at ten seconds: the whole of `SHUTDOWN_BUDGET`.

    `apply` runs on the link thread -- the thread that reads the socket and the
    one that consults the stop event -- so every second it spends is a second a
    SIGTERM waits. Setting the source's stop event costs nothing and is done
    here; waiting for `kubectl` to die is handed to the reaper, which `stop`
    joins inside the budget it already has.
    """
    pollers, poller_factory = recording_pollers()
    tunnels: list[FakeTunnel] = []
    raw = config_dict(tmp_path, screens=1)
    raw["integrations"] = [integration_dict("prom", local_port=19090)]
    config = DaemonConfig.model_validate(raw)
    rack = Rack(tmp_path, screens=1)
    rack.supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=rack.store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=rack._open,
        poller_factory=poller_factory,
        tunnel_factory=lambda tunnel_config, stop: (
            tunnels.append(FakeTunnel(tunnel_config, stop)) or tunnels[-1]
        ),
    )
    rack.supervisor.start()
    entered = threading.Event()
    released = threading.Event()

    def slow_teardown() -> None:
        entered.set()
        released.wait(WAIT)

    tunnels[0].on_shutdown = slow_teardown
    started = time.monotonic()

    rack.supervisor.apply(
        DaemonConfig.model_validate(
            {**raw, "integrations": [], "screens": [{**raw["screens"][0], "params": {"big": "x"}}]}
        )
    )
    elapsed = time.monotonic() - started

    assert entered.wait(WAIT), "the reaper never reached the teardown"
    released.set()
    rack.supervisor.stop()
    assert elapsed < WAIT / 2, f"the apply waited {elapsed:.3f}s for a kubectl teardown"


def test_apply_replaces_the_poller_of_a_source_whose_configuration_moved(tmp_path: Path) -> None:
    """A changed source is a removal and an addition, not an edit in place.

    A rack whose snapshot names a different Prometheus used to poll the old one
    for ever with a warning as the only sign. Nothing here can edit a running
    poller -- it holds an `Integration` built from the config it was handed --
    so the honest reconfiguration is to take it down and start another.
    """
    pollers, poller_factory = recording_pollers()
    rack = Rack(tmp_path, screens=1, poller_factory=poller_factory)
    rack.supervisor.start()
    moved = {**rack.raw["integrations"][0], "url": "http://elsewhere:9090"}

    rack.supervisor.apply(with_integrations(rack.raw, moved))
    rack.supervisor.stop()

    assert [poller.config.url for poller in rack.supervisor.pollers] == ["http://elsewhere:9090"]
    assert len(pollers) == 2, "a poller was built for the new configuration"
    assert pollers[0].joined == 1, "and the one on the old URL was taken down"
    assert "prom" in rack.store.read().health, (
        "the name is still configured, so its health entry is not dropped under the new poller"
    )


def test_a_source_that_is_only_reordered_is_left_running(tmp_path: Path) -> None:
    """The diff is over the models, so a snapshot that lists the same sources in
    another order does not take four panels' data source down and up."""
    pollers, poller_factory = recording_pollers()
    raw = config_dict(tmp_path, screens=1)
    raw["integrations"] = [integration_dict("prom"), integration_dict("qb")]
    rack = Rack(tmp_path, screens=1, poller_factory=poller_factory)
    rack.raw = raw
    config = DaemonConfig.model_validate(raw)
    rack.supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=rack.store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=rack._open,
        poller_factory=poller_factory,
    )
    rack.supervisor.start()
    try:
        rack.supervisor.apply(
            DaemonConfig.model_validate(
                {**raw, "integrations": [integration_dict("qb"), integration_dict("prom")]}
            )
        )

        assert len(pollers) == 2, "no poller was started or stopped"
        assert all(poller.joined == 0 for poller in pollers)
    finally:
        rack.supervisor.stop()


def test_apply_says_what_it_did_to_the_integrations(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It used to say it could not do it at all. What replaced that has to be
    just as readable from a journal: which sources went, and which arrived."""
    pollers, poller_factory = recording_pollers()
    rack = Rack(tmp_path, screens=1, poller_factory=poller_factory)
    rack.supervisor.start()
    try:
        caplog.clear()
        with caplog.at_level("INFO", logger="ors_daemon.supervisor"):
            rack.supervisor.apply(with_integrations(rack.raw, integration_dict("qb")))

        message = next(record for record in caplog.records if "integration" in record.getMessage())
        assert message.added == ["qb"]
        assert message.retired == ["prom"]
    finally:
        rack.supervisor.stop()


def test_apply_is_bounded_by_one_deadline_however_many_screens_changed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It runs on the link thread -- the thread that reads the socket and the one
    that consults the stop event -- so every second it spends is a second a
    SIGTERM waits, out of ten for the whole daemon. A timeout *each* would be
    multiplied by a number this module does not choose: how many screens a
    snapshot holds is the server's decision, and four wedged workers at three
    seconds apiece is a shutdown systemd SIGKILLs. So there is one deadline, and
    sixteen screens cost what four do.

    What the deadline bounds is the *waiting*, and nothing else. A worker that
    will not stop has its lease revoked and its panel slept and closed anyway,
    and the replacement rack is brought up in full -- because the one thing an
    apply may not leave behind is a rack that is half of each configuration.
    """
    clock = FakeMonotonic()
    supervisor, displays = wedged_rack(tmp_path, clock, screens=4, tunnels=0, pollers=0)
    retired = [slot.worker for slot in supervisor._slots]
    raw = config_dict(tmp_path, screens=4)
    replacement = DaemonConfig.model_validate(
        {**raw, "screens": [{**screen, "name": f"N{n}"} for n, screen in enumerate(raw["screens"])]}
    )
    started = clock.now

    try:
        with caplog.at_level("ERROR"):
            supervisor.apply(replacement)

        assert clock.now - started <= APPLY_BUDGET
        granted = [timeout for worker in retired for timeout in worker.granted]  # type: ignore[union-attr]
        assert granted[0] == APPLY_BUDGET, "the first wedged worker may spend the lot"
        assert granted[-1] == 0.0, "and the last is given nothing, not three seconds more"
        assert all(display.calls == ["sleep", "close"] for display in displays), (
            "and every retired panel is blanked regardless"
        )
        assert sorted(worker.screen_name for worker in supervisor.workers) == [
            "N0",
            "N1",
            "N2",
            "N3",
        ], "and the pushed configuration is fully on the rack, not half of it"
        assert any("budget" in record.getMessage() for record in caplog.records)
    finally:
        supervisor.stop()


# --- a panel that would not start, and one that would not come off its bus ---


def refusing_to_start(monkeypatch: pytest.MonkeyPatch, refuse: set[str]) -> None:
    """Make `ScreenWorker.start` answer as a Pi too short of memory to fork does."""
    started = ScreenWorker.start

    def start(self: ScreenWorker) -> None:
        if self.screen_name in refuse:
            raise RuntimeError("can't start new thread")
        started(self)

    monkeypatch.setattr(ScreenWorker, "start", start)


def test_a_screen_whose_worker_would_not_start_keeps_no_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its panel has been slept and closed, so the slot describes nothing.

    Kept, it was matched as *unchanged* by every later diff -- so the start loop
    skipped it, the watchdog skipped it, and one dark circle stayed dark for the
    life of the process while `_diff`'s own docstring promised a failed panel
    "is opened again here".
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        refusing_to_start(monkeypatch, {"RENAMED"})

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert [slot.screen.config.name for slot in rack.supervisor._slots] == ["S2"]
        assert [worker.screen_name for worker in rack.supervisor.workers] == ["S2"]
        assert [entry.name for entry in rack.supervisor._unavailable] == ["RENAMED"]
        assert rack.panels("RENAMED")[0].calls == ["sleep", "close"], "its panel was blanked"
    finally:
        rack.supervisor.stop()


def test_a_screen_whose_worker_would_not_start_is_still_reported_after_an_unrelated_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dark circle, permanently, and absent from the status file.

    Measured on the draft: after the failed start, `unavailable=['R1']` and
    `workers=['S2']`; after a push that touched only the *other* screen,
    `unavailable=[]`, `workers=['R2']`, `status screens=['R2']` -- a four-panel
    rack with a dead panel reporting exactly what a healthy one-panel rack
    reports. That is verbatim the case `UnavailableScreen` exists to prevent.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        refusing_to_start(monkeypatch, {"R1"})
        rack.supervisor.apply(edited(rack.raw, name="R1"))
        renamed = {
            **rack.raw,
            "screens": [{**rack.raw["screens"][0], "name": "R1"}, *rack.raw["screens"][1:]],
        }

        rack.supervisor.apply(edited(renamed, index=1, name="R2"))
        rack.supervisor.tick()

        assert [entry.name for entry in rack.supervisor._unavailable] == ["R1"]
        reported = json.loads((tmp_path / "status.json").read_text())["screens"]
        by_name = {screen["name"]: screen for screen in reported}
        assert set(by_name) == {"R1", "R2"}, "every configured screen is reported"
        assert by_name["R1"]["state"] == "unavailable"
        assert "can't start new thread" in by_name["R1"]["error"]
    finally:
        rack.supervisor.stop()


def test_a_screen_whose_worker_would_not_start_is_started_again_by_the_next_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same promise `_diff` makes about a panel that would not open.

    A Pi that was out of threads for a moment is a rack that comes back on the
    next push, without a restart -- and nothing else in the daemon retries this.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        refuse = {"R1"}
        refusing_to_start(monkeypatch, refuse)
        rack.supervisor.apply(edited(rack.raw, name="R1"))
        renamed = {
            **rack.raw,
            "screens": [{**rack.raw["screens"][0], "name": "R1"}, *rack.raw["screens"][1:]],
        }
        refuse.clear()

        rack.supervisor.apply(edited(renamed, index=1, name="R2"))

        assert sorted(worker.screen_name for worker in rack.supervisor.workers) == ["R1", "R2"]
        assert rack.supervisor._unavailable == []
        assert len(rack.panels("R1")) == 2, "a fresh panel, because the first one was closed"
    finally:
        rack.supervisor.stop()


def test_a_retirement_that_overran_the_budget_still_holds_the_kept_workers_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bus guard gets its own deadline, not the joins' leftovers.

    One wedged retiring worker spends the whole of `APPLY_BUDGET`, and every kept
    worker was then offered `pause(0.0)` -- which returns False whenever that
    worker is inside `tick`. Measured on the draft with four screens and one
    wedged retirement: `pause timeouts granted to kept workers: [0.0, 0.0, 0.0]`.
    At ~23ms of SPI per frame on four panels, catching one mid-`show` is likely,
    and that is the M2 interleaving that produced the pale grey rectangle: the
    init registers are wrong afterwards, not just the framebuffer.
    """
    clock = FakeMonotonic()
    rack = Rack(tmp_path, screens=2, shutdown_clock=clock)
    rack.supervisor.start()
    try:
        retiring = rack.supervisor._slots[0]
        retiring.worker = Wedged(clock, "S1")  # type: ignore[assignment]
        granted: list[float] = []
        paused = ScreenWorker.pause

        def pause(self: ScreenWorker, timeout: float) -> bool:
            granted.append(timeout)
            return paused(self, timeout)

        monkeypatch.setattr(ScreenWorker, "pause", pause)

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert retiring.worker.granted == [APPLY_BUDGET]  # type: ignore[union-attr]
        assert granted == [BUS_GUARD_PER_SCREEN], (
            "the kept worker was offered whatever the retirement left, which was nothing"
        )
    finally:
        rack.supervisor.stop()


def test_the_bus_guard_is_bounded_however_many_screens_are_kept(tmp_path: Path) -> None:
    """M27 from the other side: a whole budget per pause is 12s for four panels.

    The guard is one deadline for the rack, like every other wait here, because
    how many screens a snapshot holds is the server's decision and not this
    module's. What each screen is *promised* is `BUS_GUARD_PER_SCREEN`, which is
    an order of magnitude more than a frame costs; what the rack can spend in
    total is `BUS_GUARD_BUDGET`, whatever the count.

    Six screens, so that five kept ones ask for more than the budget holds: the
    two halves of that sentence only disagree past four panels, and a rack is
    allowed to grow a screen.
    """
    clock = FakeMonotonic()
    rack = Rack(tmp_path, screens=6, shutdown_clock=clock)
    rack.supervisor.start()
    try:
        wedged = [Wedged(clock, slot.screen.config.name) for slot in rack.supervisor._slots[1:]]
        for slot, worker in zip(rack.supervisor._slots[1:], wedged, strict=True):
            slot.worker = worker  # type: ignore[assignment]
        started = clock.now

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert clock.now - started <= BUS_GUARD_BUDGET, "the whole guard, not one per screen"
        assert [timeout for worker in wedged for timeout in worker.granted] == [
            BUS_GUARD_PER_SCREEN,
            BUS_GUARD_PER_SCREEN,
            BUS_GUARD_PER_SCREEN,
            BUS_GUARD_PER_SCREEN,
            0.0,
        ], "each is promised a real hold, and the last is given what is left, which is nothing"
    finally:
        rack.supervisor.stop()


def test_a_screen_that_will_not_come_off_the_bus_stops_the_opens_on_that_bus(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Proceeding silently is what this is about: an overrun guard is not a guard.

    A fifty-command init sequence run while a bus-mate is drawing comes up
    showing unconfigured RAM, and it stays wrong afterwards because the init
    *registers* are wrong rather than the framebuffer. A panel that is dark and
    says so in the status file is diagnosable; a pale grey rectangle is not, and
    the next push retries the open either way.
    """
    clock = FakeMonotonic()
    rack = Rack(tmp_path, screens=2, shutdown_clock=clock)
    rack.supervisor.start()
    try:
        rack.supervisor._slots[1].worker = Wedged(clock, "S2")  # type: ignore[assignment]

        with caplog.at_level("ERROR", logger="ors_daemon.supervisor"):
            rack.supervisor.apply(edited(rack.raw, name="RENAMED"))
        rack.supervisor.tick()

        assert rack.panels("RENAMED") == [], "a panel was initialised on a bus still being drawn on"
        assert [entry.name for entry in rack.supervisor._unavailable] == ["RENAMED"]
        assert any("bus" in record.getMessage() for record in caplog.records), (
            "the log has to say the guard was skipped, not only that a screen would not stop"
        )
        reported = json.loads((tmp_path / "status.json").read_text())["screens"]
        assert [screen["state"] for screen in reported if screen["name"] == "RENAMED"] == [
            "unavailable"
        ]
    finally:
        rack.supervisor.stop()


def gc9a01(bus: int, cs: int) -> dict[str, Any]:
    """A panel on a real SPI device, so that two screens can be on two buses."""
    return {"backend": "gc9a01", "spi_bus": bus, "spi_cs": cs, "dc": 25, "rst": 27}


def test_a_panel_on_another_bus_is_opened_although_one_screen_would_not_come_off(
    tmp_path: Path,
) -> None:
    """The skip is per bus, because the hazard is: a Pi 3 carries two chip
    selects on SPI0 and two on SPI1, and a worker drawing on SPI1 cannot corrupt
    an init sequence clocked out of SPI0. Skipping every open over one wedged
    screen would darken panels that were never in danger."""
    clock = FakeMonotonic()
    rack = Rack(
        tmp_path,
        screens=2,
        displays=[gc9a01(bus=0, cs=0), gc9a01(bus=1, cs=0)],
        shutdown_clock=clock,
    )
    rack.supervisor.start()
    try:
        rack.supervisor._slots[1].worker = Wedged(clock, "S2")  # type: ignore[assignment]

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert len(rack.panels("RENAMED")) == 1, "it shares no wire with the screen that wedged"
        assert rack.supervisor._unavailable == []
    finally:
        rack.supervisor.stop()


def test_a_panel_sharing_a_bus_with_a_different_chip_select_is_still_skipped(
    tmp_path: Path,
) -> None:
    """Two chip selects on SPI0 is the shipped rack, and the M2 failure exactly.

    A chip select picks a device on a bus; it does not give it one. So a screen
    on SPI0.1 that will not stop drawing is a screen writing the same wire the
    init sequence for SPI0.0 would be clocked out of.
    """
    clock = FakeMonotonic()
    rack = Rack(
        tmp_path,
        screens=2,
        displays=[gc9a01(bus=0, cs=0), gc9a01(bus=0, cs=1)],
        shutdown_clock=clock,
    )
    rack.supervisor.start()
    try:
        rack.supervisor._slots[1].worker = Wedged(clock, "S2")  # type: ignore[assignment]

        rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        assert rack.panels("RENAMED") == []
        assert [entry.name for entry in rack.supervisor._unavailable] == ["RENAMED"]
    finally:
        rack.supervisor.stop()


def test_a_failed_apply_does_not_leave_the_status_file_claiming_the_pushed_config(
    tmp_path: Path,
) -> None:
    """The link nacks, and the server's answer to a nack is to push again.

    So the fingerprint must not already claim the snapshot that failed: a server
    told the push landed does not send it again, and that is a rack left half of
    each configuration with nothing anywhere disagreeing. The other direction
    corrects itself -- an under-claim is one more push.
    """
    rack = Rack(tmp_path, screens=2)
    rack.supervisor.start()
    try:
        rack.supervisor.tick()
        before = json.loads((tmp_path / "status.json").read_text())["config_fingerprint"]

        class Exploding(Wedged):
            """Raises once, on the apply's join, and behaves on the shutdown's.

            What is under test is the rollback, not what a supervisor does with a
            thread that raises twice -- and a `stop` that could not join the rack
            would hide the assertion behind a second traceback.
            """

            raised = False

            def join(self, timeout: float | None = None) -> None:
                if not Exploding.raised:
                    Exploding.raised = True
                    raise RuntimeError("a bug on the way through the retirement")

        rack.supervisor._slots[0].worker = Exploding(FakeMonotonic(), "S1")  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="a bug on the way through"):
            rack.supervisor.apply(edited(rack.raw, name="RENAMED"))

        rack.supervisor.tick()
        after = json.loads((tmp_path / "status.json").read_text())
        assert after["config_fingerprint"] == before
    finally:
        rack.supervisor.stop()


def test_the_reaper_finishes_what_it_has_been_handed_when_it_is_closed(tmp_path: Path) -> None:
    """`stop` closes it and then joins it, and a source dropped between those two
    is a `kubectl port-forward` holding a local port with nobody left to signal
    it. So closing means "finish and leave", not "leave"."""
    store = SnapshotStore()
    reaper = _Reaper(store)
    tunnels = [
        FakeTunnel(TunnelConfig(kubeconfig="/tmp/kc", namespace="n", remote_port=1, local_port=p))
        for p in (19090, 19091)
    ]
    sources = [
        _Source(
            config=IntegrationConfig.model_validate(integration_dict(name)),
            stop=threading.Event(),
            tunnels=[tunnel],
        )
        for name, tunnel in zip(("prom", "qb"), tunnels, strict=True)
    ]
    entered = threading.Event()
    released = threading.Event()

    def hold() -> None:
        entered.set()
        assert released.wait(WAIT)

    tunnels[0].on_shutdown = hold
    reaper.start()

    for source in sources:
        reaper.retire(source, drop_health=True)
    assert entered.wait(WAIT), "the reaper never took the first one down"
    reaper.close()
    released.set()
    reaper.join(WAIT)

    assert reaper.is_alive() is False
    assert [tunnel.shutdowns for tunnel in tunnels] == [1, 1], "the second one was not dropped"


# --- the watchdog and the apply ---------------------------------------------


class WatchedLock:
    """A lock that records who asked for it, so exclusion can be asserted on.

    `tick` runs on the main thread and `apply` on the link thread, so "they
    cannot overlap" is a claim about a lock -- and a claim about a lock cannot be
    proved by timing one thread against another. What can be proved is that the
    tick *asks* for the one the apply holds, and that it has not got past the
    asking while the apply still holds it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.waiting = threading.Event()
        self.acquisitions = 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.waiting.set()
        taken = self._inner.acquire(*args, **kwargs)
        self.acquisitions += 1
        return taken

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "WatchedLock":
        self.acquire()
        return self

    def __exit__(self, *exception: Any) -> None:
        self.release()


def test_a_tick_takes_the_lock_an_apply_holds(tmp_path: Path) -> None:
    """The watchdog restarts workers, and the apply opens panels; both walk `_slots`.

    `tick` -> `_check` -> `_replace` starts a brand-new thread drawing on a
    bus-mate while a fifty-command init sequence is on the wire, and it can fire
    on a *retired* slot between the revoke loop and the join loop -- handing it a
    fresh live panel on a backend about to be slept and closed. The same
    unlocked read is what lets one status file describe a rack that is half of
    each configuration: `tick` reads `_slots`, `_unavailable` and `_config` while
    `apply` swaps them.
    """
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    watched = WatchedLock(rack.supervisor._shutdown_lock)
    rack.supervisor._shutdown_lock = watched  # type: ignore[assignment]
    try:
        rack.supervisor.tick()

        assert watched.acquisitions == 1
    finally:
        rack.supervisor.stop()


def test_the_watchdog_cannot_replace_a_worker_while_an_apply_is_running(tmp_path: Path) -> None:
    """The stale heartbeat is not contrived: a worker wedged inside an SPI write
    is precisely the one `pause` cannot hold off, so the wedged bus-mate and the
    watchdog's restart of it arrive together."""
    rack = Rack(tmp_path, screens=1)
    rack.supervisor.start()
    watched = WatchedLock(rack.supervisor._shutdown_lock)
    rack.supervisor._shutdown_lock = watched  # type: ignore[assignment]
    try:
        worker = rack.supervisor.workers[0]
        worker.heartbeat = LONG_AGO

        with watched:  # stands in for an apply in flight, on the link thread
            watched.waiting.clear()
            ticking = threading.Thread(target=rack.supervisor.tick)
            ticking.start()
            assert watched.waiting.wait(WAIT), "the tick never asked for the lock"
            assert rack.supervisor.workers[0] is worker, (
                "the watchdog started a drawing worker inside the open window"
            )

        ticking.join(WAIT)
        assert ticking.is_alive() is False
        assert rack.supervisor.workers[0] is not worker, "and it is replaced once the apply is done"
    finally:
        rack.supervisor.stop()


def test_the_event_the_supervisor_publishes_is_the_one_its_own_stop_sets(tmp_path: Path) -> None:
    """A property handing out a *fresh* `Event` passes every test that asserts
    against a double: the link thread would then never notice SIGTERM, never
    refuse a push while stopping, and time out `link.join(3.0)` on every
    shutdown. So it is pinned on the real supervisor, by identity and by effect.
    """
    supervisor, _, _ = make(tmp_path, screens=1)
    published = supervisor.stop_event

    assert supervisor.stop_event is published, "read twice, and it is the same event"
    assert published.is_set() is False
    supervisor.stop()
    assert published.is_set(), "the one `stop` sets first, before it joins anything"


# --- frames -----------------------------------------------------------------


class RecordingStream:
    """A frame stream that records what a worker offered it, and encodes nothing."""

    def __init__(self) -> None:
        self.offers: list[tuple[int, Image.Image]] = []
        self.arrived = threading.Event()
        self.retired: list[set[int]] = []

    def offer(self, screen_id: int, image: Image.Image) -> bool:
        self.offers.append((screen_id, image))
        self.arrived.set()
        return True

    def retire(self, screen_ids: set[int]) -> None:
        self.retired.append(set(screen_ids))

    def wait_for(self, screen_id: int) -> bool:
        """True once this screen has offered a frame. Its own handshake per id.

        `arrived` cannot serve after a reconfiguration: it is already set by
        whatever the rack offered before the push, so waiting on it would return
        instantly and prove nothing about the worker that replaced it.
        """
        for _ in range(int(WAIT / 0.02)):
            if any(offered == screen_id for offered, _ in self.offers):
                return True
            time.sleep(0.02)
        return False


def raw_with_ids(tmp_path: Path, screens: int = 1) -> dict[str, Any]:
    raw = config_dict(tmp_path, screens)
    for index, screen in enumerate(raw["screens"], start=1):
        screen["id"] = index * 10
    return raw


def with_ids(tmp_path: Path, screens: int = 1) -> DaemonConfig:
    """The rack's configuration as a server assembles it: every screen has a row id."""
    return DaemonConfig.model_validate(raw_with_ids(tmp_path, screens))


def test_a_worker_offers_its_frames_under_the_servers_screen_id(tmp_path: Path) -> None:
    """The server routes a frame by its own row id, and task 8 refuses one for a
    screen this daemon does not own -- so the id has to travel in the snapshot."""
    frames = RecordingStream()
    supervisor, store, _ = build(with_ids(tmp_path), tmp_path, frames=frames)
    supervisor.start()
    try:
        store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
        assert frames.arrived.wait(WAIT), "no frame was ever offered"
    finally:
        supervisor.stop()

    assert {screen_id for screen_id, _ in frames.offers} == {10}


def test_a_screen_with_no_server_id_offers_no_frames(tmp_path: Path) -> None:
    """A hand-written config has never been through a server, so nothing on it
    can be addressed by a browser. It still draws, which is the whole point.

    The wait is on a frame reaching the *glass*, and it has to be: without one
    the assertion below is that nothing was offered by a worker that may not
    have drawn anything yet, which passes on a rack that is broken. It used to
    build a `threading.Event`, hang it off the display after `start` had already
    returned, and never wait on it.
    """
    frames = RecordingStream()
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=1))
    drawn = threading.Event()
    display = RecordingDisplay()
    display.on_show = drawn.set
    supervisor, store, displays = build(config, tmp_path, display=display, frames=frames)
    supervisor.start()
    try:
        store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
        assert drawn.wait(WAIT), "the panel never drew anything"
    finally:
        supervisor.stop()

    assert frames.offers == []
    assert displays["S1"].images != []


def test_a_screen_that_cannot_be_streamed_says_so_once(tmp_path: Path, caplog) -> None:
    """Otherwise the whole thing is silent in both of the cases that produce it.

    A hand-written YAML has no ids at all, and a server too old to send them
    produces the same document -- so the symptom is a browser panel that never
    fills in while the daemon draws perfectly and its log says nothing whatever.
    One line at INFO, because it is a fact about the configuration rather than a
    fault, and once because the alternative is a line per watchdog restart.
    """
    frames = RecordingStream()
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=1))
    supervisor, _, _ = build(config, tmp_path, frames=frames)
    with caplog.at_level("INFO"):
        supervisor.start()
        try:
            supervisor._frame_handler(supervisor._screens[0])
        finally:
            supervisor.stop()

    lines = [record for record in caplog.records if "cannot be streamed" in record.message]
    assert len(lines) == 1
    assert getattr(lines[0], "screen", None) == "S1"


def test_a_screen_whose_row_id_changed_offers_frames_under_the_new_one(
    tmp_path: Path,
) -> None:
    """`_unchanged` has to read `ScreenConfig.id`, and nothing else can catch it.

    `_frame_handler` captures the id in a closure when the worker is built, so a
    slot kept across a push that changed it goes on addressing frames by the old
    number for ever. The server's `_owns` then refuses every one of them, and
    the browser panel is permanently blank while the server's log blames the
    daemon -- the sticky refusal task 8's fix round removed, arriving by a
    different road.

    Comparing `config.model_dump(exclude={'id'})` passes the whole suite without
    this, which is why it is written end to end rather than against `_unchanged`:
    what has to hold is that the *frames* move, not that a helper returns False.
    """
    frames = RecordingStream()
    raw = raw_with_ids(tmp_path, screens=1)
    supervisor, store, _ = build(DaemonConfig.model_validate(raw), tmp_path, frames=frames)
    supervisor.start()
    try:
        store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
        assert frames.wait_for(10), "no frame was ever offered"

        moved = {**raw, "screens": [{**raw["screens"][0], "id": 20}]}
        supervisor.apply(DaemonConfig.model_validate(moved))
        store.put("prom", {"cpu": 43.0}, latency_ms=1.0, now=NOW)

        assert frames.wait_for(20), "the worker kept the id it was built with"
    finally:
        supervisor.stop()


def test_a_screen_a_push_deletes_stops_being_streamed(tmp_path: Path) -> None:
    """Nothing else clears it. The subscription is whole-daemon state that only a
    `frames` request replaces, so a screen removed from the configuration would
    stay in the set for the life of the connection -- and the pump would go on
    waking for an id with no worker, no panel and no row behind it."""
    frames = RecordingStream()
    raw = raw_with_ids(tmp_path, screens=2)
    supervisor, _, _ = build(DaemonConfig.model_validate(raw), tmp_path, frames=frames)
    supervisor.start()
    try:
        supervisor.apply(DaemonConfig.model_validate({**raw, "screens": [raw["screens"][0]]}))
    finally:
        supervisor.stop()

    assert frames.retired == [{20}]


def test_deleting_a_screen_that_never_had_a_row_id_retires_nothing(tmp_path: Path) -> None:
    """None is an absence and not a value, so there is nothing here to stop.

    A screen with no id could never have been streamed, and `retire` takes a set
    of screen ids -- handing it a None is a type this stream has no answer for
    and, one refactor from now, an exception on the link's own thread.
    """
    frames = RecordingStream()
    raw = raw_with_ids(tmp_path, screens=2)
    raw["screens"][1].pop("id")
    supervisor, _, _ = build(DaemonConfig.model_validate(raw), tmp_path, frames=frames)
    supervisor.start()
    try:
        supervisor.apply(DaemonConfig.model_validate({**raw, "screens": [raw["screens"][0]]}))
    finally:
        supervisor.stop()

    assert [ids for ids in frames.retired if ids] == []


def test_a_push_that_keeps_every_screen_retires_none_of_them(tmp_path: Path) -> None:
    """A worker rebuilt for any other reason is the same screen, still watched."""
    frames = RecordingStream()
    raw = raw_with_ids(tmp_path, screens=2)
    supervisor, _, _ = build(DaemonConfig.model_validate(raw), tmp_path, frames=frames)
    supervisor.start()
    try:
        renamed = {**raw, "screens": [{**raw["screens"][0], "name": "RENAMED"}, raw["screens"][1]]}
        supervisor.apply(DaemonConfig.model_validate(renamed))
    finally:
        supervisor.stop()

    assert [ids for ids in frames.retired if ids] == []


def test_a_sequence_survives_the_worker_being_replaced(tmp_path: Path) -> None:
    """The browser drops frames by `seq`. A watchdog restart is invisible to it,
    so a counter living on the worker would restart the count under a page that
    is still open and every frame after it would look stale."""
    ticker = FakeMonotonic()
    frames = FrameStream(fps=2.0, monotonic=ticker)
    frames.enable({10})
    supervisor, store, _ = build(with_ids(tmp_path), tmp_path, frames=frames)
    supervisor.start()
    try:
        store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
        first = drain(frames)
        supervisor.workers[0].heartbeat = LONG_AGO
        supervisor.tick()
        ticker.advance(10.0)
        store.put("prom", {"cpu": 43.0}, latency_ms=1.0, now=NOW)
        second = drain(frames)
    finally:
        supervisor.stop()

    assert (first, second) == (0, 1)


def test_the_status_file_carries_what_the_frame_stream_dropped(tmp_path: Path) -> None:
    """The counter is on the stream and the supervisor is the only thing that can
    see both it and the file. Without this line it is published to nobody."""
    ticker = FakeMonotonic()
    frames = FrameStream(fps=MAX_FPS, monotonic=ticker)
    frames.enable({10})
    status_path = tmp_path / "status.json"
    supervisor, _, _ = build(with_ids(tmp_path), tmp_path, status_path=status_path, frames=frames)
    for _ in range(4):
        frames.offer(10, Image.new("RGB", (240, 240)))
        ticker.advance(1.0)
    supervisor.tick()

    assert json.loads(status_path.read_text())["frames_dropped"] == 3


def test_an_unpaired_rack_reports_no_dropped_frames_rather_than_failing_to_write(
    tmp_path: Path,
) -> None:
    """There is no stream at all, and nought is the honest answer: nothing was
    dropped because nothing was ever offered."""
    status_path = tmp_path / "status.json"
    supervisor, _, _ = make(tmp_path, screens=1, status_path=status_path)

    supervisor.tick()

    assert json.loads(status_path.read_text())["frames_dropped"] == 0


def drain(frames: FrameStream) -> int:
    """The seq of the next frame the pump would send. Waits for one to exist."""
    for screen_id, image in frames.take(WAIT):
        frame = frames.encode(screen_id, image)
        assert frame is not None
        return frame.seq
    raise AssertionError("no frame was offered")


# --- what an ordinary push costs on a rack with nothing wrong with it --------


def test_an_ordinary_apply_does_not_wait_out_a_healthy_workers_heartbeat_floor(
    tmp_path: Path, watch_parks: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The case the overrun test manufactures with a wedged worker is the case a
    healthy rack was in on every single push.

    A worker that is awake, unfaulted and drawing parks in
    `SnapshotStore.wait_for_change`, and until `wake` existed the slot's stop
    event did not notify that condition -- so `_retire` set the flag and then
    joined against a worker that would not look at it until its five-second
    floor elapsed. Measured on a one-screen virtual rack, four consecutive
    applies: 2.008s, 3.003s (overran, worker abandoned), 0.001s, 1.019s. The
    only reason it is not five seconds is that `APPLY_BUDGET` cuts it off at
    three, which is the whole of it -- three seconds out of the daemon's
    ten-second shutdown budget, three seconds in which the link thread reads
    nothing and beats nothing, and an ERROR saying the apply went wrong on a
    push where nothing did.

    Nothing here is a race the test can lose. `watch_parks` reports from inside
    the store's lock, so the worker is provably parked before the apply starts:
    a test that merely waited for a frame would sometimes catch it between
    `tick` and `_wait`, where the stop event is read anyway and the defect is
    invisible.
    """
    rack = Rack(tmp_path, screens=1)
    condition = watch_parks(rack.store)
    rack.supervisor.start()
    retiring = rack.supervisor.workers[0]
    try:
        condition.await_parks(1)
        started = time.monotonic()
        with caplog.at_level("ERROR"):
            rack.supervisor.apply(edited(rack.raw, name="renamed"))
        elapsed = time.monotonic() - started
        # Read here and not after the `finally`, which closes the store and
        # would release the worker itself -- the question is whether *the apply*
        # joined it or gave up on it.
        joined = not retiring.is_alive()
    finally:
        rack.supervisor.stop()

    assert joined, "the retired worker was abandoned rather than joined"
    assert elapsed < 1.0, f"an ordinary push cost {elapsed:.3f}s of the link thread"
    assert not [record for record in caplog.records if "overran" in record.getMessage()], (
        "the overrun ERROR fired on a rack with nothing wrong with it"
    )
    assert [worker.screen_name for worker in rack.supervisor.workers] == ["renamed"]


def test_an_apply_that_retires_nothing_does_not_wake_the_rack(tmp_path: Path) -> None:
    """`wake` is cheap and not free: every worker re-tests its predicate.

    An apply that only *adds* a screen retires no slot and sets no stop event,
    so there is nothing for the kept workers to re-read -- and waking them
    anyway would be a lock and four predicate calls per push for no news.
    """
    rack = Rack(tmp_path, screens=1)
    woken: list[int] = []
    rack.store.wake = lambda: woken.append(1)  # type: ignore[method-assign]
    rack.supervisor.start()
    try:
        raw = config_dict(tmp_path, screens=2)
        raw["screens"][0] = rack.raw["screens"][0]
        rack.supervisor.apply(DaemonConfig.model_validate(raw))

        assert woken == []
        assert len(rack.supervisor.workers) == 2
    finally:
        rack.supervisor.stop()


# --- identify, as a command off the link rather than as a CLI subcommand -----


def running_rack(
    tmp_path: Path, screens: int = 2
) -> tuple[Supervisor, dict[str, RecordingDisplay]]:
    """A real rack with real workers on virtual panels, and server row ids on its
    screens -- which is what a `Command` addresses one by.

    Settled before it is handed over: every worker has drawn its first frame and
    is parked on the store for a whole heartbeat floor afterwards, so a test
    that counts frames is counting its own doing. Without this the first
    `connecting` frame lands *after* the count is taken, and "the other panel
    was left alone" fails on a rack that was merely still starting.
    """
    supervisor, _, displays = build(with_ids(tmp_path, screens), tmp_path)
    supervisor.start()
    for worker in supervisor.workers:
        deadline = time.monotonic() + WAIT
        while not worker.renders and time.monotonic() < deadline:
            time.sleep(0.005)
        assert worker.renders, f"{worker.screen_name} never drew its first frame"
    return supervisor, displays


def digits(display: RecordingDisplay) -> int:
    """How many frames this panel has taken. The identify scene is one of them."""
    return len(display.images)


def test_identify_paints_every_panel_with_its_own_ordinal(tmp_path: Path) -> None:
    """The whole point of the command: somebody standing at the rack maps a
    physical panel to a line of the configuration. `screen_id` of None is the
    ordinary case -- one press, every panel numbered."""
    supervisor, displays = running_rack(tmp_path, screens=2)
    try:
        before = {name: digits(display) for name, display in displays.items()}

        painted = supervisor.identify(None)

        assert painted == 2
        assert [worker.current_scene for worker in supervisor.workers] == ["identify"] * 2
        assert all(digits(displays[name]) > count for name, count in before.items())
    finally:
        supervisor.stop()


def test_identify_addresses_one_panel_by_its_server_row_id(tmp_path: Path) -> None:
    """`Command.screen_id` is the server's row id and nothing else identifies a
    screen: `name` and `position` are unique over nothing in the schema."""
    supervisor, displays = running_rack(tmp_path, screens=2)
    try:
        before = {name: digits(display) for name, display in displays.items()}

        painted = supervisor.identify(20)

        assert painted == 1
        assert [worker.current_scene for worker in supervisor.workers] == ["connecting", "identify"]
        assert digits(displays["S1"]) == before["S1"], "the other panel was left alone"
    finally:
        supervisor.stop()


def test_identify_for_a_screen_this_rack_does_not_have_paints_nothing(tmp_path: Path) -> None:
    """Nought is the honest answer and the caller reports it. Painting the whole
    rack instead would be `identify` on somebody else's glass, which is the
    failure `not_a_flag` exists for reached by a different road."""
    supervisor, displays = running_rack(tmp_path, screens=2)
    try:
        before = {name: digits(display) for name, display in displays.items()}

        assert supervisor.identify(999) == 0
        assert all(digits(displays[name]) == count for name, count in before.items())
    finally:
        supervisor.stop()


def test_identify_on_a_stopping_daemon_touches_nothing(tmp_path: Path) -> None:
    """The panels are slept and their serial devices closed by then, and on a
    GC9A01 a write to one that has been torn down is a write to nothing. The
    same check `apply` makes on the other side of the same handover."""
    supervisor, displays = running_rack(tmp_path, screens=2)
    supervisor.stop()
    before = {name: digits(display) for name, display in displays.items()}

    assert supervisor.identify(None) == 0
    assert all(digits(displays[name]) == count for name, count in before.items())


def test_one_wedged_panel_does_not_cost_the_link_thread_or_the_other_panels(
    tmp_path: Path,
) -> None:
    """It runs on the thread that reads the socket and consults the stop event.

    A worker wedged inside an SPI write never gives its tick lock up, so an
    unbounded acquire here parks the link for ever -- and a deadline shared with
    whoever went first would leave the last panel nothing, which is the defect
    `BUS_GUARD_BUDGET` exists for, met again.
    """
    supervisor, displays = running_rack(tmp_path, screens=2)
    wedged = supervisor.workers[0]
    wedged._lock.acquire()
    try:
        started = time.monotonic()
        painted = supervisor.identify(None)
        elapsed = time.monotonic() - started

        assert painted == 1, "the panel that was free still took its digit"
        assert displays["S2"].images, "the second panel was given no time at all"
        assert elapsed < IDENTIFY_BUDGET
    finally:
        wedged._lock.release()
        supervisor.stop()


def test_the_digit_on_the_glass_is_the_screens_position(tmp_path: Path) -> None:
    """Which number a panel shows is the whole of what the command is for.

    The printed map `ors-daemon identify` produces is keyed by `position`, and
    two ways of numbering the same rack make that map useless -- so this asserts
    the pixels rather than the call. The row id is the tempting thing to reach
    for here, because it is what addressed the screen; it is not what anybody at
    the rack is reading off the front of the config.
    """
    supervisor, displays = running_rack(tmp_path, screens=2)
    try:
        supervisor.identify(None)
    finally:
        supervisor.stop()

    system = system_scenes()["identify"]
    for name, position in (("S1", 1), ("S2", 2)):
        expected = render_scene(system, RenderContext(data={"params": {"ordinal": str(position)}}))
        assert displays[name].images[-1].tobytes() == expected.tobytes(), name


def test_identify_steps_over_a_panel_that_has_no_worker_yet(tmp_path: Path) -> None:
    """A slot with an open panel and nothing started on it is a real state: a
    stop that landed between `start`'s two phases leaves one, and so does a
    worker that would not fork. There is no thread to draw the digit and no lock
    to take, and an `AttributeError` out of here reaches the link thread."""
    supervisor, displays = running_rack(tmp_path, screens=2)
    try:
        supervisor._slots[0].worker = None
        before = digits(displays["S1"])

        assert supervisor.identify(None) == 1

        assert digits(displays["S1"]) == before
        assert displays["S2"].images
    finally:
        supervisor.stop()
