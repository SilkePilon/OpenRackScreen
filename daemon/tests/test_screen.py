import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.config import ResolvedScreen, system_scenes
from ors_daemon.screen import _NIGHT_PARK_CHUNK as NIGHT_PARK_CHUNK
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_render import load_builtin_templates, render_scene
from ors_schema.daemon import DisplayConfig, NightWindow, ScreenConfig
from ors_schema.scene import Scene
from PIL import Image

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 8, 11, 23, 30, tzinfo=UTC)
NIGHT_WINDOW = NightWindow(start="23:00", end="07:00")
UNTIL_MORNING = 7.5 * 3600
"""Seconds from `NIGHT` to the end of `NIGHT_WINDOW`."""
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""

RING_PARAMS = {"title": "CPU", "value": "{{prom.cpu}}", "big": "42%"}


class RecordingDisplay:
    """A panel that counts what it was asked to do, and can refuse any of it.

    Every backend entry point can fail, because on real hardware every one of
    them is the same SPI bus: `sleep` and `wake` reach it through the same
    `_command` that `show` does, and raise the same `DisplayError`.
    """

    def __init__(self, fail_times: int = 0, fail_sleeps: int = 0, fail_wakes: int = 0) -> None:
        self.images: list[Image.Image] = []
        self.sleeps = 0
        self.wakes = 0
        self.closed = 0
        self.fail_times = fail_times
        self.fail_sleeps = fail_sleeps
        self.fail_wakes = fail_wakes
        self.on_show: Callable[[], None] | None = None

    def show(self, image: Image.Image) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("SPI write failed")
        self.images.append(image)
        if self.on_show is not None:
            self.on_show()

    def sleep(self) -> None:
        self.sleeps += 1
        if self.fail_sleeps:
            self.fail_sleeps -= 1
            raise OSError("SPI command 0x10 failed")

    def wake(self) -> None:
        self.wakes += 1
        if self.fail_wakes:
            self.fail_wakes -= 1
            raise OSError("SPI command 0x11 failed")

    def close(self) -> None:
        self.closed += 1


class RecordingStop:
    """A stop event that records what it was asked to wait for, and waits for none of it."""

    def __init__(self) -> None:
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return False


class LockProbeDisplay(RecordingDisplay):
    """Asks, from inside `show`, whether the worker's render lock is held.

    `threading.Lock` is not reentrant, so a non-blocking acquire from the very
    thread that holds it fails -- which is what makes this a proof rather than a
    timing window: no second thread, and nothing to wait for.
    """

    def __init__(self) -> None:
        super().__init__()
        self.worker: ScreenWorker | None = None
        self.free_during_show: bool | None = None

    def show(self, image: Image.Image) -> None:
        assert self.worker is not None
        acquired = self.worker._lock.acquire(blocking=False)
        if acquired:
            self.worker._lock.release()
        self.free_during_show = acquired
        super().show(image)


def make(
    params: dict[str, Any] | None = None,
    night: NightWindow | None = None,
    clock: FakeClock | None = None,
    display: Any = None,
    template: str = "ring-gauge",
    depends: tuple[str, ...] = ("prom",),
    floor: float = 5.0,
) -> tuple[ScreenWorker, SnapshotStore, Any]:
    resolved = ResolvedScreen(
        config=ScreenConfig(
            name="CPU",
            position=1,
            display=DisplayConfig(backend="virtual", out_dir="/tmp"),
            rotation=0,
            hflip=False,
            template=template,
            params={},
        ),
        scenes=list(load_builtin_templates()[template].scenes),
        params=load_builtin_templates()[template].bind_params(
            RING_PARAMS if params is None else params
        ),
        depends_on=frozenset(depends),
    )
    store = SnapshotStore()
    store.register("prom")
    worker = ScreenWorker(
        screen=resolved,
        store=store,
        display=display or RecordingDisplay(),
        system=system_scenes(),
        night=night or NightWindow(enabled=False),
        stop=threading.Event(),
        clock=clock or FakeClock(NOW),
        floor=floor,
    )
    return worker, store, worker._display


def publish(store: SnapshotStore, cpu: float = 42.0) -> None:
    store.put("prom", {"cpu": cpu}, latency_ms=1.0, now=NOW)


def test_a_screen_with_no_data_yet_shows_the_connecting_scene() -> None:
    worker, _, display = make()
    worker.tick()

    assert worker.current_scene == "connecting"
    assert len(display.images) == 1


def test_a_stale_source_shows_the_stale_scene() -> None:
    worker, store, _ = make()
    publish(store)
    for _ in range(3):
        store.fail("prom", "timeout", now=NOW)
    worker.tick()

    assert worker.current_scene == "stale"


def test_healthy_data_selects_a_template_scene() -> None:
    worker, store, _ = make()
    publish(store)
    worker.tick()

    assert worker.current_scene not in {"connecting", "stale", "error"}


def test_a_screen_depending_on_nothing_never_waits_for_an_integration() -> None:
    worker, _, _ = make(params={"big": "HELLO"}, template="text-only", depends=())
    worker.tick()

    assert worker.current_scene == "default"


def test_nothing_is_redrawn_while_data_and_scene_are_unchanged() -> None:
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    publish(store)
    worker.tick()
    worker.tick()
    worker.tick()

    assert len(display.images) == 1


def test_new_data_triggers_exactly_one_redraw() -> None:
    worker, store, display = make()
    publish(store)
    worker.tick()
    publish(store, cpu=43.0)
    worker.tick()

    assert len(display.images) == 2


def test_the_heartbeat_floor_redraws_a_frozen_screen() -> None:
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    publish(store)
    worker.tick()
    clock.advance(5.1)
    worker.tick()

    assert len(display.images) == 2


def test_a_clock_stepped_backwards_does_not_freeze_the_floor() -> None:
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    publish(store)
    worker.tick()
    clock.advance(-3600)
    worker.tick()

    assert len(display.images) == 2


def test_entering_the_night_window_sleeps_the_panel_and_stops_rendering() -> None:
    clock = FakeClock(NIGHT)
    worker, store, display = make(night=NIGHT_WINDOW, clock=clock)
    publish(store)

    worker.tick()
    worker.tick()

    assert display.sleeps == 1, "sleep is sent once, not every tick"
    assert display.images == []
    assert worker.asleep is True


def test_leaving_the_night_window_wakes_the_panel_and_renders() -> None:
    clock = FakeClock(NIGHT)
    worker, store, display = make(night=NIGHT_WINDOW, clock=clock)
    publish(store)
    worker.tick()

    clock.advance(9 * 3600)
    worker.tick()

    assert display.wakes == 1
    assert len(display.images) == 1


def test_a_night_window_narrower_than_the_floor_still_draws_on_the_way_out() -> None:
    # A one-minute window under a five-minute floor: legal config, and exactly
    # what someone testing their night window sets. Nothing else moves -- the
    # version, the scene and the floor are all where they were before the
    # window -- so waking is the only thing that can put a frame up.
    clock = FakeClock(datetime(2026, 8, 11, 22, 59, 30, tzinfo=UTC))
    worker, store, display = make(
        night=NightWindow(start="23:00", end="23:01"), clock=clock, floor=300.0
    )
    publish(store)
    worker.tick()
    clock.advance(60)
    worker.tick()
    assert worker.asleep is True

    clock.advance(60)
    worker.tick()

    assert display.wakes == 1
    assert len(display.images) == 2, "leaving the window draws; it does not wait for the floor"


def test_a_per_screen_override_replaces_the_global_window() -> None:
    clock = FakeClock(NIGHT)
    worker, store, display = make(night=NIGHT_WINDOW, clock=clock)
    worker._night = NightWindow(enabled=False)
    publish(store)
    worker.tick()

    assert display.sleeps == 0
    assert len(display.images) == 1


def test_a_failing_sleep_faults_the_screen_instead_of_spinning() -> None:
    clock = FakeClock(NIGHT)
    display = RecordingDisplay(fail_sleeps=99)
    worker, store, _ = make(display=display, night=NIGHT_WINDOW, clock=clock)
    publish(store)

    for _ in range(4):
        worker.tick()

    assert worker.faulted is True
    assert worker.asleep is False, "a sleep that failed did not put the panel to sleep"
    assert display.sleeps == 3, "retried up to the fault latch, then left alone"


def test_a_failing_wake_faults_the_screen_rather_than_stranding_it_in_the_dark() -> None:
    clock = FakeClock(NIGHT)
    display = RecordingDisplay(fail_wakes=99)
    worker, store, _ = make(display=display, night=NIGHT_WINDOW, clock=clock)
    publish(store)
    worker.tick()
    clock.advance(9 * 3600)

    for _ in range(4):
        worker.tick()

    assert worker.faulted is True, "a screen nobody can wake must not report as healthy"
    assert display.wakes == 3
    assert display.images == []


def test_a_faulted_screen_is_not_even_put_to_sleep() -> None:
    clock = FakeClock(NOW)
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display, night=NIGHT_WINDOW, clock=clock)
    publish(store)
    for _ in range(3):
        worker.tick()
    assert worker.faulted is True

    clock.advance(11.5 * 3600)
    worker.tick()

    assert display.sleeps == 0, "sleep is the same bus write that just failed three times"


def test_a_display_failure_retries_then_faults_the_screen_without_raising() -> None:
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    publish(store)

    for _ in range(4):
        worker.tick()

    assert worker.faulted is True


def test_a_faulted_screen_stops_touching_its_backend() -> None:
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    publish(store)
    for _ in range(4):
        worker.tick()
    before = display.fail_times

    worker.tick()
    assert display.fail_times == before


def test_identify_renders_the_ordinal_immediately() -> None:
    worker, _, display = make()
    worker.identify("2")

    assert worker.current_scene == "identify"
    assert len(display.images) == 1


def test_the_next_tick_takes_the_panel_back_off_the_identify_digit() -> None:
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    publish(store)
    worker.tick()
    worker.identify("2")

    worker.tick()

    assert worker.current_scene == "default", "the digit stands for one loop wait, not forever"
    assert len(display.images) == 3


def test_identify_leaves_a_faulted_backend_alone() -> None:
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    publish(store)
    for _ in range(4):
        worker.tick()
    before = display.fail_times

    worker.identify("2")

    assert display.fail_times == before


@pytest.mark.parametrize(("rotation", "hflip"), [(0, False), (90, False), (270, True), (180, True)])
def test_rotation_and_flip_are_applied_before_the_backend_sees_the_image(
    rotation: int, hflip: bool
) -> None:
    worker, store, display = make()
    worker._screen.config.rotation = rotation
    worker._screen.config.hflip = hflip
    publish(store)
    worker.tick()

    assert display.images[0].size == (240, 240)


def _breaks_on(name: str) -> Callable[..., Image.Image]:
    """A renderer that fails for one scene and draws every other one for real."""

    def render(scene: Scene, ctx: Any, **kwargs: Any) -> Image.Image:
        if scene.name == name:
            raise ValueError("element exploded")
        return render_scene(scene, ctx, **kwargs)

    return render


def test_a_render_failure_paints_the_error_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, store, display = make()
    monkeypatch.setattr("ors_daemon.screen.render_scene", _breaks_on("default"))
    publish(store)

    worker.tick()

    assert worker.current_scene == "error"
    assert len(display.images) == 1


def test_the_same_render_failure_is_logged_once_not_once_per_frame(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker, store, display = make()
    monkeypatch.setattr("ors_daemon.screen.render_scene", _breaks_on("default"))
    publish(store)

    with caplog.at_level(logging.ERROR, logger="ors_daemon.screen"):
        for cpu in (43.0, 44.0, 45.0):
            worker.tick()
            publish(store, cpu=cpu)

    assert len(display.images) == 3, "the error scene is repainted, so this is not deduplication"
    assert len(caplog.records) == 1


def test_an_erroring_screen_is_paced_like_a_healthy_one(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, store, display = make()
    monkeypatch.setattr("ors_daemon.screen.render_scene", _breaks_on("default"))
    publish(store)

    worker.tick()
    worker.tick()
    worker.tick()

    assert len(display.images) == 1, (
        "the error frame is on the glass, but the selection that produced it has not moved"
    )


def test_repeated_identical_backend_failures_are_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    publish(store)

    with caplog.at_level(logging.WARNING, logger="ors_daemon.screen"):
        for _ in range(3):
            worker.tick()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert [record.message for record in caplog.records if record.levelno == logging.ERROR] == [
        "screen faulted"
    ]


def test_a_failure_that_comes_back_after_a_good_frame_is_logged_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker, store, _ = make()
    publish(store)

    with caplog.at_level(logging.ERROR, logger="ors_daemon.screen"):
        monkeypatch.setattr("ors_daemon.screen.render_scene", _breaks_on("default"))
        worker.tick()
        monkeypatch.setattr("ors_daemon.screen.render_scene", render_scene)
        # New data before each of the two frames below: an erroring screen is
        # paced like any other, so without a reason to redraw there is nothing
        # to recover from and then nothing to fail again.
        publish(store, cpu=43.0)
        worker.tick()
        monkeypatch.setattr("ors_daemon.screen.render_scene", _breaks_on("default"))
        publish(store, cpu=44.0)
        worker.tick()

    assert len(caplog.records) == 2, "a recurrence after a recovery is a new incident"


def test_a_tick_holds_the_render_lock_across_the_backend_write() -> None:
    display = LockProbeDisplay()
    worker, store, _ = make(display=display)
    display.worker = worker
    publish(store)

    worker.tick()

    assert display.free_during_show is False


def test_identify_holds_the_same_lock_a_tick_does() -> None:
    display = LockProbeDisplay()
    worker, _, _ = make(display=display)
    display.worker = worker

    worker.identify("2")

    assert display.free_during_show is False


def _forbidden(version: int, timeout: float) -> bool:
    raise AssertionError(f"the store was waited on: {version=} {timeout=}")


@pytest.mark.parametrize("state", ["asleep", "faulted"])
def test_a_worker_that_cannot_draw_waits_on_the_clock_not_on_the_data(state: str) -> None:
    worker, store, _ = make()
    setattr(worker, state, True)
    # The version is now well past the one the worker last drew, so waiting for
    # it to change would return at once, every lap, forever.
    publish(store)
    store.wait_for_change = _forbidden  # type: ignore[method-assign]
    worker._stop_event.set()

    worker._wait()


def test_a_sleeping_worker_parks_in_chunks_a_watchdog_can_live_with() -> None:
    clock = FakeClock(NIGHT)
    worker, store, _ = make(night=NIGHT_WINDOW, clock=clock)
    worker.tick()
    assert worker.asleep is True
    stop = RecordingStop()
    worker._stop_event = stop  # type: ignore[assignment]
    store.wait_for_change = _forbidden  # type: ignore[method-assign]

    worker._wait()

    assert stop.waits == [NIGHT_PARK_CHUNK], "bounded, so the heartbeat keeps moving"


def test_a_sleeping_worker_keeps_its_heartbeat_moving_through_the_night() -> None:
    clock = FakeClock(NIGHT)
    worker, store, display = make(night=NIGHT_WINDOW, clock=clock)
    publish(store)
    stop = RecordingStop()
    worker._stop_event = stop  # type: ignore[assignment]
    # Nothing in a sleeping loop may reach the store: that wait is the only one
    # here that blocks on a real condition variable, and it would be five
    # seconds of genuine sleep dressed up as a passing test.
    store.wait_for_change = _forbidden  # type: ignore[method-assign]

    # The real loop, driven by hand: tick, park, and move the clock on by
    # whatever was parked for -- from lights-out until the panel wakes again.
    stamps = []
    laps = 0
    while True:
        worker.tick()
        stamps.append(worker.heartbeat)
        if not worker.asleep:
            break
        worker._wait()
        clock.advance(stop.waits[-1])
        laps += 1
        assert laps < 10_000, "the night never ended"

    assert max(stop.waits) <= NIGHT_PARK_CHUNK, (
        "one long park leaves the heartbeat frozen, and the watchdog reads exactly that"
    )
    assert laps >= UNTIL_MORNING / NIGHT_PARK_CHUNK
    assert stamps[-1] > stamps[0], "the heartbeat moved across the night"
    assert display.wakes == 1, "and the night still ended on time"


def test_a_wake_that_failed_is_retried_at_the_floor_not_at_the_next_nightfall() -> None:
    clock = FakeClock(NIGHT)
    display = RecordingDisplay(fail_wakes=99)
    worker, store, _ = make(display=display, night=NIGHT_WINDOW, clock=clock)
    worker.tick()
    clock.advance(9 * 3600)
    worker.tick()
    assert worker.asleep is True, "the wake failed, so the panel is still dark"
    stop = RecordingStop()
    worker._stop_event = stop  # type: ignore[assignment]

    worker._wait()

    assert stop.waits == [5.0], "parking on the boundary would leave it dark until nightfall"


def test_a_drawing_worker_waits_for_the_data_it_last_drew_to_change() -> None:
    worker, store, _ = make()
    calls: list[tuple[int, float]] = []

    def record(version: int, timeout: float) -> bool:
        calls.append((version, timeout))
        return False

    store.wait_for_change = record  # type: ignore[method-assign]
    publish(store)
    worker.tick()
    worker._wait()

    assert calls == [(worker._seen_version, 5.0)]


def test_the_loop_closes_the_panel_on_the_way_out() -> None:
    worker, _, display = make()
    worker._stop_event.set()

    worker.run()

    assert display.closed == 1


def test_a_wait_that_blows_up_does_not_take_the_thread_down() -> None:
    worker, store, display = make()

    def boom(version: int, timeout: float) -> bool:
        # Set from in here so the guard's own fallback wait returns at once:
        # the point is that the loop survives, not how long it pauses for.
        worker._stop_event.set()
        raise RuntimeError("the condition variable is broken")

    store.wait_for_change = boom  # type: ignore[method-assign]

    worker.run()

    assert worker.renders == 1
    assert display.closed == 1


def test_the_thread_draws_until_it_is_told_to_stop() -> None:
    display = RecordingDisplay()
    drawn = threading.Event()
    display.on_show = drawn.set
    # A floor this short only bounds how long the loop sits in `wait_for_change`
    # after the stop event is set; nothing in the test waits for it to elapse.
    worker, store, _ = make(display=display, floor=0.05)

    worker.start()
    try:
        publish(store)
        assert drawn.wait(WAIT), "the loop never drew anything"
    finally:
        worker._stop_event.set()
        worker.join(WAIT)

    assert not worker.is_alive()
    assert worker.renders >= 1
    assert display.closed == 1
