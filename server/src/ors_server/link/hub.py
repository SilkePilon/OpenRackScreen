from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ors_schema.link import (
    MAX_PROBE_HOLD_S,
    MAX_WATCHED_SCREENS,
    Command,
    ConfigPush,
    DetectRequest,
    DetectResult,
    Frame,
    FramesRequest,
    ProbeRequest,
    ProbeResult,
)

log = logging.getLogger(__name__)

Sender = Callable[[str | bytes], Awaitable[None]]

Request = DetectRequest | ProbeRequest
"""What may be asked of a rack and waited on, as opposed to merely sent.

The two of them are exactly the messages that carry a `request_id`, which is
what this file needs of them: everything else on `ServerMessage` -- a config
push, a command, a frames request -- is fire-and-forget by design, and has no
id to key a wait on. Named rather than left as a union at the call site so that
whoever adds the third one is told where it has to be added.
"""

Reply = DetectResult | ProbeResult
"""The answering half. Same reasoning, from the other end of the wire."""

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


REQUEST_TIMEOUT = MAX_PROBE_HOLD_S + 2 * SEND_TIMEOUT
"""How long a caller waits for one rack's answer before giving up on it.

`SEND_TIMEOUT`'s argument, one message further on. A request handler is parked
on this call -- `POST /api/daemons/{id}/detect` and `/probe` are the callers --
so a rack that has stopped answering must not be able to hold it open for as
long as it likes. Unbounded, one Pi that accepted a socket and then wedged its
worker thread costs a request handler apiece for every operator who presses the
button, and the interface spins against a rack that will never speak.

Forty seconds, and unlike `SEND_TIMEOUT` the number is not free to be chosen on
its own: `ProbeRequest.hold_s` is bounded by `MAX_PROBE_HOLD_S`, which is thirty,
so a probe this server will accept and send can legally keep a panel lit for
thirty seconds before the daemon has a single thing to say about it. Anything at
or below that is a wait that expires on probes that are working perfectly --
honestly reported and wrong every time, because a timeout is indistinguishable
from a rack that has gone, and the wizard's advice would be to check the wiring
that had just proved itself. The remaining ten seconds are the round trip either
side of the hold, which is `SEND_TIMEOUT` twice over: the request has to reach a
Pi and the answer has to come back, and this file has already decided how long
one leg of that may take.

Written as that sum and not as `40.0`, because neither half of it is this file's
to choose alone: a schema that raised the hold, against a literal left here,
would be a probe that always times out -- and nothing would say so until an
operator was standing in front of a rack that had just worked.

It is not the sum of every bound on the path. The send is bounded separately and
happens before the wait starts, so a request whose socket wedges costs
`SEND_TIMEOUT` and then returns -- it never reaches this at all. What this covers
is the interval in which the daemon has the question and the server has nothing
to do but wait. The two legs do stack on the path that succeeds and then goes
quiet, though, so a route's worst case is `SEND_TIMEOUT + REQUEST_TIMEOUT` --
**forty-five seconds** -- and that, not forty, is the number to hold a browser's
patience against.

Overshooting and undershooting cost differently here, as they do for the send.
Overshooting holds a handler open on a rack that is already known to be quiet;
undershooting fails a probe that worked, which is the failure this feature exists
to make impossible to have. So it is set past the worst legal case rather than at
a typical one -- a wizard's probe holds for a second or two, and never meets this.
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


@dataclass(frozen=True)
class _Wait:
    """One caller parked on one answer, and the rack the answer has to come from.

    The daemon id is here and not derivable from anywhere else: the table is
    keyed by `request_id`, because that is what the reply carries, and `drop`
    has to be able to find every wait belonging to one connection without
    reading any of the messages back.
    """

    daemon_id: int
    future: asyncio.Future[Reply | None]
    """Resolved with the reply, or with None by `drop` when the rack goes away.

    A result and not an exception for either outcome, for the reason `_send`
    returns a bool: neither is an error. `request` answers None to a caller that
    has to *say* whether an answer came back, and one `None` covering "offline",
    "the send never left", "nobody answered in time" and "the rack went away
    mid-question" is one thing for that caller to check rather than four.
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
        self._pending: dict[str, _Wait] = {}
        """Every question a caller is still waiting on, by `request_id`.

        By the id of the question and not by the rack it was asked of, which is
        the only key that can be right: two of these can be in flight to one
        daemon at once -- a detect and a probe, or two probes from two operators
        -- and a table keyed by the daemon holds one entry for both, so one
        caller gets the other's answer and the other never gets one at all. The
        reply carries the id back for exactly this reason.
        """
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

    def drop(self, connection: Connection) -> bool:
        """Take this connection out of the hub. True if it was the live one.

        The return is what lets a caller tell "the rack has gone" from "this
        handler's socket was superseded and the rack is fine". Both arrive here,
        and they are the same call from the handler's point of view -- it cannot
        know which it is, because the guard below is the only thing that does.
        `ws_daemon` needs the difference in order not to write "the link closed"
        into the history of a rack that is online and streaming.
        """
        connection.closed.set()
        # Identity, not the id: a handler whose socket was replaced still runs
        # its own cleanup, and it arrives here holding a connection the hub
        # forgot about. Matching on `daemon_id` alone would let that late
        # arrival take the live daemon offline -- the rack would be reported
        # unplugged, and every push would be dropped as unsendable, seconds
        # after it reconnected.
        if self._connections.get(connection.daemon_id) is not connection:
            return False
        del self._connections[connection.daemon_id]
        # Nothing is left to answer for this daemon, and the last version it
        # confirmed is not evidence about the one it will be running when it
        # comes back. Also what stops `_acked` outliving the daemon rows.
        self._acked.pop(connection.daemon_id, None)
        self._abandon_waits(connection.daemon_id)
        # After the delete, and only past the identity guard above: a superseded
        # handler reaching here seconds after its daemon reconnected would
        # otherwise paint the rack as unplugged in every open tab while it is
        # streaming frames -- the same failure the guard exists for, arriving at
        # the interface instead of at a push.
        self._announce()
        return True

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

    async def request(
        self, daemon_id: int, message: Request, timeout: float = REQUEST_TIMEOUT
    ) -> Reply | None:
        """Ask one rack something and wait for its answer. None if none came back.

        The one thing on this link that is not fire-and-forget, and the shape
        every later "ask the rack something" feature gets to reuse: the caller
        is an HTTP handler that has to answer somebody, so the question goes out,
        the wait is keyed by the `request_id` the message carries, and the reply
        that quotes that id back *from this rack* resolves it.

        None for every way that fails -- no socket, a send that never left, a
        rack that did not answer inside `timeout`, a rack that disconnected
        mid-question -- because a caller that has to report an outcome has one
        thing to check rather than four exception types, and because none of
        them is an error here. What it is *not* allowed to be is ambiguous with
        a successful answer, which is why `Reply` has no falsy member: a
        `DetectResult` naming no panels at all is a real answer and is not this.

        **Safe to await from an `async def` route, and only from one.** The two
        lines that matter -- the registration and the identity-checked removal --
        each run with no await inside them, and this class is event-loop-affine:
        that is a statement about this loop and says nothing whatever about a
        threadpool. A `def` FastAPI route calling this would be mutating
        `_pending` from another thread while the daemon socket's reader is
        iterating it in `drop`, which is the same failure `relay_frame` copies
        its watcher set to survive, in a table where the consequence is a
        crossed answer rather than a dropped frame.

        `timeout` is a parameter and not only a constant because the two callers
        are not asking the same kind of question -- a detect is a directory
        listing and a probe holds a bus for as long as the operator asked it to.
        The default is the one that has to cover the worst legal case; see
        `REQUEST_TIMEOUT`.

        **Both of those callers pass their own, so the default is a path nothing
        in this server takes.** It is not dead -- it is what the *next* caller
        gets by omitting an argument -- and that is the thing worth knowing
        before omitting one: forty seconds is the worst legal probe, and a
        question that is not a probe inherits a request handler parked for four
        times as long as the answer it was waiting for could have taken, against
        a rack that has simply gone quiet. `ors_server.api.daemons` names its two
        waits and says what each is derived from; a third question should name
        its own rather than take this.

        **Spec §6.4's "one probe at a time per rack" is not enforced here, and
        is owed by `POST /api/daemons/{id}/probe`.** Nothing below bounds how
        many waits one rack may hold, deliberately: §6.4's rule is about what a
        probe does to *hardware* -- it holds a bus -- and not about how many
        correlated waits a link may carry, so enforcing it here would bound every
        later "ask the rack something" feature by a rule that is about one of
        them. And this returns `None`, which is "no answer": only a route can
        refuse a second probe with a 409 and a reason, which is what an operator
        standing in front of the rack needs to be told instead of a timeout.
        """
        if daemon_id not in self._connections:
            # Offline is an answer and it is available now. Registering a wait
            # anyway would spend a whole `timeout` of a request handler proving
            # what `is_online` already knew, and the caller would be told
            # "no answer" instead of "no rack".
            return None
        wait = _Wait(daemon_id=daemon_id, future=asyncio.get_running_loop().create_future())
        # Registered *before* the send, not after it. `_send` awaits, so the
        # daemon socket's reader runs while this call is inside it -- and a rack
        # that answers a detect in a millisecond can have its reply read and
        # delivered before this line would otherwise have been reached. The
        # question that got the fastest answer would be the one that timed out.
        self._pending[message.request_id] = wait
        try:
            if not await self._send(daemon_id, message.model_dump_json()):
                # Nothing was asked, so nothing is coming. `_send` has already
                # dropped the socket if the send is what failed, and has already
                # resolved this wait on the way past -- which is harmless and is
                # not what is answering here.
                return None
            return await asyncio.wait_for(wait.future, timeout)
        except TimeoutError:
            log.warning(
                "a rack did not answer in time",
                extra={"daemon": daemon_id, "request": message.request_id, "timeout_s": timeout},
            )
            return None
        finally:
            # On every way out, and there are four of them: the answer, the
            # timeout, a send that never left, and the caller being cancelled --
            # a shutdown, or a browser that hung up. An entry left behind is
            # never looked at again, because the key was minted for one question
            # that is now over, so it is a future and a message held for the
            # life of the process, once per question asked.
            #
            # By identity, for the reason `drop` matches on identity. The ids are
            # the caller's to mint and this class cannot make them unique: if one
            # is reused, the second request has replaced this entry, and removing
            # it by key alone would take the live wait out from under a caller
            # whose answer is still on its way.
            if self._pending.get(message.request_id) is wait:
                del self._pending[message.request_id]

    def deliver_reply(self, daemon_id: int, message: Reply) -> None:
        """Hand a rack's answer to whoever asked *that rack*. From `ws_daemon`.

        Matched on the id of the question **and** the rack it came back from,
        which is `record_ack`'s identity guard at half strength and for the same
        kind of reason. The ids are the caller's to mint and this class cannot
        make them unique -- `request`'s own cleanup is written around that fact
        -- so matching on the id alone defends against a collision *within* one
        rack while trusting the same ids to be unique *across* racks. A route
        minting `f"detect-{n}"` per rack, or reusing a per-rack counter, makes
        rack 12's answer resolve rack 7's question as a matter of routine: the
        caller is handed a panel list, believes it describes rack 7, and the
        wizard offers an operator wiring for hardware on a different Pi.

        The daemon id, though, and **not** the `Connection` it arrived over --
        which is where this parts company with `record_ack`, for a reason that
        is the same fact seen from the other side. An ack describes a boot, so
        one read from a superseded socket describes the *previous* boot and is
        worthless. A reply answers one question that was asked exactly once, and
        the daemon sends it down whichever socket it has when it is ready: a
        reply arriving over a connection the hub has since replaced is still the
        only answer that question is ever going to get. So this refuses another
        *rack*, and nothing else.

        A reply nobody is waiting for is dropped and said so, never raised. All
        three ways it happens are ordinary -- the wait expired and the rack
        answered anyway, this build never asked at all, or the id belongs to
        another rack's question -- and the caller is the daemon socket handler,
        which catches a disconnect and a validation error and nothing else. A
        `KeyError` out of here is a closed link and a rack offline for the
        duration, over a message the daemon got right.
        """
        wait = self._pending.get(message.request_id)
        if wait is None or wait.daemon_id != daemon_id or wait.future.done():
            log.info(
                "a reply arrived that nobody is waiting for",
                extra={"daemon": daemon_id, "request": message.request_id, "said": message.type},
            )
            return
        wait.future.set_result(message)

    def _abandon_waits(self, daemon_id: int) -> None:
        """Tell everyone waiting on this rack that it has gone, now.

        The alternative is each of them sitting out its own `REQUEST_TIMEOUT`
        for a rack the hub has already taken offline -- forty seconds of a
        request handler apiece, spent on a socket that is known to be closed.

        Called from past `drop`'s identity guard, which is where the whole of
        this decision lives. A superseded handler's cleanup arrives there seconds
        after the rack reconnected, and the questions in flight are still
        perfectly answerable: the daemon replies down whatever socket it has now,
        and `deliver_reply` matches the rack and the question rather than the
        connection. Failing them from a stale handler would refuse an answer that
        was already coming.

        The entries themselves are left for `request`'s own `finally` to remove.
        One owner for that removal, and it is the only one that can check the
        identity of what it is removing.
        """
        gone = [
            request_id
            for request_id, wait in self._pending.items()
            if wait.daemon_id == daemon_id and not wait.future.done()
        ]
        for request_id in gone:
            # `set_result` schedules the waiting task rather than running it, so
            # nothing re-enters this class before the loop above is finished --
            # but the list is built first regardless, because a fan-out that
            # mutates what it is iterating is the bug this file has already had.
            self._pending[request_id].future.set_result(None)
        if gone:
            log.info(
                "a rack went away with questions in flight",
                extra={"daemon": daemon_id, "requests": sorted(gone)},
            )

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
        # **No await may precede this check.** `request` refuses an offline rack
        # up front, and that refusal is only belt-and-braces because this returns
        # False without ever suspending -- so a wait registered ahead of the send
        # is removed in the same synchronous stretch. Anything awaited above this
        # line -- a per-daemon lock, a metric, a queue -- makes that refusal the
        # only thing standing between an offline rack and a wait left visible
        # across a yield for another handler to resolve, and no test here says so.
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
