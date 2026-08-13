"""Panels on their way to a browser, and everything that keeps them off the glass.

This is the first thing in the daemon that exists for somebody looking at a web
page rather than for the rack, so it works under one rule that overrides every
other consideration here: *nothing a browser does may slow a panel down, and no
failure of the link may darken one.* Three decisions follow from it and live
nowhere else.

*The render thread does not encode and does not send.* `offer` is called from
inside `ScreenWorker`'s tick lock. It takes a lock, checks a set, compares two
floats and stores one reference -- microseconds -- and the WebP encode happens
on `FramePump`'s thread afterwards. Measured on a dev machine, a rendered
`ring-gauge` costs 0.73ms to encode at these settings; a Pi 3B+ is roughly an
order of magnitude slower, so ~7-15ms, against ~23ms of SPI for the frame it
would be sharing a lock with. That is the smaller half of the argument. The
larger half is `LinkClient.send`, which holds `_send_lock` for up to
`SEND_DEADLINE_S` of five seconds against a server that has stopped reading:
called from a worker, it freezes that panel for five seconds a frame, stales the
heartbeat the watchdog reads, and gets the worker torn down and rebuilt -- all
because a browser tab was open. A thread is needed for the send whatever happens
to the encode, and once there is one the encode belongs on it too.

*Frames are dropped, never queued and never waited on.* One image per screen is
held, and a newer one replaces it rather than joining it. A queue would be
memory this end does not control, sized by how slow the network is, holding
images that are stale by the time anything could see them. `seq` is what lets
the far end tell which frame is the newest, and it is why dropping is safe.

*Off by default, and the server has to ask.* A rack nobody is watching pays for
this module a set membership test per rendered frame, and that is the ordinary
case -- `offer` returning False is not an error path.
"""

from __future__ import annotations

import io
import logging
import math
import threading
import time
from collections.abc import Callable

from ors_schema.link import MAX_FRAME_BYTES, Frame, FramesRequest
from PIL import Image

log = logging.getLogger(__name__)

QUALITY = 60
"""WebP quality. What a 240x240 panel costs to look at, in bytes.

Sixty because the consumer is a circular 240px preview in a browser and the
producer is a Pi: a rendered `ring-gauge` is 5,042 bytes here against 7,642 at
quality 90 and 18,682 at 100, and nothing at this size distinguishes the three
on a screen. Lossless is 17,816 -- and this is a photograph of a panel, not the
panel itself, so nothing downstream needs the pixels back exactly.
"""

METHOD = 0
"""libwebp's fastest analysis pass, deliberately.

Method 4 (the library default) makes the same panel 3,604 bytes instead of
5,042 -- 29% smaller -- for 2.20ms instead of 0.73ms, three times the CPU. On
this rack the scarce resource is the Pi and not the LAN: 1.4 KB per frame at 2
fps is 11 KB/s that a wifi link does not notice, and the milliseconds are taken
out of the same four cores that are drawing four panels.
"""

MAX_FPS = 5.0
"""The most frames a second this daemon will produce for one screen, whatever it is asked for.

`FramesRequest.fps` is a number chosen at the far end of an authenticated
socket, and an unbounded one is a browser deciding how much of a Pi's CPU the
rack may spend on being looked at. Four panels at this cap is twenty encodes a
second, which at the 7-15ms an encode costs on a Pi 3B+ is between a seventh and
a third of one core -- corrected from "a tenth", which was about two times
optimistic against this module's own measurement. It is a ceiling rather than a
rate: nothing here can produce a frame the render loop did not draw, and a
static rack draws about 0.8 times a second.

Five rather than something cinematic because these are monitoring panels: their
data arrives at a poll interval measured in seconds, and the interface is
showing what is on the glass, not animating it.

`FramesRequest.fps` is bounded in the schema as well, and neither bound makes
the other redundant. The schema's is about the *message* -- it is what stops a
NaN or an infinity parsing at all, which is a value no comparison here can
refuse -- and this one is about this hardware. A build for something other than
a Pi would move this number and leave that one alone.
"""

PUMP_POLL_S = 0.5
"""How long the pump may park before it looks at the stop event, in seconds.

Not a rate: it is woken by every offer. It exists so that a shutdown is felt by
a thread that has been idle for hours, in the same way `RECV_TIMEOUT_S` does it
for the link. Half a second because the wake costs a lock and a dictionary
check, and because `close()` is what makes an ordinary shutdown immediate --
this is only the floor under a caller that forgets.
"""

_MAX_TRACKED_SCREENS = 256
"""How many screens' sequence numbers and frame times are remembered at once.

Bounded because a screen id is a row id on somebody else's database, and rows
are deleted and remade: over the life of a daemon that is never restarted, the
set of ids that have ever been watched only grows. Pruned to what is currently
enabled when it passes this, and only then -- which is the whole rule for both
tables, and for each of them the "only then" is the load-bearing half.

For `_seq`, because the counter is exactly what a receiver uses to drop stale
frames: dropping it for a screen somebody is still watching would restart the
count under them and make every later frame look older than the one on their
page.

For `_last`, because it is the rate limiter's memory and forgetting it is how
the cap is bypassed. It used to go with the subscription, which reads
reasonably -- a screen nobody watches has no rate to limit -- and made
`enable`/`disable` a reset button on the far end of the socket: measured, 100
offers accepted out of 100 with the clock frozen, against a cap that allows one.
The same is true of `enable({1}); enable({2}); enable({1})`, so it is the
subscription change and not `disable` that had to stop clearing it.

Two hundred and fifty-six is two orders of magnitude past a rack, so nothing
that is watching a rack ever reaches it.
"""


class FrameStream:
    """Decides which panels become frames, and hands them over without encoding them.

    Two threads meet here and the division between them is the point. Screen
    workers call `offer`, from inside their tick locks, and it does no work. The
    pump calls `take` and `encode`, and does all of it.
    """

    def __init__(
        self,
        fps: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        max_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        """`monotonic` rather than the `Clock` every other component here takes.

        The question this asks is "has enough time elapsed since the last frame",
        which is the same question `ScreenWorker.heartbeat` and the link's send
        deadline ask, and both of those are monotonic for the same reason: an
        NTP step must not be able to answer it. `Clock` is a wall clock --
        `datetime.now(zone)` on a Pi with no RTC, which steps on every boot's
        first sync -- and subtracting two readings of one stamps a screen's last
        frame in its own future whenever the step is backwards, which stops that
        screen's frames until real time catches up. Reading `.timestamp()` off it
        removes the DST half of that hazard (an epoch second is an instant, not a
        wall-clock field, so it does not repeat an hour or skip one) and leaves
        the NTP half untouched. Injected, rather than called directly, so a test
        can prove an interval without spending it.

        `max_bytes` is a parameter and not the constant read inline so that a
        test can drive the over-large path with a real panel instead of
        manufacturing a quarter of a megabyte of image.
        """
        self._fps = _capped(fps)
        self._monotonic = monotonic
        self._max_bytes = max_bytes
        # One condition, guarding every field below, and also what the pump
        # parks on. A second lock would be a second thing to reason about for a
        # class whose whole state is four small dictionaries.
        self._condition = threading.Condition()
        self._enabled: set[int] = set()
        self._pending: dict[int, Image.Image] = {}
        self._last: dict[int, float] = {}
        self._seq: dict[int, int] = {}
        self._closed = False

        self.dropped = 0
        """Panels replaced before anything could encode them, since this started.

        The measure of how far behind the link is running, and published because
        nothing else can see it: a dropped frame is by design invisible
        everywhere else. Never reset, so a rack that has been up for a month
        reads as a total rather than as a rate.
        """

    def request(self, request: FramesRequest) -> None:
        """Do what the server just asked for. The `FramesHandler` the link calls.

        `FramesRequest` is whole-daemon state and not a per-screen toggle: its
        `screen_ids` is the complete set of screens anyone is watching, so it
        *replaces* what this daemon was streaming rather than adding to it, and
        `enabled=False` clears the lot. Task 12 sends it that way -- the
        alternative, a message per subscriber, freezes the other three panels of
        a four-panel rack canvas the moment a fifth view is opened.
        """
        if not request.enabled:
            self.disable()
            return
        self.enable(set(request.screen_ids), fps=request.fps)

    def enable(self, screen_ids: set[int], fps: float | None = None) -> None:
        """Stream exactly these screens, at most this often. Replaces the set."""
        with self._condition:
            self._enabled = set(screen_ids)
            if fps is not None:
                self._fps = _capped(fps)
            self._forget_unwatched()

    def disable(self) -> None:
        """Stop streaming everything.

        What is already waiting to be encoded goes with it. A frame held while
        nobody is watching is a stale panel image if anything ever sends it, and
        the browser that asked for it has gone.

        What does *not* go with it is `_last`. See `_MAX_TRACKED_SCREENS`: the
        rate limiter's memory is the only thing standing between a browser and
        four panels' worth of encodes, and clearing it here made an
        enable/disable loop a way of asking for every frame the render loop
        draws. Nothing is lost by keeping it -- the readings are monotonic, so a
        screen re-enabled a minute later is due a frame immediately either way.

        Three things call this: a `frames` request that says `enabled=False`,
        the link when a connection drops, and `run` on the way down. The middle
        one matters most, because a subscription is not a fact about the socket
        that carried it: without it, a Pi whose server has gone away goes on
        encoding WebP for a browser that cannot be reached until the link comes
        back and says otherwise, which on a flapping wifi link is most of the
        time.
        """
        with self._condition:
            self._enabled = set()
            self._forget_unwatched()

    def retire(self, screen_ids: set[int]) -> None:
        """Stop streaming these screens, because they no longer exist.

        For `Supervisor.apply`, and the one caller that knows something the
        server's subscription cannot: a screen deleted from a pushed
        configuration has no worker, no panel and no row, so a subscription that
        still names it is a subscription to nothing. Without this the id stays
        in `_enabled` for as long as the connection lasts -- harmless in the
        sense that nothing offers frames for it, and not harmless in the sense
        that `_MAX_TRACKED_SCREENS` prunes to what is *enabled*, so a rack
        reconfigured often enough would protect ids that had been gone for
        months and drop ones that had not.

        Deliberately not a `disable`: the other screens are still watched. And
        deliberately not `_seq`, for the reason `_MAX_TRACKED_SCREENS` gives --
        a row id is reused, and a receiver cannot tell a restarted counter from
        a flood of stale frames.
        """
        if not screen_ids:
            return
        with self._condition:
            self._enabled -= screen_ids
            for screen_id in screen_ids:
                self._pending.pop(screen_id, None)

    def close(self) -> None:
        """Nothing further will be streamed. Releases whoever is parked in `take`."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        """Whether this stream has been closed. What ends the pump's loop.

        A closed stream never parks in `take` again -- it has nothing to wait
        for and nothing to deliver -- so a pump that only watched the stop event
        would come straight back round, find nothing, and spin a whole core on a
        machine that is trying to shut down. The same shape of bug as the one
        `ScreenWorker.run` documents about a closed store.
        """
        with self._condition:
            return self._closed

    def offer(self, screen_id: int, image: Image.Image) -> bool:
        """Hand over a panel that has just reached the glass. True if it was taken.

        Called on a screen worker's thread, with that worker's tick lock held, so
        everything expensive is deliberately absent: no encode, no send, no
        allocation beyond one dictionary entry, and nothing that can block on
        anything but this object's own lock. False is the ordinary answer.

        The image is stored rather than copied. `ScreenWorker` renders a fresh
        one every frame and keeps no reference to it once `_show` returns, and
        Pillow's rotate and transpose return new images rather than editing
        theirs, so nothing can mutate this one underneath the pump.
        """
        with self._condition:
            if self._closed or screen_id not in self._enabled:
                return False
            interval = self._interval
            if interval is None:
                return False
            now = self._monotonic()
            last = self._last.get(screen_id)
            if last is not None and now - last < interval:
                return False
            self._last[screen_id] = now
            if screen_id in self._pending:
                # Replaced, not queued. The newest frame wins, which is the same
                # bargain the server's hub makes with a slow watcher: a stale
                # panel image is worse than a skipped one.
                self.dropped += 1
            self._pending[screen_id] = image
            self._condition.notify()
            return True

    def take(self, timeout: float) -> list[tuple[int, Image.Image]]:
        """Everything waiting, at most one panel per screen. Parks until there is some.

        For the pump and nothing else. Returns an empty list when the wait
        expires or when the stream has been closed, so the caller's loop is the
        thing that decides whether to go round again.
        """
        with self._condition:
            if not self._pending and not self._closed:
                self._condition.wait(timeout)
            waiting = list(self._pending.items())
            self._pending.clear()
            return waiting

    def encode(self, screen_id: int, image: Image.Image) -> Frame | None:
        """One panel as a `Frame`, or None if it should not be sent after all.

        The expensive half of this class, and it runs on the pump's thread.

        Two things can still refuse a panel here, both of them late on purpose.
        The screen may have stopped being watched between the offer and now,
        which costs nothing to check and means a subscription that ends really
        does end the frames. And the encode may be larger than either end of the
        link will carry, which is dropped and logged rather than sent: a message
        past a reader's limit closes the socket, and a rack that reconnects,
        gets pushed to and sends the same frame again is a loop nothing recovers
        from. A frame nobody could send takes no sequence number, so the count
        stays a count of frames that were actually produced.
        """
        # Outside `self._condition`, and that is this module's whole thesis
        # rather than an accident of where the line sits. This lock is the one a
        # screen worker takes in `offer`, from inside its own tick lock: holding
        # it across the encode would move 7-15ms of Pi onto every worker that
        # offered a frame while the pump was busy, four of them serialising
        # behind one encoder, against ~23ms of SPI each. The lock is taken below
        # for the four dictionary operations it exists for, once the bytes are
        # already made.
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=QUALITY, method=METHOD)
        webp = buffer.getvalue()
        if len(webp) > self._max_bytes:
            log.error(
                "a frame was too large to send and has been dropped",
                extra={"screen": screen_id, "bytes": len(webp), "allowed": self._max_bytes},
            )
            return None
        with self._condition:
            if screen_id not in self._enabled:
                return None
            seq = self._seq.get(screen_id, 0)
            self._seq[screen_id] = seq + 1
        return Frame(screen_id=screen_id, seq=seq, webp=webp)

    @property
    def _interval(self) -> float | None:
        """The shortest gap between two frames of one screen, or None for no frames.

        None and not zero for a rate of nought, which is the direction the
        arithmetic falls over in: `1 / fps` guarded by "unless fps is nought, in
        which case the interval is nought" reads as *no limit at all*, so a
        server asking for no frames would get one per rendered frame per panel --
        the exact load this module exists to keep off the Pi.
        """
        if self._fps <= 0.0:
            return None
        return 1.0 / self._fps

    def _forget_unwatched(self) -> None:
        """Drop what is remembered about screens nobody is watching. Lock held.

        `_pending` goes with the subscription, and only `_pending`: an image
        nobody asked for must not be sent, and there is nothing about holding
        one that a later subscriber would want back.

        `_last` and `_seq` are pruned by weight rather than by subscription, and
        that is this function keeping the promise `_MAX_TRACKED_SCREENS` makes
        -- "pruned to what is currently enabled when it passes this, and only
        then". `_last` used to be cleared here on every call, which meant that
        the one thing enforcing `MAX_FPS` was reset by any change to the watched
        set, `disable` included. See `_MAX_TRACKED_SCREENS` for the measurement.
        """
        self._pending = {
            screen_id: image
            for screen_id, image in self._pending.items()
            if screen_id in self._enabled
        }
        if len(self._last) > _MAX_TRACKED_SCREENS:
            self._last = {
                screen_id: at for screen_id, at in self._last.items() if screen_id in self._enabled
            }
        if len(self._seq) > _MAX_TRACKED_SCREENS:
            self._seq = {
                screen_id: seq for screen_id, seq in self._seq.items() if screen_id in self._enabled
            }


def _capped(fps: float) -> float:
    """What the daemon will actually do about a rate the server asked for.

    `min` is the whole answer for every number: an infinity clamps to `MAX_FPS`,
    a negative stays negative and `_interval` reads it as no frames at all.

    NaN is the exception, and it is the reason this is a function. It compares
    False against everything, so `min(nan, MAX_FPS)` is `nan`, `nan <= 0.0` is
    False -- so `_interval` does not read it as "no frames" either -- and
    `now - last < nan` is False for every offer for ever. Measured before this
    line: 100 offers accepted out of 100 with the clock frozen, where the cap
    allows one. It cannot arrive off the wire any more (`FramesRequest.fps` is
    bounded and refuses one), and it is answered here as well because the guard
    that stops a browser spending a Pi's CPU should not depend on a peer having
    validated its own message. No frames rather than the cap, because a rate
    that is not a number is not a request for any particular rate.
    """
    if math.isnan(fps):
        return 0.0
    return min(fps, MAX_FPS)


class FramePump(threading.Thread):
    """Where a panel becomes bytes and goes down the socket. Never a worker's thread.

    One thread for the whole rack rather than one per screen: the work is
    serialised by the link's `_send_lock` anyway, so four threads would spend
    their time queueing for it, and the point of this class is that the queueing
    happens somewhere that is not a render loop.
    """

    def __init__(
        self,
        frames: FrameStream,
        send: Callable[[Frame], bool],
        stop: threading.Event,
        poll: float = PUMP_POLL_S,
    ) -> None:
        # Never `_stop`: `threading.Thread._stop` is a real method that `join`
        # calls, and shadowing it makes every join raise `TypeError`.
        super().__init__(name="frames", daemon=True)
        self._frames = frames
        self._send = send
        self._stop_event = stop
        self._poll = poll

    def run(self) -> None:
        """Encode and send until stopped. Nothing gets out of here.

        The same rule as every other thread in this daemon, with a weaker excuse
        for breaking it and the same answer: this one cannot darken a panel, but
        a thread that ended would leave the interface showing a frozen image of a
        rack that is working perfectly, with nothing anywhere saying why. So an
        image that will not encode and a send that raises both cost one frame.
        """
        while not self._stop_event.is_set() and not self._frames.closed:
            try:
                waiting = self._frames.take(self._poll)
            except Exception:
                log.exception("the frame stream failed")
                self._stop_event.wait(self._poll)
                continue
            for screen_id, image in waiting:
                if self._stop_event.is_set():
                    # Between frames, because a shutdown must not wait out a
                    # rack's worth of sends at up to `SEND_DEADLINE_S` each.
                    return
                self._one(screen_id, image)

    def _one(self, screen_id: int, image: Image.Image) -> None:
        try:
            frame = self._frames.encode(screen_id, image)
        except Exception:
            log.exception("could not encode a panel", extra={"screen": screen_id})
            return
        if frame is None:
            return
        try:
            self._send(frame)
        except Exception as exc:
            # `LinkClient.send` answers False rather than raising, so this is
            # about anything that gets past it. Debug, because the interesting
            # version of "the link is down" is logged by the link.
            log.debug("could not send a frame", extra={"screen": screen_id, "error": str(exc)})
