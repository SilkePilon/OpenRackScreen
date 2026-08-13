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

*A reconfiguration is a diff, not a restart.* `apply` is the only thing here
that deliberately stops a panel that is working, so it stops as few as it can:
a screen whose resolved configuration is unchanged keeps its worker, keeps its
SPI device and does not blink. It resolves the new config before it touches the
old one, it bounds how long it waits (`APPLY_BUDGET`), and it never leaves a
rack that is half of each configuration.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ors_schema.daemon import (
    DaemonConfig,
    IntegrationConfig,
    NightWindow,
    ScreenConfig,
    TunnelConfig,
)
from PIL import Image

from ors_daemon.clock import Clock
from ors_daemon.config import ResolvedScreen, config_fingerprint, resolve_screens, system_scenes
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


APPLY_BUDGET = 3.0
"""How long `apply` may spend *waiting* for the rack it is replacing, in seconds.

One deadline for the whole apply, for the reason `SHUTDOWN_BUDGET` gives: how
many screens a snapshot holds is the server's decision, so a timeout each is
multiplied by a number this module does not choose.

Three seconds, and the number is bounded from both ends.

The ceiling is a shutdown. `apply` runs synchronously as the link's
`on_snapshot`, on the thread that reads the socket and the thread that consults
the stop event, so every second it spends is a second a SIGTERM waits -- spent
out of `SHUTDOWN_BUDGET`'s ten for the *whole* daemon, alongside the link's
`RECV_TIMEOUT_S` of 1.0 and `CLOSE_TIMEOUT_S` of 2.0. Those three come to six,
which leaves four for `stop` itself to join four workers, a poller and a tunnel
and then sleep the panels. Measured on the draft: a three-second apply delayed
`join()` by the full three seconds, so this is not a bound on paper. Past the
budget systemd sends SIGKILL before `Supervisor.stop` sleeps the panels, and
four GC9A01s stay lit until somebody pulls the power.

The floor is a real reconfiguration. A worker asked to stop is parked on its own
event or on the store and returns in milliseconds; the only thing that spends
this is a worker wedged inside an SPI write, which is a screen already lost.
Three seconds is enough for several of those to be given up on in turn and short
enough that a whole rack's worth still fits.

What it bounds is the waiting, and deliberately nothing else. Overrunning does
not abandon the apply: a worker that will not stop has its lease revoked and its
panel slept and closed anyway, and the replacement rack is opened and started in
full. Abandoning halfway is four panels showing half of each configuration, with
an ack that says otherwise -- which is worse than being late.
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


def _unchanged(
    running: ResolvedScreen,
    pushed: ResolvedScreen,
    running_night: NightWindow,
    pushed_night: NightWindow,
) -> bool:
    """Whether a running screen and a pushed one are the same screen.

    "The same" has to mean *everything a `ScreenWorker` reads*, because anything
    it reads and this misses is an edit the server believes has landed and the
    glass disagrees with. That is:

    - `config`, which is the whole `ScreenConfig` and therefore the display
      wiring (a different SPI device is a different panel), the rotation and
      hflip the frame is transposed by, the position, the name, the raw params
      and the screen's own night override. Compared as one model rather than
      field by field, so a field added to the schema is covered by default
      instead of silently falling out of the diff.
    - `scenes` and `params`, which is what a *template* edit changes. The name
      of the template is in `config`, but its contents are not: a snapshot that
      redraws `ring-gauge` names the same template on the same screen, and
      comparing the config alone would leave the old layout on the glass.
    - `depends_on`, which is what decides whether a screen shows `connecting`.
      It is derived from the scenes, the params and the *integration names*, so
      a screen can change here without changing anything above it.
    - the effective night window, which is the one comparison that cannot be
      made between the two `ResolvedScreen`s alone: the rack's window lives on
      `DaemonConfig`, and the worker takes it at construction and never re-reads
      it. Hence the two `night` arguments -- what the running worker was built
      with, and what the push asks for. Without it a rack told to sleep an hour
      earlier sleeps at the old hour, and nothing anywhere says why.

    What it deliberately does not cover, recorded rather than hidden:

    - the integrations themselves. A screen keeps its worker when a Prometheus
      URL moves under it, because `apply` cannot restart a poller or a tunnel
      inside its budget -- see `apply`, which logs that case.
    - `DaemonConfig.timezone`. The clock is built once, in `__main__`, and is
      shared by every worker and poller; swapping it under a running rack is a
      change to a component this class is handed rather than one it owns.
    - anything that compares equal but behaves differently, which for a
      `Template` means nothing today: `Scene` is a pydantic model compared by
      value all the way down.
    """
    return (
        running.config == pushed.config
        and running.scenes == pushed.scenes
        and running.params == pushed.params
        and running.depends_on == pushed.depends_on
        and (running.config.sleep_override or running_night)
        == (pushed.config.sleep_override or pushed_night)
    )


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
    worker: ScreenWorker | None = None
    restarts: int = 0
    shut_down: bool = False
    """Claimed by whichever `stop` reaches this slot first, before it does the
    work. It is what makes shutdown repeatable without being repeated: `stop`
    runs more than once by design, and a panel may only be slept and closed
    once."""
    stop: threading.Event = field(default_factory=threading.Event)
    """What this slot's worker waits on, and what retires it.

    Per slot rather than one shared by the rack, and that is the mechanism the
    whole of `apply` rests on. A worker parks on this event -- in `run`'s loop
    guard and in every branch of `_wait` -- so setting it releases the thread at
    once, whether it is between two frames or eight hours into a night park. The
    alternative, a flag the worker checks between waits, would be noticed up to
    a whole `_NIGHT_PARK_CHUNK` later: twenty seconds against a three-second
    budget is a join that always times out.

    The supervisor's own `_stop_event` cannot serve, because it is one event for
    the rack: setting it to retire one screen stops the other three, the poller
    and the tunnel. `stop` therefore sets every one of these as well as that one.

    It belongs to the slot and not to the worker, so a worker the watchdog has
    abandoned is retired by the same set that retires its replacement.

    Never named `_stop` anywhere it could reach a `Thread`: `threading.Thread`
    has a real `_stop` that `join` calls, and shadowing it makes every join raise
    `TypeError` -- invisible until SIGTERM.
    """


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
        apply_budget: float = APPLY_BUDGET,
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
        self._apply_budget = apply_budget
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
    def stop_event(self) -> threading.Event:
        """What everything the supervisor does not own parks on. Read, never set.

        The link thread is the caller: it has to refuse to *start* an apply once
        a shutdown has begun -- an apply is a teardown and repaint of four panels
        run against a supervisor that is being torn down underneath it -- and it
        can only do that if it is watching the same event `stop` sets first.
        Handing out a second event would mean a link that learns about a SIGTERM
        after the panels have already been slept.
        """
        return self._stop_event

    @property
    def workers(self) -> list[ScreenWorker]:
        """The worker currently drawing each panel, in panel order.

        Derived rather than stored because the watchdog replaces workers and the
        panel they draw on outlives them; a second list would be a second thing
        to keep in step. A replaced worker is not here -- it is abandoned, not
        tracked -- which is also what keeps it out of the status file.
        """
        return [slot.worker for slot in self._slots if slot.worker is not None]

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
        # Two phases, and the order is not cosmetic. Panels share buses -- on a
        # Pi 3 SPI0 carries two chip selects and SPI1 carries two more -- so a
        # worker that starts drawing while its bus-mate is still running its
        # fifty-command init sequence corrupts that init, and the panel comes up
        # showing unconfigured RAM. Which panel loses depends on start order, so
        # the same rack fails differently on every restart and sometimes not at
        # all. The script this replaces got it right and said so: "Init displays
        # one at a time (GPIO race prevention)". Nothing is drawn until every
        # panel is initialised.
        for screen in self._screens:
            if self._stopped:
                return
            self._open_panel(screen)
        for slot in list(self._slots):
            if self._stopped:
                return
            self._start_worker(slot)

    def apply(self, config: DaemonConfig) -> None:
        """Swap to a pushed configuration, without restarting the process.

        *It is a diff, not a teardown.* A screen whose resolved configuration is
        identical is not stopped, its panel is not closed and its glass does not
        blink. That is the difference between a redundant push and a rack-wide
        flicker -- and the server pushes on every connect it cannot prove is
        redundant, so a wifi blip is a connect. It also bounds the work: a
        one-screen edit costs one screen, against a budget measured in seconds
        and a screen count this module does not choose.

        *It resolves before it touches anything.* `resolve_screens` runs against
        the new config alone and raises `ConfigError` on a template no screen can
        name, while the previous configuration is still driving the panels. The
        raise becomes a nack, which is how the person who saved the edit finds
        out; a rack torn down first and found unservable second is four dark
        panels and a server that believes it is fine.

        *The order inside is the same order `stop` uses, and for the same
        reason.* Nothing touches a backend while a thread that might also touch
        it is still running, and nothing is drawn until every panel is
        initialised:

        1. retire: revoke the leases, set the retiring slots' stop events, join;
        2. off the bus: hold every *kept* worker off its panel, blank the retired
           panels, open the new ones -- a changed screen's SPI device cannot be
           opened until the old one has let go of it, and a bus-mate that kept
           drawing across a fifty-command init sequence is the M2 race arriving
           by a different road;
        3. start: every new worker, once every new panel is open.

        The whole of it runs under `_shutdown_lock`, so a SIGTERM landing
        midway waits for a coherent rack rather than shutting down half of one.
        That wait is what `APPLY_BUDGET` bounds.

        What it does *not* do is reconfigure the integrations: pollers and
        tunnels keep running the set they were started with. Taking a tunnel down
        is a `kubectl` SIGTERM-then-SIGKILL wait measured at ten seconds in M2,
        which is the entire shutdown budget, so it does not fit here and is
        logged loudly instead of done quietly.
        """
        # First, and outside the lock: nothing has moved, and nothing may.
        replacement = resolve_screens(config)

        with self._shutdown_lock:
            if self._stopped:
                # The link refuses to start an apply once the stop event is set,
                # which is as much as it can do from there -- this is the same
                # check on the other side of the handover, where a signal that
                # landed in between is still visible.
                raise RuntimeError("this daemon is stopping; not applying a configuration")

            # The whole list, compared as models. A first pass compared the
            # *names* as well, which reads as thoroughness and is dead code: two
            # integration lists whose names differ are already unequal, so the
            # extra clause could never decide anything -- and a mutation of it
            # survived the whole suite, which is how it was found.
            if self._config.integrations != config.integrations:
                log.warning(
                    "this snapshot changes the rack's integrations; "
                    "the daemon has to be restarted before that takes effect",
                    extra={"integrations": [item.name for item in config.integrations]},
                )

            kept, fresh, retired = self._diff(replacement, config.night)
            self._config = config
            self._config_fingerprint = config_fingerprint(config)
            self._screens = replacement
            if not fresh and not retired:
                # Everything the workers read is already what the push asks for,
                # so there is nothing to do at the glass. Not merely fast: no
                # panel is closed, so nothing blinks and no SPI device changes
                # hands. The config above is still adopted, because the
                # fingerprint the status file reports is about the document.
                log.info("the pushed configuration is the one already running")
                return

            remaining = _deadline(self._apply_budget, self._shutdown_clock)
            self._retire(retired, remaining)
            # Reassigned before the opens, not after, so `_open_panel` appends
            # into the list a concurrent `stop` will walk. There is no instant at
            # which a panel is open and unreachable by a shutdown.
            self._slots = kept
            self._unavailable = []
            with self._off_the_bus(kept, remaining):
                for slot in retired:
                    slot.shut_down = True
                    self._shut_down_panel(slot.panel.backend, slot.screen.config.name)
                for screen in fresh:
                    self._open_panel(screen)
            for slot in list(self._slots):
                if slot.worker is not None or slot.shut_down:
                    continue
                try:
                    self._start_worker(slot)
                except Exception as exc:
                    # A Pi too short of memory to fork. `_start_worker` has
                    # already revoked the lease, claimed the slot and blanked the
                    # panel, so what is left is to say so where a headless rack
                    # is read -- without this the screen simply vanishes from the
                    # status file, and four screens with two dead ones report
                    # what a healthy two-screen rack reports.
                    #
                    # Not re-raised. The rest of the rack is on the new
                    # configuration, and a nack would have the server push the
                    # whole thing again on every connect to fix a machine that is
                    # out of threads.
                    name = slot.screen.config.name
                    log.error("screen unavailable", extra={"screen": name, "error": str(exc)})
                    self._unavailable.append(UnavailableScreen(name=name, reason=str(exc)))
            # Panel order, which is what `workers` and the status file promise.
            # By identity: every slot's screen is one of the objects in
            # `_screens`, kept slots included, because `_diff` re-points them.
            rank = {id(screen): index for index, screen in enumerate(self._screens)}
            self._slots.sort(key=lambda slot: rank[id(slot.screen)])
            if remaining() <= 0.0:
                log.error(
                    "applying a configuration overran its budget; "
                    "a screen would not stop, and a SIGTERM now has that much less time",
                    extra={"budget_s": self._apply_budget},
                )

    def _diff(
        self, replacement: list[ResolvedScreen], night: NightWindow
    ) -> tuple[list[_Slot], list[ResolvedScreen], list[_Slot]]:
        """Split the new screens into what is already running and what is not.

        Matched by content and not by name or by position, because neither
        identifies a screen: the schema makes `name` unique over nothing, and a
        screen that moved position is a screen that draws somewhere else. So a
        slot is reused only when *everything the worker reads* is equal -- see
        `_unchanged` for the list and for what it deliberately leaves out.

        A screen with no slot is a fresh one, and that includes a screen whose
        panel failed to open on the last attempt: it is opened again here, which
        is the only thing in the daemon that retries an open. A ribbon reseated
        between two pushes comes back without a restart.
        """
        # Read before `apply` adopts the new config, which is the only moment
        # the window the running workers were built with is still knowable.
        running_night = self._config.night
        unclaimed = list(self._slots)
        kept: list[_Slot] = []
        fresh: list[ResolvedScreen] = []
        for screen in replacement:
            match = next(
                (
                    slot
                    for slot in unclaimed
                    if _unchanged(slot.screen, screen, running_night, night)
                ),
                None,
            )
            if match is None:
                fresh.append(screen)
                continue
            unclaimed.remove(match)
            # Re-pointed at the new object although the two compare equal, so
            # that the ordering below can key on identity rather than on an
            # equality that two identical screens would both satisfy.
            match.screen = screen
            kept.append(match)
        return kept, fresh, unclaimed

    def _retire(self, retired: list[_Slot], remaining: Callable[[], float]) -> None:
        """Take the retiring workers off their panels and wait for them to leave.

        Leases first and all of them, before the first join: a revoked lease
        forwards nothing, so from this line on the only thread that can reach any
        of these panels is this one -- which is what makes it safe to blank them
        afterwards whether or not the joins succeed.

        The joins share one deadline. A worker that does not make it is logged
        and abandoned, exactly as `stop` abandons one, because there is no way to
        kill a Python thread and waiting longer buys nothing.
        """
        for slot in retired:
            slot.panel.revoke()
            slot.stop.set()
        for slot in retired:
            if slot.worker is None:
                continue
            slot.worker.join(remaining())
            if slot.worker.is_alive():
                log.warning(
                    "a screen would not stop for a reconfigure; "
                    "its panel is being handed over anyway",
                    extra={"screen": slot.screen.config.name},
                )

    @contextlib.contextmanager
    def _off_the_bus(self, kept: list[_Slot], remaining: Callable[[], float]) -> Iterator[None]:
        """Hold every kept worker off its panel for the block.

        Because the block opens panels, and panels share buses. M2 measured what
        that costs when they interleave: one panel worked and the others came up
        showing unconfigured RAM, non-deterministically, and the fix was to open
        every panel before starting any worker. On a rack that is already running
        the kept workers are the ones drawing, so the same rule needs this to
        hold at all.

        A worker that will not come off is logged and drawn past rather than
        waited on: it is wedged inside an SPI write, which is a screen already
        lost, and spending the rest of the budget on it would delay the panels
        that are fine. Released in a `finally` whatever the block does, or a
        failed open would leave three panels frozen for the life of the process.
        """
        held: list[ScreenWorker] = []
        try:
            for slot in kept:
                worker = slot.worker
                if worker is None:
                    continue
                if worker.pause(remaining()):
                    held.append(worker)
                else:
                    log.warning(
                        "a screen would not come off its panel while another is opened",
                        extra={"screen": slot.screen.config.name},
                    )
            yield
        finally:
            for worker in held:
                worker.resume()

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
                    # Computed at construction and again in `apply`, which are
                    # the only two moments the config can move: hashing it here
                    # would put a serialisation of the whole document on the
                    # once-a-second path for an answer that changes at most once
                    # a push.
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
            for slot in self._slots:
                # A screen worker waits on an event of its slot's own, so that a
                # reconfigure can retire one screen without stopping the rack --
                # which means the shared event above reaches the poller and the
                # tunnel and nothing else. Set here, before the first join, or
                # every worker would be joined without ever having been asked to
                # leave: the whole budget spent, and then four panels slept
                # underneath four threads still drawing on them.
                #
                # Every call re-walks the list rather than remembering it, for
                # the same reason the blanking below does: `stop` can run while
                # `start` is still opening panels, and the slots recorded after
                # it are reached by the next call.
                slot.stop.set()
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
                if slot.worker is None:
                    # Its panel is open but nothing was ever started on it --
                    # a stop that landed between the two phases. The panel is
                    # still slept below; there is simply no thread to wait for.
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
        if slot.worker is None:
            # Not yet started, so it has no heartbeat to be late with.
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
        worker = self._make_worker(slot.screen, panel, slot.stop)
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

    def _open_panel(self, screen: ResolvedScreen) -> None:
        """Open one panel and record it, drawing nothing yet."""
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
        slot = _Slot(screen=screen, panel=_Panel(backend))
        self._slots.append(slot)
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

    def _start_worker(self, slot: _Slot) -> None:
        """Give an already-open panel the worker that draws on it."""
        name = slot.screen.config.name
        worker = self._make_worker(slot.screen, slot.panel, slot.stop)
        try:
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
            # A panel with no worker is still this supervisor's to blank: the
            # slot already exists, so `stop` can reach it, but nothing will ever
            # draw on it again. Revoking first settles whether the worker got as
            # far as running.
            slot.panel.revoke()
            # Claimed before it is blanked, exactly as `stop` does it: the slot
            # is already in the list, so without this the panel is slept and
            # closed twice -- and a second close on a GC9A01 writes to a serial
            # device that has already been torn down.
            slot.shut_down = True
            log.error("could not start a worker; blanking its panel", extra={"screen": name})
            self._shut_down_panel(slot.panel.backend, name)
            raise
        slot.worker = worker

    def _make_worker(
        self, screen: ResolvedScreen, panel: _Panel, stop: threading.Event
    ) -> ScreenWorker:
        """One worker for one panel, waiting on the event its slot owns.

        Not `self._stop_event`, and that is the whole mechanism `apply` needs:
        the shared event is one event for the rack, so setting it to retire one
        screen would stop the other three, the poller and the tunnel. See
        `_Slot.stop`, and note that `stop` sets both.
        """
        return ScreenWorker(
            screen=screen,
            store=self._store,
            display=panel,
            system=system_scenes(),
            night=self._config.night,
            stop=stop,
            clock=self._clock,
        )
