"""The threads, and the order they are brought up and taken down in.

Everything else in the daemon is a component with one job; this is where they
meet, and every decision here is about a seam rather than about a feature.

*The supervisor owns the panels, the workers only draw on them.* A backend is
built once, handed to a worker through a revocable `_Panel`, and slept and
closed by the supervisor after that worker has stopped. The lease exists because
the two halves of that sentence are otherwise in conflict: `ScreenWorker.run`
closes its display on the way out, so a supervisor that waited for the thread
and then slept the panel would be writing a command to a device it had already
torn down -- and a rack whose panels stay lit after shutdown is the one symptom
this whole path exists to prevent.

*Shutdown is ordered, and the order is the point.* Stop event, release the
workers parked on the snapshot, join every thread, then sleep and close the
panels. Nothing touches a backend while a thread that might also touch it is
still running. All of it inside one deadline (`SHUTDOWN_BUDGET`), because the
number of threads is a fact about the config and systemd's patience is not.

*The watchdog watches screen workers and nothing else.* A poller backs off up to
60s between polls, which is longer than the watchdog's timeout: watching one
would restart a healthy poller in the middle of its backoff.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ors_schema.daemon import DaemonConfig, IntegrationConfig, ScreenConfig, TunnelConfig
from PIL import Image

from ors_daemon.clock import Clock
from ors_daemon.config import ResolvedScreen, config_fingerprint, system_scenes
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.integrations import UrlProvider, build_integration
from ors_daemon.poller import Poller
from ors_daemon.screen import _NIGHT_PARK_CHUNK, ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.status import UnavailableScreen, build_status, write_status
from ors_daemon.tunnel import Tunnel

log = logging.getLogger(__name__)

DisplayFactory = Callable[[ScreenConfig, str], DisplayBackend]
PollerFactory = Callable[[IntegrationConfig, UrlProvider | None], Poller | None]
TunnelFactory = Callable[[TunnelConfig, threading.Event], Tunnel]

DEFAULT_WATCHDOG_TIMEOUT = _NIGHT_PARK_CHUNK * 1.5
"""How long a screen worker may go without stamping its heartbeat, in seconds.

Derived from the worker's night park rather than written as 30.0 beside a
comment saying "keep this above 20.0", because the relationship is real and a
comment cannot enforce it: a sleeping worker parks in `_NIGHT_PARK_CHUNK`
chunks *so that* its heartbeat keeps moving, and a timeout at or below that
value restarts all four panels every chunk, all night -- ~960 spurious restarts
before morning, each one logged as a fault, describing a rack that is fine.

Half as much again is the margin, and it is deliberately not tighter: the
longest legitimate gap between two heartbeats is a whole park chunk plus a tick,
and a Pi 3B+ pushing four panels is not a machine whose scheduling latency is
worth betting a restart on. It is not much looser either -- a genuinely wedged
panel is still noticed inside half a minute, and every second past that is a
second of a rack showing a number that stopped being true.
"""

_MAX_RESTARTS = 3
"""How many times the watchdog restarts one screen before leaving it alone.

A restart abandons the wedged thread -- there is no way to kill a Python thread
-- so an uncapped watchdog answers a permanently wedged panel with a new thread
every timeout: ~2,900 a day, each one holding a render context, and each one
wedging in the same place for the same reason. Three is enough to ride out
something transient and few enough that the fourth failure is a fact about the
hardware, which is what the log line then says.
"""

SHUTDOWN_BUDGET = 10.0
"""How long `stop` may spend getting the threads to leave, in seconds -- in total.

One deadline shared by every wait, rather than a timeout each, and that is the
difference between a bound and a hope. How many threads there are is what the
config says it is: four panels, a poller and a tunnel today, and more the moment
a rack grows a screen or M5 adds its second integration. A per-thread timeout is
therefore multiplied by a number *this module does not choose* -- six threads at
5s each is 30s, which was `TimeoutStopSec` exactly, and the tunnel's own
SIGTERM-then-SIGKILL wait put the measured worst case at ~40s. Past that systemd
sends SIGKILL, `stop` never reaches the panels, and four GC9A01s stay lit until
the rack is power-cycled -- the one outcome this whole path exists to prevent.

Ten seconds is what the whole shutdown costs at worst, whatever the config holds.
The shipped unit's `TimeoutStopSec=30` is derived from this number and names it,
so the two cannot drift apart silently; the margin between them is for what the
deadline deliberately does not cover -- sleeping and closing the panels, which
happens when the deadline has expired just as surely as when it has not.

A healthy shutdown spends none of it. Every thread is parked on the stop event or
on the store, and both are released before the first join, so the joins return in
milliseconds. A thread that has already left is joined instantly whatever is left
of the budget, which is why one wedged thread spending the lot costs a healthy
one nothing: they have all been leaving since the first line of `stop`.
"""


def _deadline(budget: float, monotonic: Callable[[], float]) -> Callable[[], float]:
    """A function answering how much of `budget` is left. Never negative.

    Passed around rather than recomputed from a stored end time, so every wait in
    a shutdown provably reads the same deadline: there is only one, and it is
    taken once.
    """
    end = monotonic() + budget

    def remaining() -> float:
        return max(0.0, end - monotonic())

    return remaining


def _url_of(tunnel: Tunnel) -> UrlProvider:
    """A URL provider bound to one tunnel, reading it at call time.

    A factory rather than a closure written inline, and that is not a style
    choice: `tunnel` is then a parameter of *this* call and cannot be the loop
    variable of whatever ends up iterating over the integrations, which is the
    bug that gives every integration the last tunnel's port. Reading rather than
    capturing `base_url` is the other half -- a tunnel is allowed to move
    underneath a poller between two polls, which is why the integration contract
    takes a callable at all.
    """

    def provider() -> str:
        return tunnel.base_url

    return provider


class _Panel:
    """One backend, leased to one worker, revocable by the supervisor.

    Two problems, one object. A worker closes its display when it leaves, which
    would take the panel down before the supervisor could put it to sleep; and a
    worker replaced by the watchdog is a thread that is still alive and still
    holding a backend, which the replacement is now also writing to. Both are
    solved by never handing a worker the backend itself: `close` ends the lease
    instead of the panel, and a revoked lease forwards nothing.

    It cannot stop a call already inside the backend -- a worker wedged in the
    middle of an SPI write is wedged there whatever this says -- so it narrows
    the window rather than closing it. Nothing available to a Python program
    closes it entirely.
    """

    def __init__(self, backend: DisplayBackend) -> None:
        self.backend = backend
        self.live = True

    def revoke(self) -> None:
        """Detach the worker from the panel. Idempotent, and one-way."""
        self.live = False

    def show(self, image: Image.Image) -> None:
        if self.live:
            self.backend.show(image)

    def sleep(self) -> None:
        if self.live:
            self.backend.sleep()

    def wake(self) -> None:
        if self.live:
            self.backend.wake()

    def close(self) -> None:
        # Deliberately not `backend.close()`. The supervisor still has to sleep
        # this panel, and on a GC9A01 a close has already torn down the serial
        # device every later command would travel over.
        self.revoke()


@dataclass
class _Slot:
    """One panel and whoever is currently drawing on it.

    Keyed by nothing: a screen's name is not unique -- the schema does not make
    it so, and uniqueness is a rule about the set rather than about any one
    screen -- so a dict of panels by name would quietly collapse two screens
    that shared one into a single panel with two workers.
    """

    screen: ResolvedScreen
    panel: _Panel
    worker: ScreenWorker
    restarts: int = 0
    shut_down: bool = False
    """Claimed by whichever `stop` reaches this slot first, before it does the
    work. It is what makes shutdown repeatable without being repeated: `stop`
    runs more than once by design, and a panel may only be slept and closed
    once."""


class Supervisor:
    """Starts threads, watches their heartbeats, and shuts the rack down cleanly."""

    def __init__(
        self,
        config: DaemonConfig,
        screens: list[ResolvedScreen],
        store: SnapshotStore,
        clock: Clock,
        status_path: Path,
        display_factory: DisplayFactory | None = None,
        poller_factory: PollerFactory | None = None,
        tunnel_factory: TunnelFactory | None = None,
        watchdog_timeout: float = DEFAULT_WATCHDOG_TIMEOUT,
        shutdown_budget: float = SHUTDOWN_BUDGET,
        shutdown_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if watchdog_timeout <= _NIGHT_PARK_CHUNK:
            raise ValueError(
                f"a watchdog timeout of {watchdog_timeout}s restarts every sleeping panel: "
                f"a worker in night mode parks for up to {_NIGHT_PARK_CHUNK}s at a time"
            )
        self._config = config
        self._config_fingerprint = config_fingerprint(config)
        self._screens = screens
        self._store = store
        self._clock = clock
        self._status_path = Path(status_path)
        self._display_factory = display_factory or (
            lambda screen, name: build_display(screen.display, name)
        )
        self._poller_factory = poller_factory
        self._tunnel_factory = tunnel_factory or (lambda cfg, stop: Tunnel(config=cfg, stop=stop))
        self._watchdog_timeout = watchdog_timeout
        self._shutdown_budget = shutdown_budget
        # The only injectable clock of the two this class reads, and deliberately
        # so: the watchdog below compares against a heartbeat the *workers* stamp
        # with `time.monotonic`, and a supervisor measuring that against anything
        # else is a watchdog that restarts a rack which is perfectly fine. This
        # one is measured against nothing but itself, so a test can prove the
        # shutdown bound without spending it.
        self._shutdown_clock = shutdown_clock
        # Never `_stop`: `threading.Thread._stop` is a real method that `join`
        # calls, and this event is handed to three classes that are threads.
        # They would each have to rename it back, so it is named right here.
        self._stop_event = threading.Event()
        self._slots: list[_Slot] = []
        self._unavailable: list[UnavailableScreen] = []
        self._shutdown_lock = threading.RLock()
        self._stopped = False
        self._shutdown_deadline: Callable[[], float] | None = None
        self._started_at = clock()

        self.pollers: list[Poller] = []
        self.tunnels: list[Tunnel] = []

    @property
    def workers(self) -> list[ScreenWorker]:
        """The worker currently drawing each panel, in panel order.

        Derived rather than stored because the watchdog replaces workers and the
        panel they draw on outlives them; a second list would be a second thing
        to keep in step. A replaced worker is not here -- it is abandoned, not
        tracked -- which is also what keeps it out of the status file.
        """
        return [slot.worker for slot in self._slots]

    def start(self) -> None:
        """Bring the rack up: tunnels and pollers first, then panels.

        Interruptible, because on the rack it is interrupted: the CLI arms
        SIGTERM before this runs, the README's own bring-up step is "start it,
        then Ctrl-C", and systemd will `stop` a unit it is still starting.
        Every lap checks whether that has happened -- opening a GC9A01 is a
        hardware reset and a fifty-command init sequence, and doing three more
        of those to close all three immediately is a slower shutdown for
        nothing. What makes it *correct* rather than merely quick is in
        `_start_screen` and `stop`: this check is an optimisation and is not
        load-bearing, because a signal can always land the instant after it.
        """
        for integration_config in self._config.integrations:
            if self._stopped:
                return
            # Registered before the poller runs, so a screen depending on an
            # integration that has not answered yet shows `connecting` rather
            # than failing to find it at all.
            self._store.register(integration_config.name)
            self._start_integration(integration_config)
        for screen in self._screens:
            if self._stopped:
                return
            self._start_screen(screen)

    def tick(self) -> None:
        """One watchdog pass and one status write."""
        if self._stopped:
            # A tick interrupted by a SIGTERM-installed `stop()` would otherwise
            # resume here and restart a wedged worker onto a closed backend. The
            # replacement happens not to draw -- `ScreenWorker.run` re-checks its
            # stop event before its first tick -- but that is another module's
            # invariant to keep, and this line makes the guarantee local.
            return
        now = time.monotonic()
        for slot in self._slots:
            self._check(slot, now)
        try:
            write_status(
                self._status_path,
                build_status(
                    started_at=self._started_at,
                    now=self._clock(),
                    config_schema_version=self._config.version,
                    # Computed once, at construction: the config cannot change
                    # under a running supervisor in M2, and hashing it on every
                    # tick would put a serialisation of the whole document on the
                    # once-a-second path for an answer that cannot have moved.
                    config_fingerprint=self._config_fingerprint,
                    # Every configured screen, not only the ones that came up.
                    screens=[*self.workers, *self._unavailable],
                    snapshot=self._store.read(),
                ),
            )
        except (OSError, ValueError) as exc:
            # `write_status` raises deliberately: the module reports, the loop
            # decides. A read-only /run or a full disk must not darken the rack
            # -- a status file is a nicety, and the panels are the product.
            #
            # `ValueError` is not the exotic half of that pair. `--status /`,
            # `--status .` and an empty one are paths with no filename, and
            # deriving the temporary file beside the target raises `ValueError`
            # rather than `OSError` for every one of them -- which escaped here,
            # escaped `run_forever` and `main`, and under `Restart=always` with
            # `StartLimitIntervalSec=0` became an infinite five-second restart
            # loop re-running the GC9A01 init sequence on four panels. A path
            # this daemon cannot write to is a warning a second, whatever kind
            # of wrong it is.
            log.warning(
                "could not write the status file",
                extra={"path": str(self._status_path), "error": str(exc)},
            )

    def run_forever(self, interval: float = 1.0) -> None:
        """Start, tick until stopped, and shut down whatever happens.

        `start` is inside the `try` because a startup that fails partway has
        already opened backends and started threads: a panel left lit, and a
        `kubectl port-forward` child orphaned holding its local port until the
        process exits. Thread exhaustion on a Pi and a template-packaging
        failure both land there -- after the first backend is open.
        """
        try:
            self.start()
            while not self._stop_event.is_set():
                self.tick()
                self._stop_event.wait(interval)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop every thread, then put every panel to sleep. Repeatable, never repeated.

        The order is the whole method. The stop event goes first so nothing
        starts another lap; the store is released because a screen worker parks
        on the snapshot's condition rather than on the event, and would
        otherwise sit there for a whole heartbeat floor -- long enough for the
        join below to expire while the worker is still alive and still drawing.
        Only once every thread has been joined are the leases revoked and the
        panels slept and closed, so no backend is ever touched by two threads.

        Every join, and the tunnel teardown between them, shares one deadline --
        one per *shutdown*, not one per call, so the two calls the paragraph
        below describes cannot spend the budget twice over. The panels are slept
        whether or not it expires: `SHUTDOWN_BUDGET` says why, and what the
        number is derived from. What the deadline buys the threads it cannot
        join is nothing -- but a lease is revoked before its panel is slept, so
        the window a wedged worker could write into is the same narrow one a
        joined worker leaves behind.

        It runs more than once by design: the CLI installs it as the SIGTERM
        handler *and* `run_forever` calls it from its `finally`, and an
        impatient operator sends the signal twice. So every step is written to
        be safe to repeat -- setting an event, closing a closed store, joining a
        finished thread and telling a stopped tunnel to stop all cost nothing --
        and the one step that is *not* repeatable, sleeping and closing a panel,
        is claimed per slot before it is done.

        That is a stronger promise than "returns early the second time", and the
        difference is a real failure. This can be called *while `start` is still
        opening panels*: the handler runs on the thread inside `start`, so a
        re-entrant lock protects nothing, and an early return would leave every
        panel opened after the signal lit, with its serial device open, for as
        long as the process lived. Draining whatever is in `_slots` on every
        call is what closes that window -- together with `_start_screen`
        recording a slot *before* it reads `_stopped`, so no panel can exist
        outside this list at a moment when a stop could be running.
        """
        with self._shutdown_lock:
            # Claimed first, under the lock, because `tick` and `_start_screen`
            # both read it to decide whether the rack is still coming up.
            self._stopped = True
            self._stop_event.set()
            # Releases every `wait_for_change`, which is where a screen worker
            # spends nearly all of its time. `ScreenWorker.run` says this is the
            # supervisor's to arrange, since the supervisor is what owns both.
            self._store.close()

            # Taken after the event is set and the store is closed, so the
            # threads have been leaving for as long as this has been running --
            # and taken once. A second `stop` (SIGTERM twice, or the handler and
            # then `run_forever`'s `finally`) inherits what is left of the first
            # one's deadline rather than opening a second budget: a poller
            # nobody can join would otherwise cost its ten seconds again on
            # every call, which is the multiplication this exists to remove.
            if self._shutdown_deadline is None:
                self._shutdown_deadline = _deadline(self._shutdown_budget, self._shutdown_clock)
            remaining = self._shutdown_deadline

            for slot in self._slots:
                if slot.shut_down:
                    # Joined and slept by an earlier call; its worker is gone
                    # and its panel is off. Re-joining would be free, but
                    # re-warning about a wedged one would not be.
                    continue
                slot.worker.join(remaining())
                if slot.worker.is_alive():
                    log.warning(
                        "worker did not stop; sleeping its panel anyway",
                        extra={"screen": slot.worker.screen_name},
                    )
            for poller in self.pollers:
                poller.join(remaining())
            for tunnel in self.tunnels:
                # Sets the stop event again on its way past, which is harmless
                # and not worth working around: it is what makes `shutdown`
                # usable on a tunnel whose own loop is still running. Repeated
                # on a later call for the same reason, and because a tunnel
                # started after an earlier one -- `start` was interrupted
                # partway -- would otherwise keep its `kubectl` child.
                #
                # The deadline goes into `shutdown` as well as into the join
                # after it, because taking a tunnel down is not a wait on a
                # thread: it signals `kubectl` and waits for it to die, which
                # is where the other ten seconds of the worst case used to be.
                tunnel.shutdown(timeout=remaining())
                tunnel.join(remaining())

            for slot in self._slots:
                if slot.shut_down:
                    continue
                # Claimed before the work and under the lock, so a signal
                # landing inside `_shut_down_panel` -- on this very thread --
                # re-enters here and skips what is already in hand instead of
                # sleeping and closing it a second time. On a GC9A01 a second
                # close writes to a device that has been torn down.
                slot.shut_down = True
                # Abandoned workers are not joined -- a wedged one never would
                # be -- and they do not need to be: their lease is revoked, so
                # the only thread that can still reach this panel is this one.
                slot.panel.revoke()
                self._shut_down_panel(slot.panel.backend, slot.screen.config.name)

    def _shut_down_panel(self, backend: DisplayBackend, name: str) -> None:
        """Blank one panel and let go of it. Raises nothing.

        Takes a backend rather than a slot because a panel can need blanking
        before it has one: see `_start_screen`, where a worker that would not
        start leaves an open panel with nowhere yet to record it.

        Each call is guarded separately: a `sleep` that fails is a panel that
        will stay lit, and a `close` skipped because of it is a serial device
        left open for as long as the process lives.
        """
        for action, call in (("sleep", backend.sleep), ("close", backend.close)):
            try:
                call()
            except Exception as exc:
                log.warning(
                    "could not shut a panel down cleanly",
                    extra={"screen": name, "action": action, "error": str(exc)},
                )

    def _check(self, slot: _Slot, now: float) -> None:
        """Restart the slot's worker if its heartbeat has stopped moving."""
        if slot.restarts > _MAX_RESTARTS:
            return
        heartbeat = slot.worker.heartbeat
        # Nought means "has not ticked yet", not "last ticked at the epoch".
        # `heartbeat` is monotonic and therefore measured from boot, so reading
        # nought as a timestamp makes every worker overdue the moment it starts.
        if not heartbeat or now - heartbeat <= self._watchdog_timeout:
            return

        slot.restarts += 1
        name = slot.screen.config.name
        if slot.restarts > _MAX_RESTARTS:
            log.error(
                "screen wedged again after every restart; leaving it alone",
                extra={"screen": name, "restarts": _MAX_RESTARTS},
            )
            return
        log.error("worker wedged, restarting", extra={"screen": name, "attempt": slot.restarts})
        self._replace(slot)

    def _replace(self, slot: _Slot) -> None:
        """Hand the slot a new worker on the panel that is already open.

        The backend is reused rather than rebuilt. Rebuilding would open a
        second device on a bus the wedged worker may be halfway through a write
        on, and re-run the GC9A01 init sequence underneath it; the lease is what
        makes reuse safe, since revoking it is what stops the old worker from
        writing again.
        """
        slot.panel.revoke()
        panel = _Panel(slot.panel.backend)
        worker = self._make_worker(slot.screen, panel)
        # Recorded only once it is running. `Thread.join` on a thread that was
        # never started raises, so a slot holding an unstarted worker would turn
        # a machine too short of memory to fork into a shutdown that leaves the
        # panels lit -- the one thing shutdown exists to do.
        worker.start()
        slot.panel = panel
        slot.worker = worker

    def _start_integration(self, integration_config: IntegrationConfig) -> None:
        url_provider: UrlProvider | None = None
        if integration_config.tunnel is not None:
            tunnel = self._tunnel_factory(integration_config.tunnel, self._stop_event)
            tunnel.start()
            self.tunnels.append(tunnel)
            url_provider = _url_of(tunnel)

        poller = (
            self._poller_factory(integration_config, url_provider)
            if self._poller_factory is not None
            else Poller(
                integration=build_integration(integration_config, url_provider),
                store=self._store,
                interval=integration_config.poll_interval,
                stop=self._stop_event,
                clock=self._clock,
            )
        )
        if poller is None:
            return
        poller.start()
        self.pollers.append(poller)

    def _start_screen(self, screen: ResolvedScreen) -> None:
        name = screen.config.name
        try:
            backend = self._display_factory(screen.config, name)
        except Exception as exc:
            # One missing panel is three working ones plus a log line, not a
            # dark rack: the backends are independent, and so are the screens.
            # But it is recorded as well as logged. The startup line is written
            # once and never repeated, and on a headless rack the status file is
            # the diagnostic -- without this, four screens with two dead panels
            # report exactly what a healthy two-screen rack reports.
            log.error("screen unavailable", extra={"screen": name, "error": str(exc)})
            self._unavailable.append(UnavailableScreen(name=name, reason=str(exc)))
            return
        panel = _Panel(backend)
        worker = self._make_worker(screen, panel)
        try:
            # Started before the slot records it, for the reason `_replace` gives.
            worker.start()
        except BaseException:
            # The same class of bug as the signal landing mid-start below, one
            # line further along, and with the same cost. Between the backend
            # opening and the slot existing there is a panel nothing else can
            # reach: `stop` walks `_slots`, so a `RuntimeError("can't start new
            # thread")` from a Pi under memory pressure would leave this one lit,
            # its init sequence ended in DISPLAY_ON, with its serial device open
            # for as long as the process lived. It is still in hand here, so it
            # is blanked here. The lease is revoked first because the worker may
            # or may not have got as far as running; revoking is what settles it.
            panel.revoke()
            log.error("could not start a worker; blanking its panel", extra={"screen": name})
            self._shut_down_panel(backend, name)
            raise
        self._slots.append(_Slot(screen=screen, panel=panel, worker=worker))
        # Recorded first, *then* the flag is read -- never the other way round.
        # `_stopped` only ever goes from false to true, so a stop that has begun
        # at any point up to this line is still visible here, while a stop that
        # begins after it finds the slot already in the list. Between the two
        # there is no instant at which this panel is open and unreachable by a
        # shutdown, which is the entire property: the check-then-act ordering,
        # not the lock, is what makes it race-free, because the stop that
        # matters most is a signal handler running on this very thread and no
        # lock excludes that.
        if self._stopped:
            log.info("stopped while opening a panel; shutting it down", extra={"screen": name})
            self.stop()

    def _make_worker(self, screen: ResolvedScreen, panel: _Panel) -> ScreenWorker:
        return ScreenWorker(
            screen=screen,
            store=self._store,
            display=panel,
            system=system_scenes(),
            night=self._config.night,
            stop=self._stop_event,
            clock=self._clock,
        )
