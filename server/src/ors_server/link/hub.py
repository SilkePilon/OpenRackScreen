from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ors_schema.link import MAX_WATCHED_SCREENS, Command, ConfigPush, Frame, FramesRequest

log = logging.getLogger(__name__)

Sender = Callable[[str | bytes], Awaitable[None]]

SEND_TIMEOUT = 5.0
"""How long one send to a daemon may take before the socket is given up on.

An unbounded send is the failure this bounds: a Pi that is TCP-alive but no
longer reading its socket fills the kernel buffers and never drains them, and
`await connection.send(...)` then never returns. Every later send to that
daemon serialises behind it, and `PATCH /api/screens/{id}` awaits a push inside
its request handler -- so the row is committed, the version is bumped, and the
browser spins until it gives up, against a rack that is never told.

Five seconds because the two errors cost very different amounts. Overshooting
holds a request handler open; undershooting drops a daemon that was healthy and
merely slow, and the daemon's link client reconnects within its first backoff
and re-acks, which costs one round trip and -- once task 8 compares versions
before pushing -- no repaint at all. So this is set well inside a human's
patience rather than at the edge of what a busy Pi might need: the link client
reads in a thread of its own and does nothing between reads, so a socket that
has not drained a few KB in five seconds is wedged, not busy.

It also has to stay at or below whatever heartbeat interval the daemon's link
client ends up sending at, or the server would notice a silent daemon before
this ever fires and the bound would be decoration. The client does not exist
yet -- there is no heartbeat cadence in `daemon/src/ors_daemon/` to measure
against today -- so that constraint belongs to whoever writes it.
"""


@dataclass(frozen=True)
class DaemonsOnline:
    """Which racks the hub is holding a socket for, at the moment that changed.

    The whole set and not the daemon that moved, because the interface paints a
    list from it: a message naming only the change would leave every browser
    reconstructing the list from a stream it may have joined halfway through,
    and a tab that connected a second ago would be missing every rack that
    connected before it. The set is at most a few dozen integers.

    Frozen, and a `frozenset` inside it, because one of these is handed to every
    watching browser at once and the hub goes on mutating `_connections`
    afterwards. A live view of that dict would be a message whose contents
    changed between being queued and being sent.
    """

    online: frozenset[int]


Watched = Frame | DaemonsOnline
"""What a browser's queue carries. One queue, because the browser has one socket.

The alternative -- a queue per kind -- makes the reader race two `get()`s and
gains nothing: both of these are things to write to the same socket, in the
order they happened.
"""


@dataclass
class Connection:
    """One daemon socket, as much of it as the hub is allowed to know.

    A `send` callable rather than the WebSocket itself, which is what keeps the
    hub testable without a server and the socket route testable without one.
    """

    daemon_id: int
    send: Sender
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the hub stops routing to this connection, for either reason.

    The hub cannot close a socket -- it holds a `send`, and `register` is called
    from the moment a socket is accepted, where awaiting a close would deadlock
    the accept. So it says so instead, and the handler owns the closing.

    That matters for the connection this one *replaced*: its handler is blocked
    in `receive`, and nothing wakes it until the dead TCP session times out. In
    the meantime it is still a reader that can hand the hub an ack from the
    daemon that just went away, under the same id. A handler racing its receive
    against this event closes the stale socket at the moment it is superseded
    rather than a ping timeout later.
    """


class Hub:
    """Who is connected, what they have acked, and who is watching which screen.

    Deliberately ignorant of the database: it moves messages and tracks
    liveness, and every decision about *what* to send is made by a caller that
    can read rows. That is what keeps the API testable without a socket and the
    socket testable without a database.

    **Event-loop-affine.** Every method has to be called from the thread running
    the event loop. Nothing here takes a lock, and the properties the identity
    guards and the frame eviction rest on are all of the form "there is no await
    between these two lines", which is a statement about this loop and says
    nothing whatever about another thread. FastAPI runs any `def` route in a
    threadpool, so a route that touches the hub must be `async def` -- including
    the read-only-looking ones, since `online_ids` builds a set from a dict a
    reconnect can be resizing, and that raises just as readily as anything else
    here. The one exception is `relay_frame`, which copies before it iterates,
    because it is the only loop whose failure is not confined to one request.
    """

    def __init__(self, send_timeout: float = SEND_TIMEOUT) -> None:
        self._connections: dict[int, Connection] = {}
        self._acked: dict[int, int] = {}
        self._watchers: dict[int, set[asyncio.Queue[Watched]]] = {}
        self._daemon_watchers: set[asyncio.Queue[Watched]] = set()
        self._send_timeout = send_timeout

    def register(self, daemon_id: int, send: Sender) -> Connection:
        # A reconnect arrives before the old socket's close is always observed,
        # so the newest connection wins outright rather than being refused.
        superseded = self._connections.get(daemon_id)
        connection = Connection(daemon_id=daemon_id, send=send)
        self._connections[daemon_id] = connection
        if superseded is not None:
            superseded.closed.set()
        # A daemon that has just connected has confirmed nothing yet, and this
        # is the moment that becomes true again: a Pi that reboots comes back
        # with no config while the hub still remembers it acking version 7, and
        # a caller comparing versions before pushing would see a match, push
        # nothing, and leave the rack blank against a server that believes it is
        # up to date. Dropped here rather than only in `drop`, because a
        # reconnect that overtakes the old handler's exit never reaches `drop`
        # with a connection that is still current.
        self._acked.pop(daemon_id, None)
        # Only when this daemon was not already online. A reconnect replaces a
        # socket without changing the set of racks anyone can see, and
        # announcing it anyway would make a flapping link several identical
        # messages a minute against every open tab. `superseded` is that
        # question already answered, read before the write above.
        if superseded is None:
            self._announce()
        return connection

    def drop(self, connection: Connection) -> None:
        connection.closed.set()
        # Identity, not the id: a handler whose socket was replaced still runs
        # its own cleanup, and it arrives here holding a connection the hub
        # forgot about. Matching on `daemon_id` alone would let that late
        # arrival take the live daemon offline -- the rack would be reported
        # unplugged, and every push would be dropped as unsendable, seconds
        # after it reconnected.
        if self._connections.get(connection.daemon_id) is not connection:
            return
        del self._connections[connection.daemon_id]
        # Nothing is left to answer for this daemon, and the last version it
        # confirmed is not evidence about the one it will be running when it
        # comes back. Also what stops `_acked` outliving the daemon rows.
        self._acked.pop(connection.daemon_id, None)
        # After the delete, and only past the identity guard above: a superseded
        # handler reaching here seconds after its daemon reconnected would
        # otherwise paint the rack as unplugged in every open tab while it is
        # streaming frames -- the same failure the guard exists for, arriving at
        # the interface instead of at a push.
        self._announce()

    def is_online(self, daemon_id: int) -> bool:
        return daemon_id in self._connections

    def online_ids(self) -> set[int]:
        return set(self._connections)

    def record_ack(self, connection: Connection, version: int) -> None:
        """Record what this connection's daemon has confirmed applying.

        The `Connection` rather than a bare id, for the reason `drop` matches on
        identity: a superseded handler is still a reader, and the ack it is
        holding was read from a socket the daemon has already left. Recorded
        under the id, that ack describes the previous boot -- so a caller
        comparing versions before pushing sees a match, pushes nothing, and
        leaves a freshly rebooted Pi blank against a server that believes it is
        up to date. Which is the failure `register` clears the ack to prevent,
        and a late ack would put straight back.
        """
        if self._connections.get(connection.daemon_id) is not connection:
            return
        self._acked[connection.daemon_id] = version

    def acked_version(self, daemon_id: int) -> int | None:
        """The version this daemon has confirmed applying, or None if unknown.

        By id, not by connection: the caller asking is the one deciding whether
        to push to a daemon, and it has a row, not a socket.

        None is "ask again", not "old": it is what an unconnected daemon, a
        daemon that has not answered its first push, and a daemon that has just
        reconnected all report, and the answer to all three is to push.
        """
        return self._acked.get(daemon_id)

    async def push_config(self, daemon_id: int, push: ConfigPush) -> bool:
        """Send a snapshot. True if it reached a socket, False if there was none.

        The return exists for `POST /api/daemons/{id}/push`, whose whole job is
        to report whether a configuration got out of the server -- and which
        cannot ask `is_online` instead, because that is true of a rack that was
        sent nothing and true of one whose send timed out and was dropped.
        """
        return await self._send(daemon_id, push.model_dump_json())

    async def send_command(self, daemon_id: int, command: Command) -> bool:
        """Send a command. True if it reached a socket, False if it did not.

        The return exists for the same reason `push_config`'s does, and for a
        route with even less room to be wrong: a command is not saved anywhere
        and is not retried on reconnect, so one that did not leave the server
        never happens at all. `is_online` cannot answer it -- it is true of a
        rack whose send this class has just timed out and dropped.
        """
        return await self._send(daemon_id, command.model_dump_json())

    async def request_frames(self, daemon_id: int, request: FramesRequest) -> None:
        await self._send(daemon_id, request.model_dump_json())

    def watch_daemons(self, queue: asyncio.Queue[Watched]) -> None:
        """Send this queue the online set whenever it changes, until it unwatches.

        Pushed rather than polled, and the cost decides it. A poll is a timer in
        every open tab forever, and it buys a list that is wrong for up to one
        interval -- which is precisely the window in which a rack that has just
        gone offline still looks like it is about to send a frame. A push is one
        `frozenset` and one `put_nowait` per tab per *connect or disconnect*,
        which is a Pi rebooting, not a rate.

        A queue and not a callback, although a callback is fewer lines: the
        callers are `register` and `drop`, and `register` runs inside the daemon
        socket's hello. A callback that raised, or that awaited anything, would
        put a browser's problem on the path a rack uses to come online.
        """
        self._daemon_watchers.add(queue)

    def unwatch_daemons(self, queue: asyncio.Queue[Watched]) -> None:
        self._daemon_watchers.discard(queue)

    def subscribe_frames(self, screen_id: int, queue: asyncio.Queue[Watched]) -> bool:
        """Returns True when this is the first watcher, which starts the daemon."""
        watchers = self._watchers.setdefault(screen_id, set())
        first = not watchers
        watchers.add(queue)
        return first

    def unsubscribe_frames(self, screen_id: int, queue: asyncio.Queue[Watched]) -> bool:
        """Returns True when the last watcher left, which stops the daemon."""
        watchers = self._watchers.get(screen_id, set())
        watchers.discard(queue)
        if watchers:
            return False
        # A screen nobody watches is not a screen with an empty set of watchers:
        # left behind, one entry per screen ever opened accumulates for the life
        # of the server, and `watched_screens` -- which is how a reconnecting
        # daemon is told what to resume sending -- would name every one of them.
        self._watchers.pop(screen_id, None)
        return True

    def watched_screens(self) -> set[int]:
        return set(self._watchers)

    def frames_for(self, daemon_id: int, owned: set[int]) -> FramesRequest:
        """The whole `frames` request for one rack: every screen it owns that
        anybody is watching, bounded to what one request may name.

        **One mechanism, because there is one rule.** Both callers assemble this
        list -- the browser socket on every subscribe and unsubscribe, the daemon
        socket on every reconnect -- and the two had it separately: one truncated
        and logged, the other did not. On a rack with more watched screens than
        the bound, that asymmetry was a hello that raised `ValidationError` after
        `hub.register`, so the rack reconnected in a loop while every open tab
        showed it online. A bound implemented twice is a bound implemented once
        and forgotten once.

        `owned` is passed in rather than read here, which is the hub's standing
        rule: ownership is a row and this class reads none. What the hub does
        know, and the caller cannot, is the *order* the screens were subscribed
        in -- `_watchers` is a dict, so it is insertion order -- and that is what
        decides which survive.

        The most recently opened win. Sorting and keeping the lowest ids keeps
        the oldest-created panels, so the screen a tab has just opened is the one
        dropped: a panel frozen from the moment it appears, with no `daemons`
        change and no stall event to say so, on a rack the interface still shows
        as online. Newest-first is also what a person means by "the panels I am
        looking at".

        Sorted for the wire, so that two requests naming the same screens look
        the same, and because a daemon's log is easier to read in order. The
        order the ids are *kept* in and the order they are *sent* in are two
        different decisions and this is the second one.
        """
        watched = [screen_id for screen_id in self._watchers if screen_id in owned]
        if len(watched) > MAX_WATCHED_SCREENS:
            dropped = watched[:-MAX_WATCHED_SCREENS]
            watched = watched[-MAX_WATCHED_SCREENS:]
            # The ids and not just a count. Without them nobody reading this can
            # tell which panel went quiet, and a count plus a limit describes
            # every rack over the bound identically.
            log.error(
                "more screens of one rack are watched than a single request may name",
                extra={
                    "daemon": daemon_id,
                    "watched": len(dropped) + len(watched),
                    "allowed": MAX_WATCHED_SCREENS,
                    "dropped": sorted(dropped),
                },
            )
        return FramesRequest(enabled=bool(watched), screen_ids=sorted(watched))

    async def relay_frame(self, frame: Frame) -> None:
        """Fan one frame out to whoever is watching that screen.

        A coroutine although it awaits nothing: this is the call the daemon
        socket makes for every frame it reads, and a fan-out that has to await
        anything at all -- a per-watcher send, a backpressure signal -- would
        otherwise change every call site. Awaiting nothing is also what makes
        the body below safe, and the two facts are the same fact.
        """
        # A copy, because the set really can change under this loop: `Hub` is
        # event-loop-affine and a `def` FastAPI route runs in a threadpool, so
        # one browser subscribing from the wrong kind of route raises
        # `RuntimeError: Set changed size during iteration` here. That is
        # neither a `WebSocketDisconnect` nor a `ValidationError`, so it escapes
        # the daemon socket handler and takes the whole rack offline -- a cost
        # nothing like the copy, which is at most four queues at 2 fps. A
        # watcher that arrives mid-frame catches the next one.
        for queue in list(self._watchers.get(frame.screen_id, ())):
            # The newest frame wins, which the design says in as many words: a
            # stale panel image is worse than a skipped one. Discarding *this*
            # one instead would serve a browser that stalled for a second and
            # then recovered its backlog, frame by stale frame, while every
            # fresh one was thrown away.
            if _offer(queue, frame):
                log.debug("dropped a frame for a slow watcher", extra={"screen": frame.screen_id})

    def _announce(self) -> None:
        """Tell every watching browser who is online now. Called on a change only.

        A copy of the watcher set for the reason `relay_frame` copies: this runs
        from inside the daemon socket's hello and from its `finally`, and a
        browser socket registering from anywhere that is not this loop would
        otherwise raise `RuntimeError: Set changed size during iteration` on a
        path that takes a whole rack offline.
        """
        if not self._daemon_watchers:
            return
        online = DaemonsOnline(online=frozenset(self._connections))
        for queue in list(self._daemon_watchers):
            if _offer(queue, online):
                log.debug("dropped a stale online list for a slow watcher")

    async def _send(self, daemon_id: int, payload: str) -> bool:
        """True if the payload was handed to a live socket, False if it was not.

        False rather than an exception for every one of the three ways that
        happens -- no socket, a send that timed out, a socket that failed --
        because none of them is an error here: the edit is already committed and
        the snapshot is pushed again on the next connect. What the boolean is for
        is a caller that has to *say* whether anything left.
        """
        connection = self._connections.get(daemon_id)
        if connection is None:
            # Offline is a normal state, not an error: the edit is already
            # saved, and the snapshot is pushed again when it reconnects.
            return False
        try:
            await asyncio.wait_for(connection.send(payload), self._send_timeout)
        except TimeoutError:
            # Indistinguishable from a dead socket as far as the hub is
            # concerned, and treated as one: a daemon that cannot take a few KB
            # inside `SEND_TIMEOUT` is not going to take the next message
            # either. `wait_for` has already cancelled the send, which leaves
            # the socket mid-frame -- which is why this drops rather than
            # retries, and why the handler watching `closed` is what closes it.
            log.warning(
                "daemon send timed out; dropping",
                extra={"daemon": daemon_id, "timeout_s": self._send_timeout},
            )
            self.drop(connection)
            return False
        except Exception as exc:
            log.info("daemon send failed; dropping", extra={"daemon": daemon_id, "error": str(exc)})
            self.drop(connection)
            return False
        return True


def _offer(queue: asyncio.Queue[Watched], item: Watched) -> bool:
    """Hand a browser something to send, without ever waiting for it. True if
    something older had to be thrown away to make room.

    The one place either fan-out above puts anything, because the property that
    makes it safe is a property of these three lines and not of either caller:
    there is no await between the `get_nowait` and the `put_nowait`, so nothing
    else on this loop -- a watcher parked in `queue.get()`, above all -- runs in
    between, and the space made is the space used.

    Never blocking is the whole point. The callers are a daemon's frame relay
    and a daemon's connect, and a browser that has stopped reading its socket
    must not be able to hold either of them up.
    """
    try:
        queue.put_nowait(item)
        return False
    except asyncio.QueueFull:
        queue.get_nowait()
        queue.put_nowait(item)
        return True
