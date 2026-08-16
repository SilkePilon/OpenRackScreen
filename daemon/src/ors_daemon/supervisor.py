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
old one, it bounds how long it waits (`APPLY_BUDGET`, `BUS_GUARD_BUDGET`), and
it never leaves a rack that is half of each configuration. Its integrations are
diffed the same way -- a source the push adds gets a poller, or every screen
that now depends on it sits on `connecting` for ever -- and the part of that
which is slow, waiting for a `kubectl` to die, is done by `_Reaper` on its own
thread so that it is not done on the link's.

*The watchdog and the apply are mutually exclusive.* `tick` takes
`_shutdown_lock` as `apply` and `stop` do, because all three walk `_slots`: a
watchdog restart landing inside an apply's open window is a brand-new thread
drawing on a bus-mate while a fifty-command init sequence is on the wire, and a
status write landing there describes a rack that is half of each configuration.
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
    DisplayConfig,
    IntegrationConfig,
    NightWindow,
    ScreenConfig,
    TunnelConfig,
)

# `PROBE_HOLD_BUDGET` is what a probe really holds a panel lit for, and this
# module is one of its two ends: `_capped_hold` cuts a longer request down to it
# and `ProbeResult` says nothing about having done so, so the server has to
# refuse a longer `hold_s` at its edge -- and the two ends may not each write the
# number down. It lives in `ors_schema.link` beside `MAX_PROBE_HOLD_S`, the wire
# bound, which is the only place both `ors-daemon` and `ors-server` can import.
#
# **Its value is load-bearing here and the argument is in its docstring.** `probe`
# holds `_shutdown_lock` for the whole hold, so the budget is additive with
# `SHUTDOWN_BUDGET`'s ten and the CLI's `LINK_JOIN_TIMEOUT` of three against the
# shipped unit's `TimeoutStopSec=30` -- about nineteen seconds at five, and about
# forty-four at the wire's thirty, which is a SIGKILL with four panels still lit.
# Whoever raises it reads that docstring first.
from ors_schema.link import PROBE_HOLD_BUDGET
from PIL import Image

from ors_daemon.clock import Clock
from ors_daemon.config import ResolvedScreen, config_fingerprint, resolve_screens, system_scenes
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.frames import FrameStream
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
"""How long `apply` may spend waiting for the workers it is *retiring*, in seconds.

One deadline for all of those joins, for the reason `SHUTDOWN_BUDGET` gives: how
many screens a snapshot holds is the server's decision, so a timeout each is
multiplied by a number this module does not choose.

Three seconds, and the number is bounded from both ends.

The ceiling is a shutdown. `apply` runs synchronously as the link's
`on_snapshot`, on the thread that reads the socket and the thread that consults
the stop event, so every second it spends is a second a SIGTERM waits. That time
is *additive* with `SHUTDOWN_BUDGET` rather than spent out of it, and the
correction matters because this docstring is where the unit file's
`TimeoutStopSec` is derived from: `stop` blocks on `_shutdown_lock`, which an
apply in flight is holding, and only takes its own deadline once it has the
lock. So the worst case a SIGTERM meets is this budget, plus `BUS_GUARD_BUDGET`,
plus whatever the opens cost (see `_off_the_bus` on why that last one is a
residual risk rather than a bound), plus `SHUTDOWN_BUDGET`'s ten, plus the CLI's
`LINK_JOIN_TIMEOUT` of three afterwards: about sixteen seconds against a
`TimeoutStopSec` of thirty. Past that, systemd sends SIGKILL before the panels
are slept and four GC9A01s stay lit until somebody pulls the power.

The floor is a real reconfiguration. A worker asked to stop is parked on its own
event or on the store and returns in milliseconds -- a real four-panel apply with
one wedged worker measured 3.001s in total, which is this budget and nothing
else. The only thing that spends it is a worker wedged inside an SPI write,
which is a screen already lost; three seconds is enough for several of those to
be given up on in turn and short enough that a whole rack's worth still fits.

What it bounds is those joins, and deliberately nothing else. It is not the bus
guard's deadline -- `BUS_GUARD_BUDGET` is, because a guard funded out of what a
wedged retirement left over is a guard that is skipped exactly when it is needed
-- and it is not a bound on the *work*. Overrunning does not abandon the apply: a
worker that will not stop has its lease revoked and its panel slept and closed
anyway, and the replacement rack is opened and started in full. Abandoning
halfway is four panels showing half of each configuration, with an ack that says
otherwise, which is worse than being late.
"""


BUS_GUARD_BUDGET = 1.0
"""How long `apply` may spend getting the kept workers off their panels, in seconds.

A deadline of its own, taken when the guard starts, and that is this number's
whole reason to exist. It used to be whatever `APPLY_BUDGET` had left, so one
wedged retiring worker spending the lot meant every kept worker was then offered
`pause(0.0)` -- which returns False whenever that worker happens to be inside
`tick`. Measured on four screens with one wedged retirement: `pause timeouts
granted to kept workers: [0.0, 0.0, 0.0]`. At ~23ms of SPI per frame on four
panels, catching one mid-`show` is likely, and that is the M2 interleaving that
produced the pale grey rectangle -- which stays wrong afterwards, because what it
corrupts is the init registers and not the framebuffer.

One second for the rack rather than one per screen, for the reason every other
deadline here is shared: four panels at a whole budget each is a twelve-second
apply, which is the same defect seen from the other side. What each screen is
promised out of it is `_BUS_GUARD_PER_SCREEN`, and four of those is exactly this.
"""

_BUS_GUARD_PER_SCREEN = 0.25
"""What one kept worker is promised out of `BUS_GUARD_BUDGET`, in seconds.

A whole tick -- read the snapshot, render, and clock ~115 KiB out at 40 MHz -- is
tens of milliseconds on a Pi 3B+, of which the SPI write is ~23ms. A quarter of a
second is an order of magnitude more than that, so a worker that is merely
*busy* always hands its panel over inside it; a worker that does not is wedged
inside a write and never will, whatever it is given. Small enough that the rack
this is built for -- four panels, so at most three kept -- fits inside the budget
with room, and large enough that the guard is a real one rather than a formality.
"""

IDENTIFY_BUDGET = 1.0
"""How long an identify may spend getting hold of the panels, in seconds -- in total.

One deadline for the rack, taken once, for the reason every other deadline here
is shared: how many screens there are is the server's decision, so a timeout each
is multiplied by a number this module does not choose.

It needs one at all because of where it runs. `Supervisor.identify` answers a
`Command` off the link, on the link's own thread -- the thread that reads the
socket, sends the heartbeat and consults the stop event -- and a worker wedged
inside an SPI write never gives its tick lock up. An unbounded identify is
therefore a rack that can never be reconfigured again, in exchange for a digit.

A second rather than three, because unlike `APPLY_BUDGET` nothing about the rack
depends on this finishing: an identify that misses a panel has painted no digit
on it, which is a press of the button that did not take and is retried by
pressing it again. It is deliberately well under `APPLY_BUDGET`, so an identify
arriving in the middle of nothing at all can never be the reason a SIGTERM is
late.
"""

_IDENTIFY_PER_SCREEN = 0.25
"""What one panel is promised out of `IDENTIFY_BUDGET`, in seconds.

`_BUS_GUARD_PER_SCREEN`'s measurement, for the same reason and against the same
lock: a whole tick -- read, render, and clock ~115 KiB out at 40 MHz -- is tens
of milliseconds on a Pi 3B+, so a worker that is merely busy always hands its
panel over inside a quarter of a second and one that does not is wedged and
never will. A separate constant rather than the same one because the two answer
to different budgets and a rack that grew a fifth panel would want them to move
apart.

Per screen rather than "whatever is left", which is exactly the defect
`BUS_GUARD_BUDGET` was split out to fix: one wedged worker spending the lot
means every panel after it is offered nothing and the last screens of a rack are
never numbered.
"""

SOURCE_TEARDOWN_BUDGET = 12.0
"""How long the reaper may spend taking one retired integration down, in seconds.

Off the apply path, so it is generous where `APPLY_BUDGET` cannot be: `Tunnel`
gives its `kubectl` child a SIGTERM and then a SIGKILL, five seconds each, which
M2 measured at ten seconds in total -- the whole of `SHUTDOWN_BUDGET`. Two more
for the joins around it. Nothing waits on this except `stop`, which joins the
reaper with what is left of its own deadline and abandons it like any other
thread that will not leave.
"""


class ProbeRefused(Exception):
    """A probe this rack would not run, and the reason a person reads.

    Refusing is a *result* here rather than an error: the whole point of the
    endpoint is to answer, and "SPI0.1 is driving the screen CPU" is as useful an
    answer as a panel lighting up. It is an exception rather than a return value
    because everything else `probe` can fail with -- a driver's `OSError`, a
    backend that will not build -- arrives that way too, and one path back means
    one place that has to remember to fill `ProbeResult.error` in.

    Its message is the whole reason, with nothing prefixed: see
    `ors_daemon.hardware._reason`, which puts a class name in front of an
    exception that carries no text of its own and leaves this one alone.

    *Two operator actions, deliberately flattened into one type.* "SPI0.2 is
    driving the screen CPU" is a refusal and asks somebody to edit a
    configuration; "SPI0.2 opened but would not take a frame" is not a refusal at
    all -- the probe ran -- and asks somebody to reseat a ribbon. They are raised
    as the same class on purpose, because the only consumer is
    `hardware.probe_handler`, and the only thing it can carry back is
    `ProbeResult(ok=False, error=<str>)`: the schema has no field for a category,
    and adding one belongs to `packages/ors-schema` rather than here. A second
    exception class today would be a distinction this daemon draws carefully and
    the wire discards one frame later, and it would read as a promise that
    somewhere downstream branches on it. So the distinction lives in the prose,
    which is the thing that actually reaches the person -- and the moment a
    caller needs to branch (a wizard that offers "check the ribbon" as a next
    step), the split and the schema field are one change, made together.
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

      `id` is in there, and it is the one field of the model that describes
      nothing about the panel -- so it is worth saying why it still counts.
      `_frame_handler` captures it in a closure when the worker is built, and a
      worker never re-reads it, so a slot kept across a push that moved the id
      addresses frames by the old number for the rest of the process. The
      server's `_owns` refuses every one of them: a browser panel that is
      permanently blank, while the server's log blames the daemon for sending
      screens it does not own.

      The flip side is accepted rather than fixed. Because `id` is inside
      `ScreenConfig` equality, a screen deleted and recreated server-side with
      byte-identical settings gets a new row id and therefore a retired worker
      and a rebuilt panel -- a brief dark circle for a field that changes
      nothing about what is drawn. Keeping the worker instead would need the id
      to be re-readable by a running worker, which means it stops being a
      closure and starts being shared mutable state on the render loop's path.
      One blink on an operation that is already a delete is the cheaper end.
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
      URL moves under it, because nothing the worker reads has changed: the
      readings arrive through the store either way, and `_reconfigure_integrations`
      is what puts the new poller behind them. A source *renamed* does change
      `depends_on` above, which is why that case restarts the screen.
    - `DaemonConfig.timezone`. The clock is built once, in `__main__`, and is
      shared by every worker and poller; swapping it under a running rack is a
      change to a component this class is handed rather than one it owns.
    - the *system* scenes -- `connecting`, `stale`, `error`, `identify`. A worker
      takes them at construction from `system_scenes()`, which reads
      `load_builtin_templates()["system"]` directly and never looks at the
      config, so a server-side edit to one of them validates, caches, acks and
      changes nothing on the glass. It is listed here beside `timezone` rather
      than compared, because comparing it would pin a difference this daemon
      cannot act on: making it act on one means routing the config's own
      `system` template through `system_scenes`, which is a change to what a
      built-in *is* and belongs with the rest of that decision.
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


def _screen_ids(screens: list[ResolvedScreen]) -> set[int]:
    """The server row ids in a list of screens. A screen without one is not in it.

    None is "no server has ever named this screen", so it is an absence rather
    than a value -- and there is nothing to stop streaming for a screen that
    could never have been streamed.
    """
    return {screen.config.id for screen in screens if screen.config.id is not None}


def _bus_of(display: DisplayConfig) -> tuple[str, int]:
    """Which wire this panel's commands travel over.

    Two screens that share one cannot be initialised while the other draws: a Pi
    3 carries two chip selects on SPI0 and two on SPI1, opening a GC9A01 is a
    hardware reset and a fifty-command init sequence over that shared wire, and a
    bus-mate drawing across it leaves the new panel showing unconfigured RAM. The
    chip select is deliberately *not* part of the answer -- it selects a device
    on the bus, it does not give it a bus of its own.

    Conservative where it does not know: a backend this function has never heard
    of gets a key made of its own name, so every screen on it shares one notional
    bus. Guessing that two unknown panels are independent is the mistake that
    costs a rack a pale grey rectangle; guessing that they are not costs an open
    that waits for a lock.
    """
    if display.backend == "gc9a01":
        return ("gc9a01", display.spi_bus)
    return (display.backend, 0)


def _probe_screen(bus: int, cs: int, dc: int, rst: int, hz: int) -> ResolvedScreen:
    """A screen that exists for one paint, describing a device nothing is driving.

    Built rather than looked up, because the whole point of a probe is that this
    panel is in no configuration yet: the operator is finding out whether it can
    be. It is never resolved, never diffed and never recorded in `_slots`, so
    `template` and `position` describe the candidate rather than instruct
    anything.

    **`template` is inert as this is written, and named correctly anyway.** The
    paint goes through `_paint`, which builds a `ScreenWorker` purely for its
    `identify` -- and that method reaches for `system_scenes()["identify"]`
    itself, so this field decides no pixel and a wrong name here would change
    nothing on the glass. It is what a reader of this function is told is being
    painted, and it is what would *become* the pattern the day `_paint` renders
    the configured template instead; a name no system template carries would then
    raise inside the bus guard, with every kept panel held off the wire behind
    it. `test_the_screen_a_probe_invents_names_a_scene_this_rack_has` asserts
    that much, and the pattern itself is pinned against `identify`'s own render
    by `test_a_probe_paints_the_pattern_identify_paints`.

    `position` is the schema's floor rather than a claim -- the ordinal on the
    glass is the device, since a candidate has no position in the rack.

    `rotation` and `hflip` are left at their defaults on purpose. How a panel is
    bolted in is something the operator tells the server *after* they have seen
    it light up, and correcting for a mount nobody has described yet would turn
    "which way up was it" into a second question this cannot answer.
    """
    return ResolvedScreen(
        config=ScreenConfig(
            name=f"probe SPI{bus}.{cs}",
            position=1,
            display=DisplayConfig(backend="gc9a01", spi_bus=bus, spi_cs=cs, dc=dc, rst=rst, hz=hz),
            template="identify",
        ),
        scenes=[],
        params={},
        depends_on=frozenset(),
    )


def _capped_hold(hold_s: float) -> float:
    """What this rack will actually hold a probe up for. See `PROBE_HOLD_BUDGET`.

    Written as a test rather than as `min(max(...))` for `frames._capped`'s
    reason: NaN compares False against everything, so `min` hands it straight
    back and `Event.wait(nan)` is a wait this module has no answer for. It cannot
    arrive off the wire -- `ProbeRequest.hold_s` refuses one -- and it is
    answered here as well, because `probe` is a public method and a guard that
    depends on a peer having validated its input is not a guard.
    """
    if not hold_s > 0.0:
        return 0.0
    return min(hold_s, PROBE_HOLD_BUDGET)


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


@dataclass
class _Source:
    """One integration, and the threads reading it.

    Grouped, because a reconfiguration retires them together: the poller and the
    tunnel behind one name are one fact about the rack, and two flat lists cannot
    say which tunnel belongs to which poller.
    """

    config: IntegrationConfig
    stop: threading.Event
    """What this source's threads park on, for the reason `_Slot.stop` exists.

    The supervisor's own `_stop_event` is one event for the rack, so setting it
    to retire one integration would stop the other sources, all four panels and
    the tunnels. `stop` therefore sets every one of these as well as that one.
    """
    poller: Poller | None = None
    tunnels: list[Tunnel] = field(default_factory=list)


class _Reaper(threading.Thread):
    """Where retired integrations are waited on, and deliberately not on the apply.

    Taking a tunnel down is a `kubectl` SIGTERM-then-SIGKILL wait that M2
    measured at ten seconds -- the whole of `SHUTDOWN_BUDGET`, three times
    `APPLY_BUDGET` -- and `apply` runs on the link thread, which is also the
    thread that notices a SIGTERM. So the apply does the part that costs nothing,
    setting the source's stop event, and hands the waiting here. That set is what
    actually stops things: the poller's next lap ends, the tunnel's loop ends,
    and `Tunnel.run`'s own `finally` kills its child. This thread is what turns
    "they have been asked to leave" into "they have gone".

    One thread for every retirement rather than one per retirement: a rack is
    pushed to on every connect the server cannot prove is redundant, for months,
    on a Pi with 1 GiB shared with the GPU.

    Started lazily, so a rack that is never reconfigured never has it. Closing it
    drains what it has been handed rather than dropping it -- `stop` joins it
    inside the shutdown deadline, and a source abandoned there is one whose
    threads are already leaving anyway.
    """

    def __init__(self, store: SnapshotStore) -> None:
        super().__init__(name="integration-reaper", daemon=True)
        self._store = store
        # Re-entrant, because `stop` can be re-entered by a second SIGTERM on the
        # very thread that is inside `close`, and a plain lock would deadlock
        # there rather than shut the rack down.
        self._condition = threading.Condition(threading.RLock())
        self._pending: list[tuple[_Source, bool]] = []
        self._closing = False

    def retire(self, source: _Source, drop_health: bool) -> None:
        """Hand over a source whose stop event the caller has already set.

        `drop_health` is False when the *name* is still configured -- a source
        whose URL moved is a removal and an addition of the same name -- because
        the status file would otherwise lose the entry the new poller has just
        registered.
        """
        with self._condition:
            self._pending.append((source, drop_health))
            self._condition.notify()

    def close(self) -> None:
        """Finish what is in hand and leave. Idempotent."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def run(self) -> None:
        """Reap until closed and drained. Nothing gets out of here.

        The same rule as every other thread in this daemon: this one is what
        stops `kubectl` children accumulating, and an exception escaping it would
        leave that undone with nothing anywhere saying why.
        """
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if not self._pending:
                    return
                source, drop_health = self._pending.pop(0)
            try:
                self._reap(source, drop_health)
            except Exception:
                log.exception(
                    "could not take a retired integration down",
                    extra={"integration": source.config.name},
                )

    def _reap(self, source: _Source, drop_health: bool) -> None:
        remaining = _deadline(SOURCE_TEARDOWN_BUDGET, time.monotonic)
        for tunnel in source.tunnels:
            # `shutdown` as well as `join`, because taking a tunnel down is not a
            # wait on a thread: it signals `kubectl` and waits for the child.
            tunnel.shutdown(timeout=remaining())
            tunnel.join(remaining())
        poller = source.poller
        if poller is not None:
            poller.join(remaining())
            if poller.is_alive():
                # It may still publish, so its health entry is left where it is:
                # a status file that had dropped a source still writing to the
                # store would describe a rack nobody is running.
                log.warning(
                    "a retired integration's poller would not stop",
                    extra={"integration": source.config.name},
                )
                return
        log.info("an integration has been taken down", extra={"integration": source.config.name})
        if drop_health:
            self._store.unregister(source.config.name)


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
        frames: FrameStream | None = None,
        sleeper: Callable[[float], object] | None = None,
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
        # Held by the supervisor and not by the workers, which is what makes a
        # screen's sequence numbers survive the worker that draws it: the
        # watchdog replaces workers and `apply` retires them, and a browser that
        # drops frames older than the last it drew must not be able to tell.
        self._frames = frames
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
        # How a probe waits out its hold, and the only wait in this class that is
        # spent on purpose rather than endured. The stop event by default, so
        # that the one interleaving it *can* be released by -- a stop that landed
        # after this probe read `_stopped` -- costs nothing to honour; `probe`
        # says why that is the exception rather than the rule. Injectable for the
        # reason `shutdown_clock` is: a test can prove the bound without spending
        # it, and no test in this repo may sleep to wait for time to pass.
        self._sleeper: Callable[[float], object] = sleeper or self._stop_event.wait
        self._slots: list[_Slot] = []
        self._sources: list[_Source] = []
        self._reaper: _Reaper | None = None
        self._unavailable: list[UnavailableScreen] = []
        # Which screens have already been reported as unstreamable, by
        # (position, name). See `_say_it_cannot_be_streamed`.
        self._unaddressable: set[tuple[int, str]] = set()
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

        The whole of it runs under `_shutdown_lock`, which `tick` takes as well:
        a SIGTERM landing midway waits for a coherent rack rather than shutting
        down half of one, and the watchdog cannot start a worker drawing on a
        bus-mate while a fifty-command init sequence is on the wire. That wait is
        what `APPLY_BUDGET` and `BUS_GUARD_BUDGET` bound.

        *It reconfigures the integrations too, without waiting for them.* A
        source the push adds gets a poller here, or every screen that now depends
        on it sits on `connecting` until somebody restarts the daemon; a source
        it drops has its stop event set here, which is what actually stops it,
        and is then handed to the reaper. A source whose configuration moved is a
        removal and an addition, because nothing can edit a running poller. What
        does *not* happen here is the waiting: see `_Reaper`.

        *It tells the frame stream which screens have stopped existing.* A
        subscription is whole-daemon state that only a `frames` request
        replaces, so a screen this push deletes would otherwise stay in the
        watched set for the life of the connection -- an id with no worker, no
        panel and no row behind it, kept alive against `_MAX_TRACKED_SCREENS`'s
        pruning at the expense of screens that do exist.
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

            kept, fresh, retired = self._diff(replacement, config.night)
            # Read while `self._screens` is still what the rack is running, which
            # is the only moment the two sets can be compared. Not the retired
            # *slots*: `_diff` retires a slot whose contents moved and rebuilds
            # the same screen straight afterwards, so a set taken from there
            # would stop streaming a panel that is merely being repainted.
            gone = _screen_ids(self._screens) - _screen_ids(replacement)
            # Adopted before the panels move, because `_make_worker` reads the
            # night window off it and `_open_panel` is about to run -- and rolled
            # back if anything below raises. What that protects is the *claim*:
            # a status file reporting the pushed fingerprint over a half-old rack
            # while the link nacks the same snapshot is a lie in the direction
            # that sticks, because a server told the push landed does not send it
            # again. The other direction corrects itself on the next connect.
            previous = (self._config, self._config_fingerprint, self._screens)
            self._config = config
            self._config_fingerprint = config_fingerprint(config)
            self._screens = replacement
            try:
                sources_changed = self._reconfigure_integrations(config)
                if not fresh and not retired:
                    # Everything the workers read is already what the push asks
                    # for, so there is nothing to do at the glass. Not merely
                    # fast: no panel is closed, so nothing blinks and no SPI
                    # device changes hands. The config above is still adopted,
                    # because the fingerprint the status file reports is about
                    # the document.
                    if not sources_changed:
                        log.info("the pushed configuration is the one already running")
                    return

                remaining = _deadline(self._apply_budget, self._shutdown_clock)
                self._retire(retired, remaining)
                # Reassigned before the opens, not after, so `_open_panel` appends
                # into the list a concurrent `stop` will walk. There is no instant at
                # which a panel is open and unreachable by a shutdown.
                self._slots = kept
                # Emptied and rebuilt rather than kept, and it is only safe
                # because every screen that has no slot -- a panel that would not
                # open, a worker that would not start -- is in `fresh` and is
                # tried again below. A record that outlived its retry would
                # report a screen unavailable that is drawing.
                self._unavailable = []
                with self._off_the_bus(kept) as unguarded:
                    for slot in retired:
                        slot.shut_down = True
                        self._shut_down_panel(slot.panel.backend, slot.screen.config.name)
                    for screen in fresh:
                        if _bus_of(screen.config.display) in unguarded:
                            self._skip_open(screen)
                            continue
                        self._open_panel(screen)
                for slot in list(self._slots):
                    # No `or slot.shut_down` guard, because no slot in this list
                    # can be shut down: the retired ones are not in it, and a
                    # failed start takes its own slot out below. The guard used
                    # to be here and was dead code, which is worse than absent --
                    # a mutation of it survived the whole suite.
                    if slot.worker is not None:
                        continue
                    try:
                        self._start_worker(slot)
                    except Exception:
                        # A Pi too short of memory to fork. `_start_worker` has
                        # already revoked the lease, blanked the panel, dropped
                        # the slot and recorded the screen as unavailable, so
                        # there is nothing left to do here but carry on.
                        #
                        # Not re-raised. The rest of the rack is on the new
                        # configuration, and a nack would have the server push
                        # the whole thing again on every connect to fix a machine
                        # that is out of threads.
                        pass
                # Panel order, which is what `workers` and the status file promise.
                # By identity: every slot's screen is one of the objects in
                # `_screens`, kept slots included, because `_diff` re-points them.
                rank = {id(screen): index for index, screen in enumerate(self._screens)}
                self._slots.sort(key=lambda slot: rank[id(slot.screen)])
                # Last, and only on the path that did not raise. A push that
                # nacks has rolled the claim back and will be sent again, so
                # leaving the stream alone is the conservative direction: a
                # screen kept in the watched set costs a subscription entry,
                # where one taken out of it is a browser panel that stops
                # filling in with nothing anywhere saying why.
                if self._frames is not None:
                    self._frames.retire(gone)
                if remaining() <= 0.0:
                    log.error(
                        "applying a configuration overran its budget; "
                        "a screen would not stop, and a SIGTERM now has that much less time",
                        extra={"budget_s": self._apply_budget},
                    )
            except BaseException:
                self._config, self._config_fingerprint, self._screens = previous
                raise

    def identify(self, screen_id: int | None = None) -> int:
        """Paint each panel's ordinal on it, now. Returns how many took the digit.

        The running rack's answer to `Command(command="identify")`, and the one
        command of the four that means something against a supervisor that is
        already driving panels: `sleep` and `wake` are what the night window and
        `ScreenConfig.sleep_override` are, and a rack whose configuration comes
        from the server has nothing to `reload` that `POST /api/daemons/{id}/push`
        does not already do.

        *`screen_id` is the server's row id, and None is the whole rack.* Nothing
        else identifies a screen -- `name` and `position` are unique over nothing
        in the schema -- and a screen carrying no id is one no server has ever
        named, so it is not addressable and is skipped rather than guessed at. A
        `screen_id` this rack does not have paints nothing and says so; painting
        the whole rack instead would be `identify` lighting up panels nobody
        asked about.

        *The ordinal is the screen's `position`*, which is what
        `__main__._identify` paints and what the printed map is keyed by. Two
        commands numbering the same panel differently would make the map useless.

        *It is bounded, and per panel.* See `IDENTIFY_BUDGET`: this runs on the
        link thread, and a worker wedged inside an SPI write never gives its tick
        lock up. A panel that will not come free is counted as not painted, which
        is what the caller reports.

        Under `_shutdown_lock` like `tick` and `apply`, because it walks
        `_slots` -- an apply is swapping that list and the panels behind it, and
        a digit painted into the middle of one lands on a backend that is about
        to be slept and closed. The lock ordering is the one `_off_the_bus`
        already establishes: this lock first, then a worker's tick lock, never
        the other way.
        """
        with self._shutdown_lock:
            if self._stopped:
                # The panels are slept and their serial devices closed by now,
                # and on a GC9A01 a command written to one that has been torn
                # down is a write to nothing. The same check `apply` makes.
                return 0
            remaining = _deadline(IDENTIFY_BUDGET, self._shutdown_clock)
            painted = 0
            for slot in self._slots:
                if screen_id is not None and slot.screen.config.id != screen_id:
                    continue
                worker = slot.worker
                if worker is None:
                    # An open panel nothing was ever started on. There is no
                    # thread to draw the digit and no lock to take.
                    continue
                if worker.identify(
                    str(slot.screen.config.position),
                    timeout=min(_IDENTIFY_PER_SCREEN, remaining()),
                ):
                    painted += 1
            return painted

    def claimed_devices(self) -> dict[tuple[int, int], str]:
        """Which SPI devices this rack's configuration is driving, and by whom.

        Keyed by `(bus, chip select)` -- the two numbers `/dev/spidev<bus>.<cs>`
        is named by -- and valued by the *screen's name*, because the answer is
        read by a person choosing a panel in the setup wizard: "SPI0.1 -- CPU"
        says why that row is unavailable, where a screen id would send them to
        look it up.

        *The running configuration, not the open panels.* `_screens` is a
        superset of what `_slots` holds: a screen whose backend would not open
        has no slot, and this still reports its device as claimed. That is the
        conservative direction and it is chosen deliberately -- the device
        belongs to a screen either way, the next push will try to open it again
        (`_diff` retries every one of them), and a wizard that offered it as free
        would be inviting an operator to give a second screen a device the first
        one is about to take back. It costs the ability to probe a screen's own
        wiring while that screen is configured, which is what `identify` is for.

        *Only backends that have a device.* A virtual panel is a directory of
        PNGs; claiming SPI0.0 for one -- which is what its unset `spi_bus` and
        `spi_cs` default to -- would hide a real device behind a screen that is
        not on the bus at all.

        *First claim wins.* Two screens may name one device: the schema does not
        forbid it, because which panels exist is not a fact a schema on another
        machine holds. Either name is an honest answer to "this is not free",
        and refusing to answer at all would make the wizard offer the device.

        Under `_shutdown_lock` like everything else that walks the rack's own
        state, because an apply is swapping `_screens` and the panels behind it.
        """
        with self._shutdown_lock:
            claimed: dict[tuple[int, int], str] = {}
            for screen in self._screens:
                display = screen.config.display
                if display.backend != "gc9a01":
                    continue
                claimed.setdefault((display.spi_bus, display.spi_cs), screen.config.name)
            return claimed

    def probe(self, bus: int, cs: int, dc: int, rst: int, hz: int, hold_s: float) -> None:
        """Light one candidate panel with the wiring an operator supplied, and hold it.

        The other half of detection. Enumeration can say which SPI devices exist
        and nothing more -- a GC9A01 has no readable id over 4-wire SPI, and DC
        and RST are GPIO lines somebody chose with a screwdriver -- so this is
        the guess being tested, and the proof is a human seeing the glass come on.
        It returns nothing: "it lit" is this returning at all, and every other
        outcome is the exception that says which.

        **It takes the same bus guard `apply` takes, and that is the whole of
        spec 6.3.** Lighting a candidate while a bus-mate is mid-frame is exactly
        the interleaving that produced M2's pale grey rectangles -- the failure
        that cost this project the most debugging -- except that here the daemon
        would be doing it *deliberately*, on a device nothing has ever opened, at
        a clock nobody has proved. So:

        - a device the running configuration already claims is refused rather
          than fought over. There is a live worker on it, and taking it is a torn
          frame at best and a wedged bus at worst;
        - every kept worker comes off the bus for the whole of it, with the same
          per-screen bound (`_BUS_GUARD_PER_SCREEN`) `_off_the_bus` promises;
        - a bus the guard could not hold refuses the probe. `_off_the_bus` yields
          exactly that set, and `apply` reads it to decide not to open a fresh
          screen; a probe is that decision made on purpose, so it reads the same
          answer. Better a refused probe than a corrupted panel -- an init
          sequence clocked out underneath a drawing bus-mate does not lose a
          frame, it leaves the panel showing unconfigured RAM until something
          re-runs the init, and nothing does;
        - the device is closed on the way out, on every path including a paint
          that raises. A probed panel that stays claimed is one the next apply
          cannot open, so a probe would cost the rack the screen it was run to
          add.

        *It paints what `identify` paints, through the thing that paints it.* A
        never-started `ScreenWorker` over the candidate backend, exactly as
        `__main__._identify` does it -- so there is one painter rather than two,
        and the person at the rack sees the pattern they have already seen. The
        ordinal is the device: `identify` numbers configured panels by
        `position`, and a candidate has none, so `0.2` names the thing being
        proved.

        *The hold is bounded here as well as on the wire.* See
        `PROBE_HOLD_BUDGET`. It is spent inside `_shutdown_lock` and inside the
        guard, so it is a rack-wide stall and time a SIGTERM waits out: the wait
        is on the stop event, but a `stop` running on another thread is parked on
        the lock this call holds and cannot set it, so the ceiling is what bounds
        this rather than the event. That is the reason the ceiling is small.

        *This widens a risk `_off_the_bus` accepted, and by whom.* Read that
        function's residual-risk note: an open performed inside the guard has no
        deadline on it, so a `spidev` open that blocks for ever freezes every kept
        panel on its last frame for the life of the process, out of the watchdog's
        reach. Until this existed, the only way to reach that open with wiring
        nobody had proved was an administrator pushing a configuration. A probe
        reaches it with a `dc`, an `rst` and an `hz` that are *by construction* a
        guess typed into a wizard -- the likeliest input there is to make an open
        misbehave -- from any session-guarded operator. The risk is unchanged in
        kind and the same argument still holds against bounding it here (a
        bounded open whose thread returns late leaks the device); what changed is
        who can reach it, and that is worth knowing before this is exposed to a
        rate limit rather than a review. It is on the hardware checklist: on a
        real Pi, does an open with the wrong `dc`/`rst` raise, or block?

        Under `_shutdown_lock` like `tick`, `apply` and `identify`, because it
        walks `_slots` and `_screens` and pauses workers -- and the lock ordering
        is the one `_off_the_bus` already establishes: this lock first, then a
        worker's tick lock, never the other way.
        """
        with self._shutdown_lock:
            if self._stopped:
                # The panels are being slept and their serial devices closed, and
                # opening another one now is a device this shutdown has already
                # walked past and will never close. The same check `apply` makes.
                raise ProbeRefused("this daemon is stopping; not probing a panel")
            claimed = self.claimed_devices().get((bus, cs))
            if claimed is not None:
                # Before the guard, not after: refusing costs the rack nothing,
                # and freezing four panels to find out is a stall for an answer
                # this already has.
                raise ProbeRefused(
                    f"SPI{bus}.{cs} is already driving the screen {claimed!r}; "
                    "it cannot be probed while it is configured"
                )

            screen = _probe_screen(bus, cs, dc, rst, hz)
            name = screen.config.name
            log.info(
                "proving one panel",
                extra={"device": f"SPI{bus}.{cs}", "dc": dc, "rst": rst, "hz": hz},
            )
            with self._off_the_bus(self._slots) as unguarded:
                if _bus_of(screen.config.display) in unguarded:
                    raise ProbeRefused(
                        f"a screen sharing the bus SPI{bus} would not stop drawing; "
                        "probing it now would corrupt both panels"
                    )
                backend = self._display_factory(screen.config, name)
                try:
                    if not self._paint(screen, backend):
                        # `ScreenWorker._show` absorbs a backend that refuses a
                        # frame, because a render loop has to survive one. Here
                        # it is the answer: the device opened and the glass took
                        # nothing, which is a ribbon seated well enough to
                        # enumerate and not well enough to clock a frame out of.
                        raise ProbeRefused(f"SPI{bus}.{cs} opened but would not take a frame")
                    self._sleeper(_capped_hold(hold_s))
                finally:
                    # Whatever happened above, including a raise. A serial device
                    # left open is a panel the next apply cannot have.
                    self._shut_down_panel(backend, name)

    def _paint(self, screen: ResolvedScreen, backend: DisplayBackend) -> bool:
        """Draw `identify`'s pattern on a panel no worker owns. True if it landed.

        A `ScreenWorker` that is never started, for `__main__._identify`'s
        reason: it is used purely for its `identify`, which is the one painter of
        this pattern in the daemon. Its stop event is its own and its store is
        the rack's -- neither is read, because nothing here runs a tick -- and the
        timeout is None because there is no loop to interleave with. It cannot
        block on a lock nobody else can hold.
        """
        worker = ScreenWorker(
            screen=screen,
            store=self._store,
            display=backend,
            system=system_scenes(),
            night=self._config.night,
            stop=threading.Event(),
            clock=self._clock,
        )
        display = screen.config.display
        return worker.identify(f"{display.spi_bus}.{display.spi_cs}")

    def _skip_open(self, screen: ResolvedScreen) -> None:
        """Record a panel that was not opened because its bus was not guarded.

        Dark and reported, rather than lit and wrong. See `_off_the_bus`: an init
        sequence clocked out while a bus-mate is drawing leaves the panel showing
        unconfigured RAM and *keeps* it that way, because what it corrupts is the
        init registers. The next push opens it again -- the same promise `_diff`
        makes about a panel whose backend would not open -- and until then the
        status file says which screen and why.
        """
        name = screen.config.name
        reason = "not opened: a screen sharing its bus would not stop drawing"
        log.error("screen unavailable", extra={"screen": name, "error": reason})
        self._unavailable.append(UnavailableScreen(name=name, reason=reason))

    def _reconfigure_integrations(self, config: DaemonConfig) -> bool:
        """Start the sources the push adds and retire the ones it drops.

        True if anything moved, which is only used to keep the "already running"
        line honest.

        Matched by name and compared as whole models, so a source whose URL,
        query set, poll interval or tunnel moved is retired and started again: a
        running `Poller` holds an `Integration` built from the configuration it
        was handed, and nothing can edit that underneath it. The alternative was
        a warning saying the daemon had to be restarted, which is worse than it
        sounds -- `_diff` retires every screen whose `depends_on` moved, and the
        replacement worker's health gate finds no entry for the new name, so a
        panel that had been showing a live reading showed `connecting` for ever.

        The retirement costs this thread nothing: setting the source's stop event
        is what stops it, and the waiting is the reaper's. The health entry is
        dropped only when the *name* has gone, because a source that was replaced
        has already registered the same name again.

        A source that will not start is logged and stepped over rather than
        raised, because the screens are already resolved and servable: a nack
        would have the server re-push a whole rack on every connect over one
        integration this Pi cannot build, and the screens that depend on it show
        `connecting`, which is what they would show anyway.
        """
        pushed = {item.name: item for item in config.integrations}
        retired = [
            source
            for source in list(self._sources)
            if pushed.get(source.config.name) != source.config
        ]
        running = {source.config.name for source in self._sources if source not in retired}
        added = [item for name, item in pushed.items() if name not in running]
        if not retired and not added:
            return False

        log.info(
            "reconfiguring this rack's integrations",
            extra={
                "retired": [source.config.name for source in retired],
                "added": [item.name for item in added],
            },
        )
        for source in retired:
            self._retire_source(source, drop_health=source.config.name not in pushed)
        for item in added:
            try:
                self._start_integration(item)
            except Exception as exc:
                log.error(
                    "could not start an integration; the screens that need it show `connecting`",
                    extra={"integration": item.name, "error": str(exc)},
                )
        return True

    def _retire_source(self, source: _Source, drop_health: bool) -> None:
        """Ask one integration's threads to leave, and hand them to the reaper."""
        source.stop.set()
        self._sources.remove(source)
        if source.poller is not None and source.poller in self.pollers:
            self.pollers.remove(source.poller)
        for tunnel in source.tunnels:
            if tunnel in self.tunnels:
                self.tunnels.remove(tunnel)
        reaper = self._start_reaper()
        if reaper is None:
            # No thread to wait on it with, on a machine that has just refused to
            # give us one. Its own threads have been told to leave and
            # `Tunnel.run`'s `finally` still kills its child; what is lost is the
            # join and the health entry, and neither is worth a nack.
            return
        reaper.retire(source, drop_health)

    def _start_reaper(self) -> _Reaper | None:
        """The reaper, started on the first retirement. None if it would not start.

        Lazy so that a rack which is never reconfigured never carries the thread,
        which is most racks: `start` retires nothing.
        """
        if self._reaper is None:
            reaper = _Reaper(self._store)
            try:
                reaper.start()
            except Exception:
                log.exception("could not start the reaper; a retired integration is left to leave")
                return None
            self._reaper = reaper
        return self._reaper

    def _diff(
        self, replacement: list[ResolvedScreen], night: NightWindow
    ) -> tuple[list[_Slot], list[ResolvedScreen], list[_Slot]]:
        """Split the new screens into what is already running and what is not.

        Matched by content and not by name or by position, because neither
        identifies a screen: the schema makes `name` unique over nothing, and a
        screen that moved position is a screen that draws somewhere else. So a
        slot is reused only when *everything the worker reads* is equal -- see
        `_unchanged` for the list and for what it deliberately leaves out.

        A screen with no slot is a fresh one, and that includes every screen the
        last attempt could not bring up: a panel whose backend would not open, a
        worker that would not start (`_start_worker` drops the slot for exactly
        this reason), a panel not opened because a bus-mate would not stop
        drawing. All three are tried again here, which is the only thing in the
        daemon that retries any of them -- a ribbon reseated between two pushes
        comes back without a restart.
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

        *Then the store is woken, once, before the first join.* Setting a
        `threading.Event` notifies nothing, and a healthy worker -- awake,
        unfaulted, drawing -- spends its life parked in
        `SnapshotStore.wait_for_change` rather than on its own event. So the
        flag alone was read only when that wait timed out, which is a whole
        heartbeat floor of five seconds, and the join below then spent whatever
        of `APPLY_BUDGET` was left. Measured on a one-screen virtual rack, four
        consecutive applies: 2.008s, 3.003s (overran, worker abandoned), 0.001s,
        1.019s -- on a rack with nothing wrong with it, on the branch that runs
        100% of the time. This is `stop`'s `self._store.close()` applied to the
        one retirement that is not a shutdown; the two are the same fix, and
        only the shutdown had it.

        Once for the rack rather than once per slot, and only when there is
        something to say: `wake` re-tests every waiter's predicate, so the
        workers that are staying pay a predicate call each and go back to
        waiting for what is left of their timeout.

        The joins share one deadline. A worker that does not make it is logged
        and abandoned, exactly as `stop` abandons one, because there is no way to
        kill a Python thread and waiting longer buys nothing.
        """
        if not retired:
            return
        for slot in retired:
            slot.panel.revoke()
            slot.stop.set()
        self._store.wake()
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
    def _off_the_bus(self, kept: list[_Slot]) -> Iterator[frozenset[tuple[str, int]]]:
        """Hold every kept worker off its panel for the block. Yields the buses it could not.

        Because the block opens panels, and panels share buses. M2 measured what
        that costs when they interleave: one panel worked and the others came up
        showing unconfigured RAM, non-deterministically, and the fix was to open
        every panel before starting any worker. On a rack that is already running
        the kept workers are the ones drawing, so the same rule needs this to
        hold at all.

        *Its own deadline.* `BUS_GUARD_BUDGET` is taken here, not inherited from
        the retirements, because a guard funded out of what a wedged retirement
        left over is a guard that is skipped exactly when it is needed -- and
        every kept worker gets a real `_BUS_GUARD_PER_SCREEN` out of it rather
        than whatever the worker before it happened to leave.

        *A worker that will not come off is not drawn past silently.* It is
        wedged inside an SPI write and waiting longer buys nothing, so the guard
        is skipped -- but *only* for the bus it is on, and the caller is told
        which. Every panel on that wire is then left unopened and reported
        unavailable, because an init sequence clocked out underneath a drawing
        bus-mate does not merely lose a frame: it leaves the panel showing
        unconfigured RAM until something re-runs the init, which nothing does.
        One dark panel that says so beats one that is lit and wrong.

        Only the *kept* workers can put a bus in that state, and that asymmetry
        is real rather than an oversight: a retiring worker has had its lease
        revoked before the first join, so a revoked `_Panel` forwards nothing and
        the thread cannot reach the wire however wedged it is. A kept worker
        keeps its lease, because it is going on drawing afterwards -- which is
        the whole reason this exists.

        Everything is held, and only the *opens* are decided per bus. Holding a
        worker that shares no wire with anything being opened costs it one frame;
        deciding it is safe costs a rack a pale grey rectangle if this function's
        idea of a bus is ever narrower than the hardware's.

        Released in a `finally` whatever the block does, or a failed open would
        leave three panels frozen for the life of the process -- and `resume`
        tolerates a worker that never paused, so a pairing bug here cannot become
        a `RuntimeError` escaping `apply` after the panels have already moved.

        *Residual risk, recorded rather than fixed.* The block opens panels while
        holding these locks, and an open is a GPIO reset, two sleeps totalling
        160ms and ~50 SPI commands with no deadline on it. A `spidev` open that
        blocks for ever therefore freezes every kept panel on its last frame for
        the life of the process, where before `apply` existed it could only block
        a startup that had nothing to freeze -- and the watchdog cannot rescue
        them, because they are blocked on a lock rather than wedged. Bounding it
        properly needs another thread, and a bounded open whose thread returns
        *after* the deadline hands back a `DisplayBackend` for a device nothing
        owns and nothing will close, which is a leaked SPI device and a panel
        that cannot be reopened by the next push either. That is a worse failure
        than the one it prevents, for a call that on this hardware opens two
        character devices; so it is written down here instead.
        """
        held: list[ScreenWorker] = []
        unguarded: set[tuple[str, int]] = set()
        remaining = _deadline(BUS_GUARD_BUDGET, self._shutdown_clock)
        try:
            for slot in kept:
                worker = slot.worker
                if worker is None:
                    continue
                if worker.pause(min(_BUS_GUARD_PER_SCREEN, remaining())):
                    held.append(worker)
                    continue
                bus = _bus_of(slot.screen.config.display)
                unguarded.add(bus)
                log.error(
                    "a screen would not come off its panel; "
                    "nothing else on its bus is being opened this time",
                    extra={"screen": slot.screen.config.name, "bus": f"{bus[0]}{bus[1]}"},
                )
            yield frozenset(unguarded)
        finally:
            for worker in held:
                worker.resume()

    def tick(self) -> None:
        """One watchdog pass and one status write, excluded against `apply`.

        Under `_shutdown_lock`, which is the same lock `apply` and `stop` take,
        and it closes two holes that are one hole. The watchdog runs on the main
        thread and an apply on the link thread, so without it `_replace` can
        start a brand-new worker drawing on a bus-mate while a fifty-command init
        sequence is on the wire -- measured, with a kept worker's heartbeat
        stale: `replaced=True held_off=False`. It can also fire on a *retired*
        slot in the window between the revoke loop and the join loop, handing it
        a fresh live panel on a backend that is about to be slept and closed. And
        the status write below reads `_slots`, `_unavailable` and `_config` while
        an apply is swapping all three, so one file could describe a rack that is
        half of each configuration -- with a fingerprint naming one of them.

        The cost is that a tick waits out an apply, which is bounded by
        `APPLY_BUDGET` plus `BUS_GUARD_BUDGET` and happens at most once a push;
        the status file is a second or two later, and describes a rack that
        exists.
        """
        with self._shutdown_lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
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
                    # One plain attribute read of an int, off the render loop
                    # and off the pump: the counter is written under the
                    # stream's own lock and read without it, which is the same
                    # bargain `build_status` makes about the workers' fields.
                    frames_dropped=0 if self._frames is None else self._frames.dropped,
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
            for source in self._sources:
                # And every source's own event, for the same reason: a poller and
                # a tunnel park on one of these so that a reconfigure can retire
                # one integration without stopping the rack, which means the
                # shared event above no longer reaches them by itself.
                source.stop.set()
            if self._reaper is not None:
                # Told to finish now, joined below. It drains what it has been
                # handed rather than dropping it, so a `kubectl` retired a
                # moment before a SIGTERM is still signalled.
                self._reaper.close()
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
            if self._reaper is not None:
                # Out of the same deadline, and last of the threads: what it is
                # waiting on is a `kubectl` this rack has already stopped caring
                # about, so it is the first thing that should lose the race. A
                # reaper that will not stop is abandoned like any other thread
                # here -- it is a daemon thread, and every child it was going to
                # signal has been signalled by `Tunnel.run`'s own `finally`.
                self._reaper.join(remaining())
                if self._reaper.is_alive():
                    log.warning("the reaper did not stop; leaving it to the process exit")

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
        """One source: its tunnel, its poller, and the event that retires both.

        Registered with the store first, so a screen depending on an integration
        that has not answered yet shows `connecting` rather than failing to find
        it at all -- which is as true of a source a push adds under a running
        rack as it is of one this daemon booted with.

        The threads park on the source's own event rather than on
        `self._stop_event`, for the reason `_Slot.stop` gives: the shared one is
        one event for the whole rack, so setting it to retire one integration
        would stop the other sources, the tunnels and all four panels. `stop`
        sets both.
        """
        self._store.register(integration_config.name)
        source = _Source(config=integration_config, stop=threading.Event())
        url_provider: UrlProvider | None = None
        if integration_config.tunnel is not None:
            tunnel = self._tunnel_factory(integration_config.tunnel, source.stop)
            tunnel.start()
            source.tunnels.append(tunnel)
            self.tunnels.append(tunnel)
            url_provider = _url_of(tunnel)
        # Recorded before the poller is built, so a source whose poller raises is
        # still one this supervisor can stop: the tunnel above is running and
        # holds a `kubectl` child.
        self._sources.append(source)

        poller = (
            self._poller_factory(integration_config, url_provider)
            if self._poller_factory is not None
            else Poller(
                integration=build_integration(integration_config, url_provider),
                store=self._store,
                interval=integration_config.poll_interval,
                stop=source.stop,
                clock=self._clock,
            )
        )
        if poller is None:
            return
        poller.start()
        source.poller = poller
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
        """Give an already-open panel the worker that draws on it.

        A start that fails takes the slot with it, and that is not tidiness. The
        panel has been slept and closed by then, so the slot describes nothing --
        but `_diff` matches slots by content, so a slot left behind was matched
        as *unchanged* by every later push: the start loop skipped it, `_check`
        skipped it, and `self._unavailable = []` dropped its record the moment
        any other screen changed. Measured: `after failed start: unavailable=['R1']
        workers=['S2']`, and after a push that touched only the other screen,
        `unavailable=[] workers=['R2'] status screens=['R2']` -- one dark circle,
        permanently, absent from the status file. Dropping the slot is what makes
        the screen `fresh` again on the next push, which opens it and starts it
        again; the record below is what reports it in the meantime.
        """
        name = slot.screen.config.name
        worker = self._make_worker(slot.screen, slot.panel, slot.stop)
        try:
            worker.start()
        except BaseException as exc:
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
            # Dropped only once the panel is off. Up to this line the slot is
            # what makes it reachable by a `stop` -- including one re-entered by
            # a signal on this very thread -- and after it there is nothing left
            # to reach: the backend is closed and its lease revoked. See this
            # method's docstring for what keeping it cost.
            if slot in self._slots:
                self._slots.remove(slot)
            self._unavailable.append(UnavailableScreen(name=name, reason=str(exc)))
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
            on_frame=self._frame_handler(screen),
        )

    def _frame_handler(self, screen: ResolvedScreen) -> Callable[[Image.Image], None] | None:
        """Where this screen's frames go, or None if they go nowhere.

        Two ways of getting None, and both are ordinary. There may be no stream
        at all -- an unpaired rack has no link to send anything down. And the
        screen may carry no `id`, which is what a hand-written YAML file looks
        like: the id is the server's row id, it is what a frame is addressed by,
        and there is no substitute for it here. `name` and `position` are not
        unique in the schema, so a daemon that guessed one would be asking the
        server to paint over a panel belonging to another rack.

        A function rather than a closure written inline at the call site, so that
        `screen` is a parameter of *this* call: written inline in a loop it would
        capture the loop variable and give every panel the last screen's id.

        The capture is also why `_unchanged` has to read `ScreenConfig.id`: the
        number is fixed here, at construction, and a worker that kept its slot
        across a push that moved it would address frames by the old one for ever.

        The second of the two Nones is said out loud, once. A rack whose screens
        carry no ids draws perfectly and streams nothing at all, which from a
        browser is a panel that never fills in and from the daemon's log was
        previously indistinguishable from a rack nobody is watching. Both ways
        of getting there -- a hand-written YAML, and a server too old to put the
        column in its snapshot -- are things an operator can act on once they
        are told.
        """
        frames = self._frames
        screen_id = screen.config.id
        if frames is None:
            return None
        if screen_id is None:
            self._say_it_cannot_be_streamed(screen)
            return None

        def offer(image: Image.Image) -> None:
            frames.offer(screen_id, image)

        return offer

    def _say_it_cannot_be_streamed(self, screen: ResolvedScreen) -> None:
        """One INFO line per screen that carries no server id, and not one more.

        Keyed by name and position together, because neither is unique on its
        own and the pair is what the status file and `identify` already use to
        tell two screens apart. Per supervisor and not per worker, so the
        watchdog restarting a panel four times does not say it four times, and
        an `apply` that changes nothing about a screen does not repeat it
        either -- but a rack reconfigured to add a screen with no id still says
        so about the new one.
        """
        key = (screen.config.position, screen.config.name)
        if key in self._unaddressable:
            return
        self._unaddressable.add(key)
        log.info(
            "this screen cannot be streamed to a browser: it carries no server id",
            extra={"screen": screen.config.name, "position": screen.config.position},
        )
