import contextlib
import threading
import time

import pytest
from ors_daemon.frames import (
    _MAX_TRACKED_SCREENS,
    MAX_FPS,
    FramePump,
    FrameStream,
)
from ors_render import load_builtin_templates, render_scene, select_scene
from ors_render.context import RenderContext
from ors_render.render import expand_params
from ors_schema.link import MAX_FRAME_BYTES, MAX_REQUESTED_FPS, Frame, FramesRequest
from PIL import Image

WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""

PROMPT = 1.0
"""What "the render loop did not wait for the link" is allowed to cost, in seconds.

An order of magnitude below `WAIT`, which is what the parked send is holding, so
the two cannot be confused: a tick that went through the link would spend the
whole of `WAIT` and fail this by a factor of five.
"""


def panel(value: float = 73.4) -> Image.Image:
    """A real 240x240 panel, rendered from a built-in template.

    Not `Image.new(...)`: a solid colour encodes to 262 bytes at these settings
    and a rendered `ring-gauge` to ~5,000, so a fixture that is one flat colour
    turns every size assertion in this file into a tautology. Measured both, and
    the draft of this suite asserted `< 20_000` against the 262-byte one.
    """
    template = load_builtin_templates()["ring-gauge"]
    context = expand_params(
        RenderContext(
            data={
                "prom": {"cpu": value},
                "params": template.bind_params(
                    {"title": "CPU", "value": "{{prom.cpu}}", "big": f"{value:.0f}%"}
                ),
            }
        )
    )
    scene = select_scene(template.scenes, context)
    assert scene is not None
    return render_scene(scene, context)


class Ticker:
    """A monotonic source a test moves by hand. Seconds since nothing in particular.

    Started away from nought so that a stream reading it as "never offered
    before" rather than as a real reading fails visibly, and because
    `time.monotonic` is seconds since boot -- a test whose fake started at zero
    would agree with a machine that has just booted and disagree with every
    other one.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingImage:
    """An image that reports being encoded, and refuses to be anything else.

    It exists to prove a negative: `offer` runs on a screen worker's thread,
    inside the tick lock, so a `save` reaching it at all is a WebP encode on the
    render loop however fast that encode happens to be on this machine.
    """

    def __init__(self) -> None:
        self.saves = 0

    def save(self, buffer: object, **kwargs: object) -> None:
        self.saves += 1
        panel().save(buffer, **kwargs)  # type: ignore[arg-type]


class ParkedSend:
    """A link whose `send` does not return until a test lets it.

    Which is the failure this whole handoff is designed around: `LinkClient.send`
    takes `_send_lock` and is bounded by `SEND_DEADLINE_S` of five seconds, so a
    server that is TCP-alive and has stopped reading parks whoever called it for
    that long. If that caller is a screen worker, the panel freezes.
    """

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.released = threading.Event()
        self.sent: list[Frame] = []

    def __call__(self, frame: Frame) -> bool:
        self.sent.append(frame)
        self.inside.set()
        self.released.wait(WAIT)
        return True


def stream(fps: float = 2.0, **kwargs: object) -> tuple[FrameStream, Ticker]:
    ticker = Ticker()
    return FrameStream(fps=fps, monotonic=ticker, **kwargs), ticker  # type: ignore[arg-type]


def encoded(frames: FrameStream, timeout: float = 0.0) -> list[Frame]:
    """Everything waiting, encoded, the way the pump does it."""
    return [
        frame
        for screen_id, image in frames.take(timeout)
        if (frame := frames.encode(screen_id, image)) is not None
    ]


# --- what is offered at all -------------------------------------------------


def test_nothing_is_offered_while_nobody_is_watching():
    frames, _ = stream()

    assert frames.offer(1, panel()) is False
    assert frames.take(0.0) == []


def test_an_enabled_screen_produces_a_webp_frame():
    frames, _ = stream()
    frames.enable({1})

    assert frames.offer(1, panel()) is True
    [frame] = encoded(frames)
    assert frame.screen_id == 1
    assert frame.webp[:4] == b"RIFF" and frame.webp[8:12] == b"WEBP"


def test_a_screen_nobody_asked_for_is_not_offered():
    frames, _ = stream()
    frames.enable({1})

    assert frames.offer(2, panel()) is False
    assert frames.take(0.0) == []


def test_disabling_stops_the_frames_again():
    frames, _ = stream()
    frames.enable({1})
    frames.disable()

    assert frames.offer(1, panel()) is False


def test_disabling_drops_what_was_waiting_to_be_encoded():
    """A frame held while nobody is watching is a stale panel image if it lands."""
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    frames.disable()

    assert frames.take(0.0) == []


def test_a_screen_taken_out_of_the_set_is_not_encoded_after_the_fact():
    frames, _ = stream()
    frames.enable({1, 2})
    frames.offer(1, panel())
    taken = frames.take(0.0)

    frames.enable({2})

    assert [frames.encode(screen_id, image) for screen_id, image in taken] == [None]


def test_enabling_replaces_the_whole_set():
    """`FramesRequest` is whole-daemon state, which is what task 12 sends."""
    frames, _ = stream()
    frames.enable({1, 2})

    frames.enable({3})

    assert frames.offer(1, panel()) is False
    assert frames.offer(3, panel()) is True


def test_a_frames_request_enables_the_screens_it_names():
    frames, _ = stream()

    frames.request(FramesRequest(enabled=True, screen_ids=[1, 2], fps=1.0))

    assert frames.offer(1, panel()) is True
    assert frames.offer(2, panel()) is True
    assert frames.offer(3, panel()) is False


def test_a_frames_request_that_is_not_enabled_clears_everything():
    frames, _ = stream()
    frames.request(FramesRequest(enabled=True, screen_ids=[1]))

    frames.request(FramesRequest(enabled=False))

    assert frames.offer(1, panel()) is False


def test_a_request_that_is_not_enabled_stops_the_screens_it_still_names():
    """`enabled` decides, not the list beside it.

    `FramesRequest` lets both travel -- `enabled=False, screen_ids=[1]` is a
    well-formed message -- and reading the list first inverts the whole thing:
    the one message that means "stop" would start the screens it named. Survived
    a mutation that dropped the `enabled` branch entirely, because every other
    test of the disabled path sends the empty list a browser happens to send.
    """
    frames, _ = stream()

    frames.request(FramesRequest(enabled=False, screen_ids=[1]))

    assert frames.offer(1, panel()) is False


def test_a_frames_request_naming_no_screens_streams_nothing():
    """`enabled=True, screen_ids=[]` is the last watcher leaving, said the long way."""
    frames, _ = stream()
    frames.request(FramesRequest(enabled=True, screen_ids=[1]))

    frames.request(FramesRequest(enabled=True, screen_ids=[]))

    assert frames.offer(1, panel()) is False


# --- how often --------------------------------------------------------------


def test_the_rate_is_capped():
    frames, ticker = stream(fps=2.0)
    frames.enable({1})

    assert frames.offer(1, panel()) is True
    assert frames.offer(1, panel()) is False, "two frames inside one interval is one frame"

    ticker.advance(0.6)
    assert frames.offer(1, panel()) is True


def test_a_frame_exactly_one_interval_later_is_taken():
    """The boundary belongs to the frame, and 2 fps is 2 fps rather than 1.99.

    Written after `<` became `<=` and the whole suite still passed: every other
    rate test steps either side of the boundary rather than onto it, which is
    exactly the case a fake clock can hit and a real one cannot. Half a second
    is exact in binary, so this really is the boundary and not a neighbour.
    """
    frames, ticker = stream(fps=2.0)
    frames.enable({1})
    frames.offer(1, panel())

    ticker.advance(0.5)

    assert frames.offer(1, panel()) is True


def test_each_screen_keeps_its_own_interval():
    frames, ticker = stream(fps=2.0)
    frames.enable({1, 2})

    assert frames.offer(1, panel()) is True
    assert frames.offer(2, panel()) is True, "one screen's frame is not another's rate"

    ticker.advance(0.4)
    assert frames.offer(1, panel()) is False
    assert frames.offer(2, panel()) is False


def test_the_rate_is_measured_on_elapsed_time_and_never_on_the_wall_clock():
    """The distinction this project has now shipped wrong twice.

    A rate limiter subtracting two readings of a wall clock goes negative at a
    DST fall-back -- so every offer inside the repeated hour passes the check --
    and jumps an hour at a spring-forward, which stamps a screen's `_last` an
    hour into its own future and stops its frames until real time catches up.
    The same is true of an NTP step, which a Pi with no RTC takes on every boot.
    There is no wall clock in this module for either to reach: the default is
    `time.monotonic`, which answers "has enough time elapsed" and nothing else.
    """
    assert FrameStream(fps=2.0)._monotonic is time.monotonic


def test_a_server_cannot_ask_for_more_frames_than_the_cap():
    """Nothing the browser does may slow the panels down.

    `MAX_REQUESTED_FPS` rather than an arbitrary large number, because the two
    bounds are separate on purpose: the schema refuses what is not a rate at
    all, and this cap is what a Pi will actually spend. The fastest rate the
    wire will carry is twelve times what this daemon will do about it.
    """
    frames, ticker = stream()

    frames.request(FramesRequest(enabled=True, screen_ids=[1], fps=MAX_REQUESTED_FPS))

    assert frames.offer(1, panel()) is True
    ticker.advance(1.0 / MAX_FPS / 2)
    assert frames.offer(1, panel()) is False
    ticker.advance(1.0 / MAX_FPS)
    assert frames.offer(1, panel()) is True


def test_a_rate_that_is_not_a_number_produces_no_frames_rather_than_every_frame():
    """The one value `min` and `<` both answer False to, in the wrong direction.

    It cannot reach here off the wire any more -- `FramesRequest.fps` refuses a
    NaN -- and the guard that decides how much of a Pi a browser may spend does
    not get to depend on the peer having validated its own message. Measured
    without it: 100 of 100 offers accepted with the clock frozen, where the cap
    allows one, and sticky for the life of the process because nothing reset it.
    """
    frames, _ = stream()
    frames.enable({1}, fps=float("nan"))

    assert [frames.offer(1, panel()) for _ in range(100)].count(True) == 0


def test_an_infinite_rate_is_the_cap_rather_than_no_rate_at_all():
    frames, ticker = stream()
    frames.enable({1}, fps=float("inf"))

    assert frames.offer(1, panel()) is True
    ticker.advance(1.0 / MAX_FPS / 2)
    assert frames.offer(1, panel()) is False
    ticker.advance(1.0 / MAX_FPS)
    assert frames.offer(1, panel()) is True


def test_an_enable_and_a_disable_are_not_a_way_around_the_cap():
    """The limiter's memory may not be resettable from the far end of the socket.

    `disable` used to drop `_last`, so a server alternating the two messages got
    one frame per offer whatever `MAX_FPS` said: measured at 100 accepted out of
    100 with the clock frozen. It is one message per round trip on an
    authenticated socket, and each accepted offer is a WebP encode on the same
    cores that are drawing four panels.
    """
    frames, _ = stream()
    accepted = 0
    for _ in range(100):
        frames.enable({1})
        if frames.offer(1, panel()):
            accepted += 1
        frames.disable()

    assert accepted == 1, "the clock never moved; one frame was due"


def test_changing_the_watched_set_is_not_a_way_around_the_cap_either():
    """The same hole by the other road, and the reason the fix is not in `disable`.

    Every `frames` request replaces the whole set, so a browser opening and
    closing a second panel walks screen 1's entry out of the table and back in
    without `disable` ever being called.
    """
    frames, _ = stream()
    frames.enable({1})
    assert frames.offer(1, panel()) is True

    frames.enable({2})
    frames.enable({1})

    assert frames.offer(1, panel()) is False, "the clock never moved"


def test_zero_frames_a_second_means_no_frames():
    """Not "no limit", which is what dividing by it the other way would mean."""
    frames, ticker = stream()

    frames.request(FramesRequest(enabled=True, screen_ids=[1], fps=0.0))

    assert frames.offer(1, panel()) is False
    ticker.advance(3_600.0)
    assert frames.offer(1, panel()) is False


def test_a_negative_rate_means_no_frames():
    """Driven through `enable`, because the wire refuses a negative outright.

    `FramesRequest.fps` is bounded at `ge=0.0`, so this is no longer a message
    that can be sent -- and the runtime answer is kept and pinned anyway, which
    is the same defence in depth `_capped` applies to a NaN.
    """
    frames, _ = stream()

    frames.enable({1}, fps=-2.0)

    assert frames.offer(1, panel()) is False


# --- sequence numbers -------------------------------------------------------


def test_sequence_numbers_increase_per_screen():
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1, 2})

    frames.offer(1, panel())
    frames.offer(2, panel())
    first = encoded(frames)
    ticker.advance(1.0)
    frames.offer(1, panel())
    second = encoded(frames)

    by_screen = {frame.screen_id: frame.seq for frame in first}
    assert by_screen == {1: 0, 2: 0}, "each screen counts its own frames"
    assert second[0].seq == by_screen[1] + 1


def test_sequence_numbers_survive_a_disable_and_an_enable():
    """The browser drops frames whose `seq` is not newer than the last it drew.

    A counter that restarted at nought every time the last watcher left would
    make every frame after the next subscribe look stale to a page that is still
    open, and the panel would stay on its last image for as long as the old
    count took to be reached again.
    """
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})
    frames.offer(1, panel())
    before = encoded(frames)[0].seq

    frames.disable()
    frames.enable({1})
    ticker.advance(1.0)
    frames.offer(1, panel())

    assert encoded(frames)[0].seq == before + 1


def test_a_frame_too_large_to_send_takes_no_sequence_number():
    # 400 bytes sits between the two fixtures: a rendered panel is ~5,000 and a
    # flat colour 262, both measured at these encoder settings.
    frames, ticker = stream(fps=MAX_FPS, max_bytes=400)
    frames.enable({1})
    frames.offer(1, panel())

    assert encoded(frames) == []

    ticker.advance(1.0)
    frames.offer(1, Image.new("RGB", (240, 240), (0, 128, 255)))
    assert encoded(frames)[0].seq == 0, "a frame nobody could send is not a frame"


# --- the handoff ------------------------------------------------------------


def test_offering_a_panel_encodes_nothing_on_the_calling_thread():
    """`offer` runs inside the worker's tick lock. It may not encode anything."""
    frames, _ = stream()
    frames.enable({1})
    image = CountingImage()

    assert frames.offer(1, image) is True
    assert image.saves == 0

    encoded(frames)
    assert image.saves == 1


def test_the_newest_panel_wins_when_the_encoder_is_behind():
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})

    frames.offer(1, panel(10.0))
    ticker.advance(1.0)
    frames.offer(1, panel(90.0))

    [(screen_id, waiting)] = frames.take(0.0)
    assert screen_id == 1
    assert waiting.tobytes() == panel(90.0).tobytes(), "a stale panel is worse"


def test_nothing_a_slow_link_does_queues_more_than_one_panel_per_screen():
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1, 2})

    for _ in range(50):
        frames.offer(1, panel())
        frames.offer(2, panel())
        ticker.advance(1.0)

    assert len(frames.take(0.0)) == 2
    assert frames.dropped == 98


# --- what a frame weighs ----------------------------------------------------


def test_a_frame_is_small_enough_to_stream():
    """Measured: a rendered `ring-gauge` is 5,042 bytes at quality 60, method 0.

    The bound is a little over twice that, which is what makes it an assertion:
    the same panel is 17,816 bytes lossless, 18,682 at quality 100 and 18,173 as
    a PNG, so every way of accidentally shipping the wrong encoder fails here.
    Four panels at 2 fps is then ~54 KB/s of base64 off a Pi's wifi.
    """
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    [frame] = encoded(frames)

    assert len(frame.webp) < 12_000
    assert len(frame.webp) <= MAX_FRAME_BYTES


def test_the_two_ends_of_the_link_share_one_bound_on_a_frame():
    """A frame the server refuses to read is a reconnect loop, not a dropped frame."""
    assert FrameStream(fps=2.0)._max_bytes == MAX_FRAME_BYTES


def test_a_frame_over_the_bound_is_dropped_rather_than_sent(caplog):
    frames, _ = stream(max_bytes=64)
    frames.enable({1})
    frames.offer(1, panel())

    with caplog.at_level("ERROR"):
        assert encoded(frames) == []

    assert "frame" in caplog.text


# --- what is remembered about screens that are gone -------------------------


def test_a_panel_nobody_watches_any_more_is_dropped_rather_than_held():
    """`_pending` is the one table that goes with the subscription.

    An image held while nobody is watching is a stale panel picture if anything
    ever sends it, and there is nothing about holding one that a later
    subscriber would want back.
    """
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    frames.enable({2})

    assert frames.take(0.0) == []


def test_when_a_screen_last_sent_a_frame_survives_the_subscription_that_asked():
    """The counterpart, and the reason the cap cannot be reset from the socket.

    This used to be dropped with the subscription, which reads reasonably --
    a screen nobody watches has no rate to limit -- and made every change to the
    watched set a reset of the one thing enforcing `MAX_FPS`. It is bounded by
    weight instead, exactly as `_seq` is.
    """
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    frames.enable({2})

    assert frames._last.keys() == {1}


def test_neither_of_the_tables_kept_per_screen_grows_without_bound():
    """Both, because both are now kept across subscriptions and for the same reason."""
    frames, ticker = stream(fps=MAX_FPS)
    for screen_id in range(_MAX_TRACKED_SCREENS + 10):
        frames.enable({screen_id})
        frames.offer(screen_id, panel())
        encoded(frames)
        ticker.advance(1.0)

    assert len(frames._seq) <= _MAX_TRACKED_SCREENS
    assert len(frames._last) <= _MAX_TRACKED_SCREENS


# --- screens that stop existing ---------------------------------------------


def test_a_retired_screen_is_no_longer_streamed():
    """`apply` deleting a screen is not a message the server's watcher sends.

    Nothing else clears the id: the subscription is whole-daemon state replaced
    only by a `frames` request, so a screen removed from a pushed configuration
    would stay in the set for the life of the connection.
    """
    frames, _ = stream()
    frames.enable({1, 2})

    frames.retire({1})

    assert frames.offer(1, panel()) is False
    assert frames.offer(2, panel()) is True


def test_retiring_a_screen_drops_the_panel_it_was_still_holding():
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    frames.retire({1})

    assert frames.take(0.0) == []


def test_retiring_a_screen_leaves_the_others_watched_and_their_sequences_alone():
    """A row id is remade, so the count may not restart under a page that is open."""
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})
    frames.offer(1, panel())
    assert encoded(frames)[0].seq == 0

    frames.retire({1})
    frames.enable({1})
    ticker.advance(1.0)
    frames.offer(1, panel())

    assert encoded(frames)[0].seq == 1


def test_retiring_nothing_changes_nothing():
    frames, _ = stream()
    frames.enable({1})

    frames.retire(set())

    assert frames.offer(1, panel()) is True


# --- the pump ---------------------------------------------------------------


def pump(frames: FrameStream, send, stop: threading.Event | None = None) -> FramePump:
    return FramePump(frames=frames, send=send, stop=stop or threading.Event(), poll=0.01)


@contextlib.contextmanager
def running(worker: FramePump, frames: FrameStream):
    """Run a pump for the block, and insist that it stops afterwards.

    The insisting is the point, and it is about the run rather than about any
    one test. A pump whose `closed` guard is gone does not fail an assertion --
    it comes straight back round, finds nothing, and spins a core -- so every
    test that merely joined it with a timeout left a live thread behind and went
    green. Enough of those and the whole suite is a `timeout 150` that expires,
    which reports as an exit code rather than as a named failure. One assertion
    here turns that into the first pump test failing by name.
    """
    worker.start()
    try:
        yield worker
    finally:
        frames.close()
        worker.join(WAIT)
        assert not worker.is_alive(), "the pump did not stop when its stream closed"


def test_the_pump_sends_what_a_screen_offers():
    frames, _ = stream()
    frames.enable({1})
    sent: list[Frame] = []
    arrived = threading.Event()

    def send(frame: Frame) -> bool:
        sent.append(frame)
        arrived.set()
        return True

    with running(pump(frames, send), frames):
        frames.offer(1, panel())
        assert arrived.wait(WAIT)
    assert sent[0].screen_id == 1


class SlowImage:
    """An image whose encode a test holds open, and lets go of when it likes.

    A real encode is 0.73ms here and 7-15ms on a Pi, which is too short to
    observe from another thread and long enough to matter four panels at a time.
    This makes the same interval arbitrarily long so that the question "was the
    lock held across it" has an answer a test can read.
    """

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()

    def save(self, buffer: object, **kwargs: object) -> None:
        self.inside.set()
        self.release.wait(WAIT)
        panel().save(buffer, **kwargs)  # type: ignore[arg-type]


def test_a_worker_may_offer_a_panel_while_the_pump_is_halfway_through_an_encode():
    """The invariant this whole module is arranged around, from the only side that can see it.

    `test_offering_a_panel_encodes_nothing_on_the_calling_thread` proves the
    weaker half -- that `offer` does not itself encode -- and that stays true
    when the pump encodes while holding the lock `offer` takes. Moving
    `image.save` inside `with self._condition` passed the entire suite.

    What it costs is the thing the module exists to prevent: `offer` runs inside
    a screen worker's tick lock, so a pump holding this lock across an encode
    puts 7-15ms of Pi into every worker that offers a frame while it is busy --
    four of them serialising behind one encoder, against ~23ms of SPI each --
    and it is spent because somebody left a browser tab open.
    """
    frames, _ = stream(fps=MAX_FPS)
    frames.enable({1, 2})
    slow = SlowImage()

    with running(pump(frames, lambda frame: True), frames):
        try:
            frames.offer(1, slow)
            assert slow.inside.wait(WAIT), "the pump never reached the encode"

            started = time.monotonic()
            assert frames.offer(2, panel()) is True
            elapsed = time.monotonic() - started

            assert not slow.release.is_set(), "the encode finished before it was let go"
            assert elapsed < PROMPT, f"a worker waited {elapsed:.3f}s on somebody else's encode"
        finally:
            slow.release.set()


def test_a_send_that_will_not_return_leaves_the_render_loop_alone():
    """The one the whole design is for.

    `LinkClient.send` holds `_send_lock` for up to `SEND_DEADLINE_S`. A frame
    handed to it from a screen worker's thread would freeze that panel for five
    seconds per frame, go stale under the watchdog, and be torn down and rebuilt
    -- because a browser tab was open.
    """
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})
    parked = ParkedSend()
    with running(pump(frames, parked), frames):
        try:
            assert frames.offer(1, panel()) is True
            assert parked.inside.wait(WAIT), "the pump never reached the link"

            started = time.monotonic()
            for _ in range(20):
                ticker.advance(1.0)
                frames.offer(1, panel())
            elapsed = time.monotonic() - started

            assert parked.inside.is_set() and not parked.released.is_set()
            assert elapsed < PROMPT, f"the render loop waited on the link for {elapsed:.3f}s"
            assert len(frames.take(0.0)) == 1, "a wedged link may not grow a queue"
        finally:
            parked.released.set()


def test_a_send_that_fails_does_not_end_the_pump():
    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})
    seen: list[Frame] = []
    second = threading.Event()

    def send(frame: Frame) -> bool:
        seen.append(frame)
        if len(seen) == 1:
            raise OSError("the socket went away mid-write")
        second.set()
        return True

    with running(pump(frames, send), frames):
        frames.offer(1, panel())
        ticker.advance(1.0)
        for _ in range(200):
            if second.wait(0.02):
                break
            frames.offer(1, panel())
            ticker.advance(1.0)
        assert second.is_set(), "one failed send ended the frame pump"


def test_an_image_that_will_not_encode_does_not_end_the_pump():
    class Unencodable:
        def save(self, buffer: object, **kwargs: object) -> None:
            raise ValueError("no encoder for this")

    frames, ticker = stream(fps=MAX_FPS)
    frames.enable({1})
    arrived = threading.Event()
    with running(pump(frames, lambda frame: bool(arrived.set()) or True), frames):
        frames.offer(1, Unencodable())
        ticker.advance(1.0)
        for _ in range(200):
            if arrived.wait(0.02):
                break
            frames.offer(1, panel())
            ticker.advance(1.0)
        assert arrived.is_set(), "one unencodable panel ended the frame pump"


def test_a_shutdown_is_felt_between_two_frames_rather_than_after_the_batch():
    """One send can be parked for `SEND_DEADLINE_S`, and a rack has four panels.

    A stop checked once per batch is four of those in a row -- twenty seconds
    against a `SHUTDOWN_BUDGET` of ten, spent after the panels are already dark,
    by a thread that is only feeding a browser.
    """
    frames, _ = stream(fps=MAX_FPS)
    frames.enable({1, 2})
    stop = threading.Event()
    sent: list[Frame] = []

    def send(frame: Frame) -> bool:
        sent.append(frame)
        stop.set()  # a SIGTERM landing while this write is on the wire
        return True

    frames.offer(1, panel())
    frames.offer(2, panel())
    worker = pump(frames, send, stop=stop)
    worker.start()
    worker.join(WAIT)

    assert not worker.is_alive()
    assert len(sent) == 1


def test_the_pump_leaves_when_the_daemon_stops():
    frames, _ = stream()
    stop = threading.Event()
    worker = pump(frames, lambda frame: True, stop=stop)
    worker.start()

    stop.set()
    worker.join(WAIT)

    assert not worker.is_alive()


def test_the_pump_leaves_when_the_stream_closes():
    frames, _ = stream()
    worker = pump(frames, lambda frame: True)
    worker.start()

    frames.close()
    worker.join(WAIT)

    assert not worker.is_alive()


def test_a_closed_stream_offers_nothing():
    frames, _ = stream()
    frames.enable({1})
    frames.close()

    assert frames.offer(1, panel()) is False


def test_a_closed_stream_hands_over_nothing_it_was_already_holding():
    """Closing happens on the way down, after the panels are slept and closed.

    A frame that got out after that describes a rack that is no longer drawing
    anything, and it is one more send between a SIGTERM and the process exiting.
    """
    frames, _ = stream()
    frames.enable({1})
    frames.offer(1, panel())

    frames.close()

    assert frames.take(0.0) == []


@pytest.mark.parametrize("fps", [0.5, 2.0, MAX_FPS])
def test_the_interval_is_the_reciprocal_of_the_rate(fps: float):
    frames, ticker = stream(fps=fps)
    frames.enable({1})
    frames.offer(1, panel())

    ticker.advance(1.0 / fps * 0.99)
    assert frames.offer(1, panel()) is False
    ticker.advance(1.0 / fps * 0.02)
    assert frames.offer(1, panel()) is True
