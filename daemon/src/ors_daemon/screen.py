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
        # Serialises everything that touches the backend. `identify` is called
        # from the CLI's thread by way of the supervisor, and may land while this
        # loop is mid-tick: `show` is several sequential SPI commands (address
        # window, then the frame), so two interleaved writes do not merely race
        # for the last frame -- they corrupt both. Held across the render too, so
        # the counters and `current_scene` a status report reads always describe
        # the frame that is actually on the glass. Not an `RLock`: nothing here
        # re-enters, and a plain lock keeps that provable.
        self._lock = threading.Lock()
        self._seen_version = -1
        self._failures = 0
        self._logged_error: str | None = None

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

    def tick(self) -> None:
        """One loop iteration, without waiting. Total: nothing gets out of here.

        Safe to call from a test, a one-shot render, or the loop below.
        """
        with self._lock:
            self.heartbeat = time.monotonic()
            now = self._clock()

            if in_window(now, self._night):
                if not self.asleep:
                    self._display.sleep()
                    self.asleep = True
                    log.info("night mode", extra={"screen": self.screen_name})
                return

            if self.asleep:
                self._display.wake()
                self.asleep = False

            if self.faulted:
                return

            snapshot = self._store.read()
            scene, name, context = self._select(snapshot)
            if not self._should_render(snapshot.version, name, now):
                return
            self._seen_version = snapshot.version
            self._render_and_show(scene, name, context)

    def identify(self, ordinal: str) -> None:
        """Paint the panel's ordinal, now, from whatever thread asked for it.

        Deliberately not sticky: the next tick draws the screen's real scene
        again, because `current_scene` has moved and the change alone is a
        reason to render. The digit therefore stands for at most one loop wait,
        which is what someone counting panels in a rack needs it to do.
        """
        with self._lock:
            if self.faulted:
                return
            scene = self._system["identify"]
            context = RenderContext(data={"params": {"ordinal": ordinal}})
            self._show(render_scene(scene, context), "identify")

    def run(self) -> None:
        """Draw until stopped. Nothing gets out of here.

        The same rule as the poller's loop and for the same reason: this is a
        daemon thread, and an exception escaping `run` would leave one panel
        frozen on its last frame with nothing anywhere saying why. `tick`
        absorbs what the renderer and the backend do; this catches what it
        cannot -- a store that has broken, a clock that raises.

        Shutdown costs at most one `floor`: the loop is parked in
        `wait_for_change`, which wakes on new data or on its own timeout and
        knows nothing about the stop event. That is bounded and quiet, and the
        alternative -- teaching the store to wake every waiter on shutdown -- is
        the supervisor's to arrange, since it owns both.
        """
        try:
            while not self._stop_event.is_set():
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
        timeout = min(self._floor, seconds_until_boundary(self._clock(), self._night))
        if self.asleep or self.faulted:
            # No data change can produce a frame in either state, and the
            # version has already moved past the one last drawn -- so waiting on
            # it would return instantly, every lap, and spin a core all night.
            self._stop_event.wait(timeout)
            return
        self._store.wait_for_change(self._seen_version, timeout=timeout)

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
        if scene_name != self.current_scene:
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
            name = "error"
        else:
            self._logged_error = None
        self._show(image, name)

    def _show(self, image: Image.Image, name: str) -> None:
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

        try:
            self._display.show(image)
        except Exception as exc:
            self._failures += 1
            log.warning(
                "display write failed",
                extra={
                    "screen": self.screen_name,
                    "error": str(exc),
                    "attempt": self._failures,
                },
            )
            if self._failures >= _MAX_DISPLAY_RETRIES:
                self.faulted = True
                log.error("screen faulted", extra={"screen": self.screen_name})
            return

        self._failures = 0
        self.current_scene = name
        self.last_render = self._clock()
        self.renders += 1
