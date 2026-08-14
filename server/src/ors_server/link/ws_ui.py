"""The browser's socket: who is online, and the panels somebody is looking at.

Two things travel down here and they arrive by different roads. The online list
is *pushed* from the hub whenever a rack connects or disconnects; frames arrive
only for screens this connection has asked for, and asking is what makes the
rack encode them at all.

The rule that shape rests on is the one thing in this module worth reading
first. `FramesRequest` is **whole-daemon state**, not a per-screen toggle: the
daemon's `FrameStream.enable` does `self._enabled = set(screen_ids)`, a replace,
and `disable()` clears the lot. So a request naming only the screen that just
changed is a request to stop every other panel on that rack. On every subscribe
and every unsubscribe, this module therefore recomputes that daemon's *whole*
watched set -- across every open tab, not this one -- and sends it entire, with
`enabled=False` only when the set is empty.

Because that recomputation is a whole-rack write, it is also rate-bounded rather
than done once per message: see `ARM_INTERVAL_S`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from ors_schema.errors import first_error
from ors_schema.link import Frame
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.datastructures import State

from ors_server.auth import require_session
from ors_server.db import Database
from ors_server.link.hub import DaemonsOnline, Watched

log = logging.getLogger(__name__)

# A router of its own, at the root, carrying the session dependency itself.
# `app.state.api` is prefixed `/api`, which would put this at `/api/ws/ui` --
# not what the spec says and not what the SPA dials -- and the root routers the
# app already has are the open ones. So the guard is spelled here, and
# `test_auth.py`'s sweep is what stops it being forgotten: it walks every route
# the app will match and fails on any socket that opens without a session.
#
# Unlike `/ws/daemon`, there is no credential in a first message to fall back
# on. Whatever is watching this socket is watching every panel of every rack,
# live, so it is refused at the handshake.
router = APIRouter(dependencies=[Depends(require_session)])

WATCHER_QUEUE = 8
"""How many messages may be waiting for one browser before the oldest is dropped.

The hub never blocks on a full queue -- it evicts the oldest and enqueues the
newest, because a stale panel image is worse than a skipped one -- so this is
not a bound on correctness but on how much a browser that has stopped reading
may cost, and on how stale what it eventually reads may be.

Eight because the thing being buffered is a four-panel rack canvas at the 2 fps
the daemon is asked for: two frames per panel, which is one second of slack for
a tab that was backgrounded or a link that hiccupped, and about 40 KB of held
WebP at what a real panel weighs. Larger would buy a browser the right to be a
second behind and then be shown a second of history at full speed; smaller
starts evicting a four-panel refresh that arrived in one tick.
"""


ARM_INTERVAL_S = 0.25
"""How often one browser connection may make the server write to one rack.

The third member of the family the `fps` cap and the `screen_ids` length closed
two of, and the same sentence covers all three: one message decides what a
subscription costs the Pi. A thousand subscribe/unsubscribe messages from one
connection produced a thousand `frames` requests in 0.19s -- each one taking the
daemon's `FrameStream._condition` and *replacing* its whole streaming state, and
each one costing the server two synchronous SQLite reads on the event loop.

Coalesced rather than refused, because the interface legitimately churns
subscriptions: a rack canvas opens four panels at once and a route change closes
them again, and a browser told "too fast" has nothing useful to do about it.
What a rack is owed is the *last* of a burst of these, never all of them, so
holding the recomputation and doing it once is not a compromise -- it is the
same answer arriving with less noise.

Leading edge, then one coalesced send at the end of the window: the first
request for a rack goes out immediately, because opening a panel must not wait a
quarter of a second to start, and everything asked for within the window after
it is sent once when the window closes. A tab can therefore cost a rack at most
four requests a second however fast it talks.

A quarter of a second because the cost of being late is half a frame: the daemon
is asked for 2 fps, so a panel whose subscription was coalesced starts at most
one frame interval behind. Longer starts to look like the panel is broken;
shorter stops coalescing the thing worth coalescing, which is a canvas of four
panels subscribing in one turn of the event loop.

Per connection *and* per rack. One tab opening panels on two racks arms both
immediately; the bound is on what one browser can do to one Pi.
"""

MAX_ROW_ID = 2**63 - 1
"""The largest integer SQLite will take as a bind parameter.

A bound on `screen_id` and not decoration. `int` is unbounded in Python and
bounded in SQLite, and the two meet inside `sqlite3.execute`, which answers a
larger one with `OverflowError: Python int too large to convert to SQLite
INTEGER`. Measured. That is not a `ValidationError`, so it is not skipped the
way an unreadable message is -- it leaves `_read`, leaves the handler, and takes
the socket down with a traceback, on a message a browser chose. Refused as a
malformed request instead, which is what it is: a screen id is a row id, and no
row has ever had this one.
"""


class _Request(BaseModel):
    """What a browser is allowed to say. Everything else is skipped.

    `extra="forbid"` for the reason the link's envelope has it: without it a
    misspelled field is silently ignored and the tab believes it subscribed.
    The audience here is a build of the SPA that may be older than this server,
    and "your message named a field I do not know" is the only answer that gets
    anybody to the cause.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["subscribe", "unsubscribe"]
    screen_id: int = Field(ge=1, le=MAX_ROW_ID)

    @field_validator("screen_id", mode="before")
    @classmethod
    def _not_a_flag(cls, screen_id: object) -> object:
        # `bool` is an `int` in Python and pydantic's lax mode takes it as one,
        # so `{"screen_id": true}` validates as 1 and subscribes to whatever
        # screen has row id 1: a build of the SPA that sends a flag where an id
        # belongs opens panel A and is shown panel B's image, with nothing
        # anywhere saying so. `ws_daemon._about` refuses the same thing on the
        # other socket. Before the range check, because `True` is `1` and would
        # pass it.
        if isinstance(screen_id, bool):
            raise ValueError("a screen id is a row id, not a flag")
        return screen_id


@dataclass
class _Watcher:
    """One browser socket's share of the hub.

    The queue is the identity the hub knows this connection by -- it is what
    `subscribe_frames` registers and what `unsubscribe_frames` removes -- so
    there is exactly one per connection and it is never rebuilt. `screens` is
    this connection's own subscriptions, which is *not* what gets sent to a
    daemon: see `_arm`.

    `screens` maps each of them to the rack that owned it when it was
    subscribed to, which is the difference between an unsubscribe costing a row
    lookup and costing nothing -- and, on the way out, between the exit path
    reading one row per panel this tab had open and reading none. It is also
    what says whether a message changed anything at all: a repeat from this same
    connection is a no-op at the hub, so a request restating it is a write to a
    rack for nothing.
    """

    queue: asyncio.Queue[Watched]
    screens: dict[int, int] = field(default_factory=dict)
    armed_at: dict[int, float] = field(default_factory=dict)
    """`time.monotonic()` when each rack was last told anything by this tab."""
    owed: set[int] = field(default_factory=set)
    """Racks whose set has moved since, waiting for the window to close."""
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def owe(self, daemon_id: int) -> None:
        self.owed.add(daemon_id)
        self.wake.set()

    def due_in(self, daemon_id: int) -> float:
        """How long until this tab may write to this rack again. Zero is now."""
        armed_at = self.armed_at.get(daemon_id)
        if armed_at is None:
            return 0.0
        return max(0.0, armed_at + ARM_INTERVAL_S - time.monotonic())


@router.websocket("/ws/ui")
async def ui_socket(socket: WebSocket) -> None:
    """One browser's live view of the racks, from open tab to closed one."""
    await socket.accept()
    state = socket.app.state
    watcher = _Watcher(queue=asyncio.Queue(maxsize=WATCHER_QUEUE))
    tasks: list[asyncio.Task] = []
    try:
        # Registered before the list is read and with no await in between, so
        # the two cannot cross: registering afterwards would lose a rack that
        # connected in the gap, and reading afterwards would send a snapshot
        # older than a change already queued behind it. The writer is started
        # only once this first message is on the wire, for the same reason.
        state.hub.watch_daemons(watcher.queue)
        online = _daemons_message(state.hub.online_ids())
        await socket.send_text(online)

        # Reading and writing are concurrent because neither can wait for the
        # other: frames arrive whenever the rack renders one, and a browser is
        # under no obligation to say anything ever again after it subscribes.
        tasks = [
            asyncio.create_task(_read(state, socket, watcher)),
            asyncio.create_task(_write(socket, watcher.queue)),
            asyncio.create_task(_flush(state, watcher)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            # Retrieved rather than discarded: this is where a closed tab
            # arrives, and where anything that is not a closed tab stops being
            # invisible. `_cancel` below would swallow both.
            task.result()
    except WebSocketDisconnect:
        # The ordinary ending: the tab was closed, or the laptop was shut.
        log.debug("a browser socket closed")
    finally:
        # The hub first, and before anything at all is awaited. A `finally`
        # entered by cancellation raises again at every await inside it that a
        # further cancel lands on -- a shutdown with a grace period is exactly
        # two of those -- so an await above this line is a dead queue left
        # subscribed for the life of the process. Even the `gather` below is
        # such an await, which is why the release is not merely first among the
        # awaits but before all of them.
        #
        # Safe despite the reader still being alive: there is no await between
        # `_let_go` and the `cancel` below, so the reader cannot run in between
        # and put a subscription back after this took them away.
        racks = _let_go(state, watcher)
        for task in tasks:
            task.cancel()
        if tasks:
            # `asyncio.wait` cancels neither of the awaitables it was handed, so
            # without this a handler that ends for any reason leaves the other
            # half of itself parked on the loop -- one per tab ever opened.
            await asyncio.gather(*tasks, return_exceptions=True)
        await _tell_the_racks(state, watcher, racks)


async def _read(state: State, socket: WebSocket, watcher: _Watcher) -> None:
    """Act on what the browser asks for, until it stops asking."""
    while True:
        raw = await socket.receive_text()
        try:
            request = _Request.model_validate_json(raw)
        except ValidationError as error:
            # Skipped rather than closed, for the reason the daemon socket skips
            # one: closing turns a single bad message into a reconnect loop, and
            # `extra="forbid"` plus a `Literal` action mean what is dropped here
            # is exactly what was unusable and is named in the log.
            log.warning(
                "a browser said something this socket does not understand; skipped",
                extra={"error": first_error(error)},
            )
            continue
        if request.action == "subscribe":
            await _subscribe(state, watcher, request.screen_id)
        else:
            await _unsubscribe(state, watcher, request.screen_id)


async def _write(socket: WebSocket, queue: asyncio.Queue[Watched]) -> None:
    """Send whatever the hub has put in this connection's queue.

    A task of its own, and the reason is the whole backpressure story. This is
    the only place in the frame path that can block: the hub's put never waits,
    and this send can, against a browser that has stopped reading its socket.
    Because it is here and not on the daemon's thread of control, what a stalled
    tab costs is its own queue depth and nothing else -- the daemon socket goes
    on relaying, and the hub goes on evicting this connection's oldest frame.
    """
    while True:
        await socket.send_text(_encode(await queue.get()))


async def _subscribe(state: State, watcher: _Watcher, screen_id: int) -> None:
    if screen_id in watcher.screens:
        # This connection already holds it. `subscribe_frames` would add a queue
        # already in the set, so nothing anywhere moves -- and a request
        # restating a set nobody changed is a write to a rack for nothing.
        # Different from a *second tab* on the same screen, which really did
        # acquire a subscription and is still told about it in full.
        return
    daemon_id = _daemon_of(state.database, screen_id)
    if daemon_id is None:
        # A screen deleted from another tab, or an id from a stale page. Not an
        # error and not worth closing a socket over -- but it must not become a
        # subscription, because nothing would ever remove it: the hub would hold
        # a queue against an id no daemon owns and no `_arm` could name.
        log.info(
            "a browser asked to watch a screen that does not exist",
            extra={"screen": screen_id},
        )
        return
    state.hub.subscribe_frames(screen_id, watcher.queue)
    watcher.screens[screen_id] = daemon_id
    await _changed(state, watcher, daemon_id)


async def _unsubscribe(state: State, watcher: _Watcher, screen_id: int) -> None:
    daemon_id = watcher.screens.pop(screen_id, None)
    if daemon_id is None:
        # Not this connection's to give up: either it never subscribed, or the
        # tab has already dropped it. `unsubscribe_frames` would discard a queue
        # that is not in the set, so there is no change to tell a rack about --
        # and no row to read to find out which rack that would be.
        return
    state.hub.unsubscribe_frames(screen_id, watcher.queue)
    await _changed(state, watcher, daemon_id)


async def _changed(state: State, watcher: _Watcher, daemon_id: int) -> None:
    """This tab has moved that rack's set. Say so now, or owe it. See `ARM_INTERVAL_S`.

    Nothing is ever dropped: a rack that is not written to here is added to
    `owed`, and `_flush` sends the recomputed set -- which by then includes this
    change and everything after it -- when the window closes.
    """
    if daemon_id in watcher.owed or watcher.due_in(daemon_id) > 0.0:
        watcher.owe(daemon_id)
        return
    await _arm(state, watcher, daemon_id)


async def _arm(state: State, watcher: _Watcher, daemon_id: int) -> None:
    """Tell one rack the complete set of its screens anybody is watching.

    Sent whole and recomputed from scratch every time, which is the correction
    at the top of this module. `Hub.frames_for` assembles it from the global
    watched set -- every screen every tab is watching -- and the screens this
    rack owns, which are read here because ownership is a row and the hub reads
    none. The daemon socket's `_resume_frames` calls the same method for the
    same reason: the intersection and the bound on how many screens one request
    may name are one rule, and they were two implementations of it.

    Sent unconditionally rather than only when the resulting *set* differs from
    what this rack was last told. A second tab subscribing to a screen the first
    already watches produces an identical message, which is idempotent at the
    daemon and costs a few dozen bytes -- and the alternative, remembering what
    each rack was last told, is state that goes wrong exactly when a daemon
    reconnects and forgets. What is skipped instead is one step earlier and
    needs no such memory: a message that moved nothing on *this* connection.

    The two lines of bookkeeping come before the await and not after it. They
    are what stops a message arriving during this send from starting a second
    one: `owed.discard` first, so a change landing mid-send is owed again rather
    than believed to be covered by this request, and `armed_at` before rather
    than after, so the window is measured from when the rack was told, not from
    when the send finished against a Pi that had stopped reading.
    """
    watcher.owed.discard(daemon_id)
    watcher.armed_at[daemon_id] = time.monotonic()
    request = state.hub.frames_for(daemon_id, _owned(state.database, daemon_id))
    await state.hub.request_frames(daemon_id, request)


async def _flush(state: State, watcher: _Watcher) -> None:
    """Send what this tab owes its racks, once per rack per window.

    A task of its own for the reason `_write` is one: the reader is parked in
    `receive_text` and a browser is under no obligation to ever say anything
    again, so a tab that subscribes to four panels and then goes quiet must
    still have the last three sent. Nothing else would wake anything.

    The inner loop re-reads `owed` rather than working from the list it slept
    on, because the reader goes on adding to it while this sleeps -- which is
    the whole point -- and `_arm` takes entries out.
    """
    while True:
        await watcher.wake.wait()
        watcher.wake.clear()
        while watcher.owed:
            await asyncio.sleep(min(watcher.due_in(daemon_id) for daemon_id in watcher.owed))
            for daemon_id in sorted(watcher.owed):
                if watcher.due_in(daemon_id) == 0.0:
                    await _arm(state, watcher, daemon_id)


def _let_go(state: State, watcher: _Watcher) -> set[int]:
    """Take this connection out of the hub. Returns the racks it had open.

    The `finally` the hub cannot do for itself: it holds a queue, not a socket,
    so a closed tab is invisible to it and the rack goes on encoding WebP --
    burning a Pi's CPU on a browser that no longer exists -- until something
    says otherwise. Nothing else ever would.

    Deliberately synchronous, and the whole of what the handler must do before
    it awaits anything on the way out. Cancellation is why: a `finally` entered
    by a cancel raises again at any await a further cancel lands on, and this
    running late is not a late release, it is no release.

    Both halves matter and only one of them is visible from outside. The
    subscriptions keep a rack encoding; the daemon watch is a queue the hub
    holds forever, one per tab ever opened, and nothing downstream would ever
    notice because the socket that would have read it is gone.

    The racks it returns include the ones this connection still *owes* a
    request, which are not always racks it still holds a screen of: a tab that
    opens a panel and closes it again inside the coalescing window has an empty
    set and a rack that has not yet been told about it. Read from the watcher
    rather than from the database, which is what makes this able to be
    synchronous at all -- and answers a case the row lookup could not, since a
    screen deleted while the tab had it open used to leave its rack encoding.
    """
    state.hub.unwatch_daemons(watcher.queue)
    daemons = set(watcher.screens.values()) | watcher.owed
    for screen_id in watcher.screens:
        state.hub.unsubscribe_frames(screen_id, watcher.queue)
    watcher.screens.clear()
    watcher.owed.clear()
    return daemons


async def _tell_the_racks(state: State, watcher: _Watcher, daemons: set[int]) -> None:
    """Re-state what is left to watch, to every rack this connection had open.

    Not held back by `ARM_INTERVAL_S`, and it is the one caller that is not: the
    window exists to coalesce what a *live* tab asks for, and there is nothing
    further coming from this one. A rack left waiting out the window for a socket
    that has gone is a rack encoding WebP for nobody.

    Best-effort by comparison with `_let_go`, and it is the ordering rather than
    the sends that carries the guarantee: a rack that misses one of these is
    re-armed by the daemon socket's `_resume_frames` on its next connect, and a
    rack that never reconnects is not encoding anything to miss.
    """
    for daemon_id in sorted(daemons):
        await _arm(state, watcher, daemon_id)


def _encode(item: Watched) -> str:
    if isinstance(item, DaemonsOnline):
        return _daemons_message(item.online)
    return _frame_message(item)


def _frame_message(frame: Frame) -> str:
    """One panel, as the SPA will read it.

    **The base64 contract, and it is deliberately not pydantic's.** `Frame`
    carries `ser_json_bytes="base64"`, and pydantic emits the *URL-safe*
    alphabet -- `-` and `_` where the standard one has `+` and `/`. The only
    decoder a browser has without shipping one is `atob`, and `atob` throws
    `InvalidCharacterError` on both of those characters. So `model_dump_json()`
    onto this socket is a payload the interface cannot read, and the failure is
    silent at the end that can fix it: `base64.b64decode` in Python accepts
    either alphabet unless asked not to, so a test written the obvious way
    passes while the page shows nothing.

    The wire form here is therefore assembled rather than dumped, with the
    standard alphabet and its padding. It is the *browser* protocol; the daemon
    link keeps pydantic's serialisation, because both ends of that one are
    pydantic and neither has ever wanted `atob`.

    M3b: decode with `atob(message.webp)` and no substitution. If this ever
    changes back, the SPA needs `.replace(/-/g, '+').replace(/_/g, '/')` before
    the `atob` and this docstring is wrong.
    """
    return json.dumps(
        {
            "type": "frame",
            "screen_id": frame.screen_id,
            "seq": frame.seq,
            "webp": base64.b64encode(frame.webp).decode("ascii"),
        }
    )


def _daemons_message(online: frozenset[int] | set[int]) -> str:
    """Which racks the server is holding a socket for, right now.

    Sorted so that two consecutive messages saying the same thing look the same,
    which is what lets the interface skip a repaint without comparing sets.

    This is also, as of M3a, the only thing that tells a browser a panel has
    stopped for a reason. A frame that a rack encoded too large is refused by
    the schema and skipped by `/ws/daemon`, and the watcher's queue simply stops
    -- so a stale image on the page is indistinguishable from a still one. That
    is left to M3b rather than answered with a per-frame event here, on purpose:
    an oversized frame is the rarest of the ways a panel goes quiet, well behind
    a rebooted Pi, a pulled cable and a wifi blip, and an event that covered only
    it would teach the interface that silence otherwise means healthy. The
    honest signal is time since the last frame, which the panel component has
    without being told, combined with this message, which covers every case
    where the rack itself has gone.
    """
    return json.dumps({"type": "daemons", "online": sorted(online)})


def _daemon_of(database: Database, screen_id: int) -> int | None:
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT daemon_id FROM screen WHERE id = ?", (screen_id,)
        ).fetchone()
    return None if row is None else int(row["daemon_id"])


def _owned(database: Database, daemon_id: int) -> set[int]:
    # `closing` rather than `with database.connect()`: sqlite3's own context
    # manager commits and leaves the connection open, so the obvious spelling
    # would leak one per subscribe on this socket.
    with closing(database.connect()) as connection:
        return {
            int(row["id"])
            for row in connection.execute("SELECT id FROM screen WHERE daemon_id = ?", (daemon_id,))
        }
