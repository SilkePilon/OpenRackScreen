from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ors_schema.link import Command, ConfigPush, Frame, FramesRequest

log = logging.getLogger(__name__)

Sender = Callable[[str | bytes], Awaitable[None]]


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
    """

    def __init__(self) -> None:
        self._connections: dict[int, Connection] = {}
        self._acked: dict[int, int] = {}
        self._watchers: dict[int, set[asyncio.Queue[Frame]]] = {}

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

    def is_online(self, daemon_id: int) -> bool:
        return daemon_id in self._connections

    def online_ids(self) -> set[int]:
        return set(self._connections)

    def record_ack(self, daemon_id: int, version: int) -> None:
        self._acked[daemon_id] = version

    def acked_version(self, daemon_id: int) -> int | None:
        """The version this daemon has confirmed applying, or None if unknown.

        None is "ask again", not "old": it is what an unconnected daemon, a
        daemon that has not answered its first push, and a daemon that has just
        reconnected all report, and the answer to all three is to push.
        """
        return self._acked.get(daemon_id)

    async def push_config(self, daemon_id: int, push: ConfigPush) -> None:
        await self._send(daemon_id, push.model_dump_json())

    async def send_command(self, daemon_id: int, command: Command) -> None:
        await self._send(daemon_id, command.model_dump_json())

    async def request_frames(self, daemon_id: int, request: FramesRequest) -> None:
        await self._send(daemon_id, request.model_dump_json())

    def subscribe_frames(self, screen_id: int, queue: asyncio.Queue[Frame]) -> bool:
        """Returns True when this is the first watcher, which starts the daemon."""
        watchers = self._watchers.setdefault(screen_id, set())
        first = not watchers
        watchers.add(queue)
        return first

    def unsubscribe_frames(self, screen_id: int, queue: asyncio.Queue[Frame]) -> bool:
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

    async def relay_frame(self, frame: Frame) -> None:
        """Fan one frame out to whoever is watching that screen.

        A coroutine although it awaits nothing: this is the call the daemon
        socket makes for every frame it reads, and a fan-out that has to await
        anything at all -- a per-watcher send, a backpressure signal -- would
        otherwise change every call site. Awaiting nothing is also what makes
        the body below safe, and the two facts are the same fact.
        """
        for queue in self._watchers.get(frame.screen_id, ()):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # The newest frame wins, which the design says in as many words:
                # a stale panel image is worse than a skipped one. So the oldest
                # goes and this one takes its place -- discarding *this* one
                # instead would serve a browser that stalled for a second and
                # recovered its backlog, frame by stale frame, while every fresh
                # one was thrown away.
                #
                # There is no await between the two calls, so nothing can touch
                # the queue in between: the space made is the space used, and
                # the set being iterated cannot change under the loop either.
                queue.get_nowait()
                queue.put_nowait(frame)
                log.debug("dropped a frame for a slow watcher", extra={"screen": frame.screen_id})

    async def _send(self, daemon_id: int, payload: str) -> None:
        connection = self._connections.get(daemon_id)
        if connection is None:
            # Offline is a normal state, not an error: the edit is already
            # saved, and the snapshot is pushed again when it reconnects.
            return
        try:
            await connection.send(payload)
        except Exception as exc:
            log.info("daemon send failed; dropping", extra={"daemon": daemon_id, "error": str(exc)})
            self.drop(connection)
