"""One panel's thread: what to draw, when to draw it, and when to stop.

Everything upstream -- the renderer, the config, the snapshot, the backends --
exists to feed this loop. Three decisions live here and nowhere else:

*Health before conditions.* A screen whose sources have not answered yet shows
`connecting`, and one whose data has gone stale shows `stale`. Both are system
scenes, picked **by name** because they carry no `when`; only once every source
a screen depends on is healthy does the screen's own scene selection run.

*Render on change, with a heartbeat floor.* A redraw costs ~23ms of SPI time per
panel before the render, and four panels share one Pi. Drawing only when the
snapshot version moved, the scene changed, the floor elapsed or an identify
arrived turns a static rack from ~12 renders/s into ~0.8.

*Night mode.* Inside the window the panel is put to sleep once and nothing is
rendered at all. The pollers keep running throughout, so the first frame after
a wake is live data rather than the previous evening's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from ors_render import RenderContext, render_scene, select_scene
from ors_render.render import expand_params
from ors_schema.daemon import NightWindow
from ors_schema.scene import Scene
from PIL import Image

from ors_daemon.clock import Clock, in_window, seconds_until_boundary
from ors_daemon.config import ResolvedScreen
from ors_daemon.displays import DisplayBackend
from ors_daemon.snapshot import Health, Snapshot, SnapshotStore

log = logging.getLogger(__name__)

_MAX_DISPLAY_RETRIES = 3
"""Consecutive failed writes before a panel is given up on.

An SPI write fails for two kinds of reason, and only one of them is worth
retrying: a transient bus error, or a panel that has come unplugged. A couple of
retries covers the first; going on past that means writing to a panel that is
not there, once a frame, forever -- which fills the journal and buries the one
line that says which panel actually died. The count resets on any good frame,
so this is three failures in a row, not three in the daemon's lifetime.
"""

_NIGHT_PARK_CHUNK = 20.0
"""The longest a sleeping worker parks in one go, in seconds.

Bounded on purpose. Do not "optimise" this back to the window boundary: the
whole night is one wait as far as this branch is concerned, and parking it in a
single call is the obvious simplification and the wrong one.

`heartbeat` is stamped at the top of `tick`, and the supervisor's watchdog
restarts any worker whose heartbeat has not moved inside its timeout. A single
park across an eight-hour night therefore reads as four wedged panels thirty
seconds after lights-out -- and each restart re-opens the backend, re-sleeps the
panel and parks again, so it repeats: ~960 spurious restarts a night, every one
logged as a fault, describing a rack that is working perfectly.

A lap here costs a clock read, an `in_window` and a stamp. No render, no SPI, no
snapshot read. So ~1,400 of them across a night are free beside the ~5,400
*rendering* laps that parking on the boundary at all was introduced to remove --
this keeps that win and drops the part of it that was never the point.

Twenty seconds because it has to sit comfortably under the watchdog's timeout,
whose default is 30s. That is a real constraint between the two modules: a
watchdog timeout at or below this value restarts every sleeping panel on
schedule.
"""

_ERROR_MESSAGE_CHARS = 40
"""How much of a render failure reaches the glass.

The `error` scene draws it at 11px inside a 0.7-wide box, so anything past this
is invisible anyway -- and the whole message is in the log, where it is read.
"""


class ScreenWorker(threading.Thread):
    """Owns one panel: what to draw, when to draw it, and when to stop."""

    def __init__(
        self,
        screen: ResolvedScreen,
        store: SnapshotStore,
        display: DisplayBackend,
        system: dict[str, Scene],
        night: NightWindow,
        stop: threading.Event,
        clock: Clock,
        floor: float = 5.0,
        on_frame: Callable[[Image.Image], None] | None = None,
    ) -> None:
        super().__init__(name=f"screen-{screen.config.name}", daemon=True)
        self._screen = screen
        self._store = store
        self._display = display
        self._system = system
        self._night = screen.config.sleep_override or night
        # Never `_stop`: `threading.Thread._stop` is a real method that `join`
        # calls, and shadowing it makes every join raise `TypeError`.
        self._stop_event = stop
        self._clock = clock
        self._floor = floor
        # Where a frame goes when somebody is watching this panel in a browser,
        # or None when nothing is. The contract is the whole reason this is a
        # callback rather than a component: it is called with this worker's tick
        # lock held, so it must not encode, must not send and must not block --
        # `FrameStream.offer` stores one reference and returns. A panel is the
        # product and a preview is not, so nothing behind this may cost the
        # render loop anything it can measure.
        self._on_frame = on_frame
        self._logged_frame_error: str | None = None
        # Serialises everything that touches the backend, for any caller that
        # arrives while this loop is mid-tick: `show` is several sequential SPI
        # commands (address window, then the frame), so two interleaved writes do
        # not merely race for the last frame -- they corrupt both. Two callers
        # arrive from outside: `identify`, from `__main__._identify` on a worker
        # it never starts, and `pause`, which is the supervisor taking this
        # worker off a bus it is about to open another panel on. It is also what
        # makes a tick atomic against itself. Held across the render, so
        # the counters and `current_scene` a status report reads always describe
        # the frame that is actually on the glass. Not an `RLock`: nothing here
        # re-enters, and a plain lock keeps that provable.
        self._lock = threading.Lock()
        self._seen_version = -1
        self._selected_scene: str | None = None
        self._failures = 0
        self._logged_error: str | None = None
        self._logged_backend_error: str | None = None

        self.screen_name = screen.config.name
        """The panel's name. The thread's own `name` is prefixed, so status
        reporting reads this rather than parsing the thread name back apart."""
        self.current_scene: str | None = None
        self.last_render: datetime | None = None
        """When the frame now on the glass was drawn, on the injected clock.

        Also what the heartbeat floor measures from, so it is one field rather
        than a public copy of a private one that could drift from it."""
        self.renders = 0
        self.faulted = False
        self.asleep = False
        self.heartbeat = 0.0
        """`time.monotonic()` at the top of the last tick, for the watchdog.

        Monotonic, unlike everything else here, because it answers "is this
        thread still turning" -- a question an NTP step must not be able to
        answer for it, in either direction.
        """
        self.held_off = False
        """Whether something outside this worker is holding it off its panel.

        Set by `pause` and cleared by `resume`, and published rather than kept
        private because "who is holding this lock" is not a question a lock can
        answer. The supervisor is the only caller and reads it back nowhere; it
        exists so that the property *can* be observed, since the alternative --
        a test that infers it from a wait that does not finish -- proves nothing
        about a lock that was simply busy.
        """

    def tick(self) -> None:
        """One loop iteration, without waiting.

        Absorbs everything the panel and the renderer do: a backend that refuses
        any of `show`, `sleep` or `wake` is counted towards the fault latch here
        rather than raising, and a scene that will not draw becomes the `error`
        scene. What is *not* caught is the rest -- a store that has broken, a
        clock that raises, an `error` scene that will not draw either -- because
        those are bugs rather than states, and `run` is where they are logged
        without costing the thread. So this is safe to call from a test or a
        one-shot render, but it is not a promise that nothing can escape.
        """
        with self._lock:
            self.heartbeat = time.monotonic()

            if self.faulted:
                # Before the night check, not after it. A faulted panel is
                # usually an unplugged one, and `sleep` and `wake` reach the bus
                # through the same `_command` as the `show` that just failed
                # three times -- so touching it here is the same write, and
                # would land outside the fault latch that has already fired.
                return

            now = self._clock()

            if in_window(now, self._night):
                if not self.asleep and self._backend("sleep", self._display.sleep):
                    self.asleep = True
                    log.info("night mode", extra={"screen": self.screen_name})
                return

            if self.asleep:
                if not self._backend("wake", self._display.wake):
                    # The panel may still be dark, so nothing is drawn onto it.
                    # `asleep` stays set, which is what puts the retry on the
                    # floor rather than on the next nightfall.
                    return
                self.asleep = False
                # Nothing is on the glass -- the panel has been dark -- so no
                # selection stands behind it, and the next test cannot find one
                # unchanged. Drawing on the way out is the stated behaviour of
                # leaving the window, and it must not depend on the floor
                # having elapsed: with a window narrower than the floor, and no
                # new data across it, nothing else would put a frame up.
                self._selected_scene = None

            snapshot = self._store.read()
            scene, name, context = self._select(snapshot)
            if not self._should_render(snapshot.version, name, now):
                return
            self._seen_version = snapshot.version
            self._render_and_show(scene, name, context)

    def identify(self, ordinal: str) -> None:
        """Paint the panel's ordinal, now, from whatever thread asked for it.

        `__main__._identify` is the only caller, and it calls this on a worker it
        never starts -- there is no identify path through the supervisor, and a
        running rack redraws over the digit within a tick. The lock is still
        taken, because "from whatever thread" is the contract this offers and a
        method that is only safe from one thread should say so instead.

        Deliberately not sticky: the next tick draws the screen's real scene
        again, because `_selected_scene` now reads `identify` and the change
        alone is a reason to render. The digit therefore stands for at most one
        loop wait, which is what someone counting panels in a rack needs.
        """
        with self._lock:
            if self.faulted:
                return
            scene = self._system["identify"]
            context = RenderContext(data={"params": {"ordinal": ordinal}})
            self._show(render_scene(scene, context), "identify")

    def pause(self, timeout: float) -> bool:
        """Keep this worker off its panel until `resume`. True if it took.

        For the supervisor, and for one situation: it is about to open a panel
        that shares an SPI bus with this one. Opening a GC9A01 is a hardware
        reset and a fifty-command init sequence over that shared wire, and a
        worker drawing across it corrupts the init -- the other panel comes up
        showing unconfigured RAM, non-deterministically, depending on which of
        them the scheduler favoured. On a rack coming up, ordering all the opens
        before all the starts is enough, because nothing is drawing yet; on a
        rack being *reconfigured*, the panels that are staying are drawing
        throughout, and this is the only thing that stops them.

        It takes the tick lock, so it waits out a `show` that is already on the
        wire rather than interrupting it -- which is the whole point: the window
        it closes is the one where two threads are on the bus at once.

        Bounded, and the answer is returned rather than raised, because it can
        legitimately fail: a worker wedged inside an SPI write never gives its
        panel up, and an apply that waited for one would spend its entire budget
        on a screen that is already lost. The caller proceeds and logs.

        Not re-entrant, deliberately: `self._lock` is a plain `Lock`, so a second
        `pause` answers False rather than double-counting a hold that one
        `resume` would then release.
        """
        taken = self._lock.acquire(timeout=max(0.0, timeout))
        if taken:
            self.held_off = True
        return taken

    def resume(self) -> None:
        """Give the panel back. A no-op on a worker that was never held off.

        Robust rather than paired by convention, and the cost of the convention
        is measured. `Lock.release` on a lock this thread never took raises
        `RuntimeError`, and the only caller makes these calls from a `finally`
        that runs *after* the retired panels have been closed and the new ones
        opened -- so the raise escapes `apply` and the link nacks a snapshot that
        is in fact on the glass, while every kept worker that *did* pause is
        never resumed and stays frozen on its last frame for the life of the
        process. One panel's pairing bug must not cost the other three.

        `held_off` is what makes that decidable, which is a second reason for it
        to be a field: it is set only by a `pause` that took the lock, so it
        answers "did this worker hand its panel over" without asking a lock a
        question locks cannot answer. Not re-entrant either way -- a second
        `pause` returns False and never sets it, so one `resume` releases one
        hold.

        Still a warning, because a caller resuming what it never held is a bug in
        the caller and this is the only place it can be seen from.
        """
        if not self.held_off:
            log.warning(
                "a screen was resumed that was never held off", extra={"screen": self.screen_name}
            )
            return
        self.held_off = False
        self._lock.release()

    def run(self) -> None:
        """Draw until stopped. Nothing gets out of here.

        The same rule as the poller's loop and for the same reason: this is a
        daemon thread, and an exception escaping `run` would leave one panel
        frozen on its last frame with nothing anywhere saying why. `tick`
        absorbs what the renderer and the backend do; this catches what it
        cannot -- a store that has broken, a clock that raises.

        Two things end it, and the second is not decoration. The stop event is
        the ordinary one. A *closed store* is the other: `wait_for_change`
        answers True the moment a store closes and goes on answering it, which
        is what releases a worker parked there when the daemon is shutting down
        -- so a loop that only watched the stop event would come straight back
        round, find nothing to render, park, be released again, and spin. It
        costs a whole core per screen with no backend call behind it, which is
        to say nothing in the status file or the logs would ever show it. Today
        the supervisor always sets the event first and the window never opens;
        M3 closing or swapping a store under a running daemon is the caller that
        opens it, and four pegged cores on a Pi 3B+ is not a symptom anyone
        could chase back to here. A closed store means no further data is
        coming, and a worker with nothing left to draw leaves.
        """
        try:
            while not self._stop_event.is_set() and not self._store.closed:
                try:
                    self.tick()
                except Exception:
                    log.exception("screen tick failed", extra={"screen": self.screen_name})
                try:
                    self._wait()
                except Exception:
                    # Only the store or the clock can get here. Losing the pace
                    # is survivable; losing the thread is not -- and an
                    # unguarded wait would turn a broken store into four frozen
                    # panels, which reads from the rack as dead hardware.
                    log.exception("screen wait failed", extra={"screen": self.screen_name})
                    self._stop_event.wait(self._floor)
        finally:
            try:
                self._display.close()
            except Exception:  # contract says it raises nothing; shutdown believes nobody
                log.warning("closing the panel failed", extra={"screen": self.screen_name})

    def _wait(self) -> None:
        """Park until there is something new to draw, or until the floor elapses."""
        if self._stop_event.is_set():
            return
        now = self._clock()
        boundary = seconds_until_boundary(now, self._night)

        if self.asleep and in_window(now, self._night):
            # Towards the window's end, in bounded chunks. Nothing can produce a
            # frame before it closes, so the floor has no business pacing this
            # -- capping it there is ~5,400 wakeups across an eight-hour night
            # to re-decide the same thing. But it must not be one long park
            # either: the laps are what keep `heartbeat` moving, and the
            # watchdog restarts a worker whose heartbeat has stopped. See
            # `_NIGHT_PARK_CHUNK`. The `min` is what makes the last chunk land
            # on the boundary itself, so the panel still wakes on time. Still
            # the stop event, so shutdown is felt at once rather than at dawn.
            self._stop_event.wait(min(boundary, _NIGHT_PARK_CHUNK))
            return

        timeout = min(self._floor, boundary)
        if self.asleep or self.faulted:
            # `asleep` outside the window is a wake that did not take, and it
            # has to be retried at the floor -- parking on the boundary would
            # leave the panel dark until nightfall. A faulted screen draws
            # nothing either way. Neither may wait on the snapshot version: it
            # has already moved past the one last drawn and the pollers keep
            # moving it, so that wait returns instantly, every lap, forever.
            self._stop_event.wait(timeout)
            return
        # The stop event goes in as well as the version, and it is the branch
        # that runs on every lap of a healthy rack -- so it is the one where
        # leaving it out cost the most. The other two above park on the event
        # directly and end the instant a `Supervisor._retire` sets it; this one
        # parks on the store, which a `threading.Event` does not notify, so
        # without this a retirement was noticed only when the floor elapsed.
        # `SnapshotStore.wake` is the notification behind the flag; between them
        # they are what turns "the flag is only read between waits" into a wait
        # that reads it.
        self._store.wait_for_change(self._seen_version, timeout=timeout, stop=self._stop_event)

    def _select(self, snapshot: Snapshot) -> tuple[Scene, str, RenderContext]:
        """The scene to draw, its name, and the context both stages agreed on.

        Health first, in two passes rather than one per integration: with two
        sources, one connecting and one stale, a single pass would answer
        whichever `depends_on` -- a `frozenset` -- happened to yield first, and
        string hashing is salted per process, so the same rack would disagree
        with itself across restarts. Connecting outranks stale because it is the
        more specific statement: a source that has never answered has no data to
        have gone stale.

        The context is returned rather than rebuilt by the caller because its
        parameters are expanded *here*, before selection. `render_screen`
        expands before it selects for the same reason: a scene's `when` must see
        the same parameter values its elements will, or a screen can select a
        scene that then declines to draw itself.
        """
        for name in self._screen.depends_on:
            health = snapshot.health.get(name)
            if health is None or health.state is Health.CONNECTING:
                return self._system["connecting"], "connecting", RenderContext()
        for name in self._screen.depends_on:
            health = snapshot.health.get(name)
            if health is not None and health.stale:
                return self._system["stale"], "stale", RenderContext()

        context = expand_params(
            RenderContext(data={**snapshot.data, "params": self._screen.params})
        )
        chosen = select_scene(self._screen.scenes, context)
        if chosen is None:
            # Every scene's condition describes a situation that is not
            # happening. The data is healthy and there is still nothing to draw,
            # which is what `stale` says; a blank panel says nothing at all.
            return self._system["stale"], "stale", RenderContext()
        return chosen, chosen.name, context

    def _should_render(self, version: int, scene_name: str, now: datetime) -> bool:
        if version != self._seen_version:
            return True
        # Against the *selection* behind the frame on the glass, not against the
        # frame itself. They differ only when a render failed: `current_scene`
        # is then `error` while the selection still names the template scene, so
        # comparing the two would never agree and the screen would redraw --
        # and fail, twice, once for each scene -- on every single lap.
        if scene_name != self._selected_scene:
            return True
        if self.last_render is None:
            return True
        # Elapsed seconds, not wall-clock ones: `astimezone(UTC)` is what makes
        # the subtraction ignore a DST shift, the same distinction
        # `seconds_until_boundary` draws. The clock rather than `time.monotonic`
        # because it is the injected one -- which is what lets a test prove the
        # floor without spending it.
        elapsed = (now.astimezone(UTC) - self.last_render.astimezone(UTC)).total_seconds()
        # A negative reading is a clock that stepped backwards, which on a Pi
        # with no RTC is the first NTP sync after every boot. Redrawing is the
        # safe side of that: the alternative is a panel frozen for as long as the
        # step was large, with the floor -- the thing that exists to catch a
        # frozen panel -- being what froze it.
        return elapsed >= self._floor or elapsed < 0

    def _render_and_show(self, scene: Scene, name: str, context: RenderContext) -> None:
        try:
            image = render_scene(scene, context)
        except Exception as exc:  # the renderer promises not to, but a panel must survive it
            message = f"{type(exc).__name__}: {exc}"
            if message != self._logged_error:
                # Once per distinct message, not once per frame: a panel that
                # logs every frame fills a Pi's journal overnight, and the
                # hundred-thousandth copy of a traceback says nothing the first
                # did not. Cleared on the next good frame, so a fault that comes
                # back after a recovery is logged as the new incident it is.
                log.error("render failed", extra={"screen": self.screen_name, "error": message})
                self._logged_error = message
            image = render_scene(
                self._system["error"],
                RenderContext(data={"params": {"message": message[:_ERROR_MESSAGE_CHARS]}}),
            )
            self._show(image, "error", selected=name)
            return
        self._logged_error = None
        self._show(image, name)

    def _backend(self, action: str, call: Callable[[], None]) -> bool:
        """Do one thing to the panel, counting a refusal towards the fault latch.

        Every touch of the backend goes through here -- `show`, `sleep` and
        `wake` alike -- because on the hardware they are all the same SPI bus:
        `GC9A01Display.sleep` and `.wake` reach it through the same `_command`
        as `show` does and raise the same `DisplayError`. A failure that escaped
        instead of being counted would leave the loop in a state it cannot get
        out of: a `sleep` that raised leaves `asleep` unlatched, which puts the
        worker back on the data wait, which the pollers keep satisfying -- 45k
        laps a second, one core, one traceback each, until morning. A `wake`
        that raised is the mirror, and worse: the panel stays dark and nothing
        anywhere says the screen is not fine.
        """
        try:
            call()
        except Exception as exc:
            self._failures += 1
            message = f"{action}: {type(exc).__name__}: {exc}"
            if message != self._logged_backend_error:
                # Once per distinct failure, the same bargain the render path
                # makes: a consecutive run of the identical failure is one line,
                # not one per lap. Cleared on the next good call below, so a
                # panel that *flaps* -- good frame, bad frame, repeating -- does
                # log each failure. That is the intent, not a gap in it: a fault
                # that comes back after a recovery is a new incident, and the
                # loop's own pacing bounds those at about a line per floor.
                log.warning(
                    "display command failed",
                    extra={
                        "screen": self.screen_name,
                        "action": action,
                        "error": str(exc),
                        "attempt": self._failures,
                    },
                )
                self._logged_backend_error = message
            if self._failures >= _MAX_DISPLAY_RETRIES:
                self.faulted = True
                log.error("screen faulted", extra={"screen": self.screen_name, "action": action})
            return False
        self._failures = 0
        self._logged_backend_error = None
        return True

    def _show(self, image: Image.Image, drawn: str, selected: str | None = None) -> None:
        """Put a frame on the glass, and record which selection produced it.

        `drawn` is what is on the panel and `selected` is what the scene
        selection asked for; they differ only for the `error` scene, which is
        drawn in place of a scene that would not render.
        """
        rendered = image
        """The frame as the renderer drew it, before the mount is corrected for.

        What a browser is shown, and deliberately not what goes down the SPI
        bus. `rotation` and `hflip` below compensate for how the panel is bolted
        into the rack, so the image the *glass* takes is transposed precisely so
        that a person standing in front of it sees this one. Sending the
        transposed copy would show the interface a panel lying on its side.
        """
        rotation = self._screen.config.rotation
        if rotation:
            # Negative: the panel is bolted in rotated by `rotation`, and the
            # image has to turn the other way to come out level. Quarter turns
            # only (the schema admits nothing else), so Pillow takes its
            # transpose fast path -- exact pixels, no resampling, and a square
            # image stays 240x240, which the GC9A01 backend refuses to write
            # anything else.
            image = image.rotate(-rotation)
        if self._screen.config.hflip:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

        if not self._backend("show", lambda: self._display.show(image)):
            return

        self.current_scene = drawn
        self._selected_scene = drawn if selected is None else selected
        self.last_render = self._clock()
        self.renders += 1
        # Last, and only on the path where the backend took the frame. A browser
        # showing something the glass refused would be describing a rack that
        # does not exist -- and it is after the counters so that nothing on the
        # way to a browser can change what a status report reads.
        self._offer_frame(rendered)

    def _offer_frame(self, image: Image.Image) -> None:
        """Hand the frame to whoever is streaming this panel. Raises nothing.

        Guarded because of where it runs. This is inside the tick lock on a
        thread whose only job is the panel, and the code behind the callback
        exists for a web page: an exception out of it would leave the screen
        frozen on its last frame with a traceback in the journal blaming the
        renderer, which is the one failure this whole module is built to prevent.

        Once per distinct reason, the same bargain `_render_and_show` makes: a
        frame path that is broken is broken every frame, and a line each fills a
        Pi's journal overnight and buries the first one. Cleared on the next good
        frame, so a fault that returns after a recovery is logged as the new
        incident it is.
        """
        if self._on_frame is None:
            return
        try:
            self._on_frame(image)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._logged_frame_error:
                log.error(
                    "could not offer a frame; the panel is unaffected",
                    extra={"screen": self.screen_name, "error": message},
                )
                self._logged_frame_error = message
            return
        self._logged_frame_error = None
