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
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import closing
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from ors_schema.errors import first_error
from ors_schema.link import MAX_WATCHED_SCREENS, Frame, FramesRequest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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


@dataclass
class _Watcher:
    """One browser socket's share of the hub.

    The queue is the identity the hub knows this connection by -- it is what
    `subscribe_frames` registers and what `unsubscribe_frames` removes -- so
    there is exactly one per connection and it is never rebuilt. `screens` is
    this connection's own subscriptions, which is *not* what gets sent to a
    daemon: see `_arm`.
    """

    queue: asyncio.Queue[Watched]
    screens: set[int] = field(default_factory=set)


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
        released = _let_go(state, watcher)
        for task in tasks:
            task.cancel()
        if tasks:
            # `asyncio.wait` cancels neither of the awaitables it was handed, so
            # without this a handler that ends for any reason leaves the other
            # half of itself parked on the loop -- one per tab ever opened.
            await asyncio.gather(*tasks, return_exceptions=True)
        await _tell_the_racks(state, released)


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
    watcher.screens.add(screen_id)
    await _arm(state, daemon_id)


async def _unsubscribe(state: State, watcher: _Watcher, screen_id: int) -> None:
    state.hub.unsubscribe_frames(screen_id, watcher.queue)
    watcher.screens.discard(screen_id)
    daemon_id = _daemon_of(state.database, screen_id)
    if daemon_id is None:
        # The row went while the tab had it open. The hub entry is gone, which
        # is the half that matters here; the daemon retires a screen its own
        # configuration no longer names.
        return
    await _arm(state, daemon_id)


async def _arm(state: State, daemon_id: int) -> None:
    """Tell one rack the complete set of its screens anybody is watching.

    Sent whole and recomputed from scratch every time, which is the correction
    at the top of this module. The set is `watched_screens()` -- global, every
    screen every tab is watching -- intersected with the screens this daemon
    owns, because asking a Pi to render ids belonging to another rack is at best
    noise in its log and at worst four workers it cannot start.

    The intersection is done here rather than in the hub because the hub reads
    no rows by design, and ownership is a row. Giving it a partitioned
    `watched_screens` would mean injecting a lookup that reads `screen` from
    inside a class whose whole value is that it does not -- and the daemon
    socket already computes exactly this intersection at the caller, in
    `_resume_frames`, so a partition would be a second mechanism for one rule.

    Sent unconditionally rather than only when the set changed. A second tab
    subscribing to a screen the first already watches produces an identical
    message, which is idempotent at the daemon and costs a few dozen bytes --
    and the alternative, tracking what each rack was last told, is state that
    goes wrong exactly when a daemon reconnects and forgets.
    """
    watched = sorted(state.hub.watched_screens() & _owned(state.database, daemon_id))
    if len(watched) > MAX_WATCHED_SCREENS:
        # Truncated, not raised. `FramesRequest` refuses a longer list, so
        # building one unchecked would raise inside a subscribe and, through the
        # handler's `finally`, tear down a tab -- against a rack whose only
        # crime is having a lot of panels. Streaming the first sixty-four is the
        # lesser failure and this line is how anyone finds out it happened.
        log.error(
            "more screens of one rack are watched than a single request may name",
            extra={
                "daemon": daemon_id,
                "watched": len(watched),
                "allowed": MAX_WATCHED_SCREENS,
            },
        )
        watched = watched[:MAX_WATCHED_SCREENS]
    await state.hub.request_frames(
        daemon_id, FramesRequest(enabled=bool(watched), screen_ids=watched)
    )


def _let_go(state: State, watcher: _Watcher) -> set[int]:
    """Take this connection out of the hub. Returns the screens it was watching.

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
    """
    state.hub.unwatch_daemons(watcher.queue)
    screens = set(watcher.screens)
    for screen_id in screens:
        state.hub.unsubscribe_frames(screen_id, watcher.queue)
    watcher.screens.clear()
    return screens


async def _tell_the_racks(state: State, screens: set[int]) -> None:
    """Re-state what is left to watch, to every rack this connection had open.

    Best-effort by comparison with `_let_go`, and it is the ordering rather than
    the sends that carries the guarantee: a rack that misses one of these is
    re-armed by the daemon socket's `_resume_frames` on its next connect, and a
    rack that never reconnects is not encoding anything to miss.
    """
    for daemon_id in sorted(_daemons_of(state.database, screens)):
        await _arm(state, daemon_id)


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


def _daemons_of(database: Database, screen_ids: set[int]) -> set[int]:
    """Which racks own this connection's screens, on one connection.

    One statement per screen rather than a single `IN` list, which would be a
    list of bind parameters sized by how many panels a tab had open -- and
    SQLite has a compile-time ceiling on those that this code has no way to
    read. The ids come from `_watcher.screens`, so every one of them existed
    when it was subscribed to; a row deleted since simply answers nothing.
    """
    if not screen_ids:
        return set()
    with closing(database.connect()) as connection:
        rows = [
            connection.execute("SELECT daemon_id FROM screen WHERE id = ?", (screen_id,)).fetchone()
            for screen_id in sorted(screen_ids)
        ]
    return {int(row["daemon_id"]) for row in rows if row is not None}


def _owned(database: Database, daemon_id: int) -> set[int]:
    # `closing` rather than `with database.connect()`: sqlite3's own context
    # manager commits and leaves the connection open, so the obvious spelling
    # would leak one per subscribe on this socket.
    with closing(database.connect()) as connection:
        return {
            int(row["id"])
            for row in connection.execute("SELECT id FROM screen WHERE daemon_id = ?", (daemon_id,))
        }
