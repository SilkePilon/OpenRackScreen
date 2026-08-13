from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_schema.link import Ack, Frame, Heartbeat, Hello, Nack, SourceStatus
from ors_server.app import AppSettings, create_app
from ors_server.link.hub import Hub
from ors_server.link.ws_daemon import (
    CLOSE_HELLO_FIRST,
    CLOSE_MALFORMED,
    CLOSE_SUPERSEDED,
    CLOSE_UNAUTHORIZED,
    CLOSE_UNSERVABLE,
    daemon_socket,
)
from ors_server.pairing import mint_token
from starlette.websockets import WebSocketDisconnect

DISPLAY = {"backend": "virtual", "out_dir": "/tmp/p"}

# A deadline, not a wait: every assertion below it is satisfied by an event, so
# a passing run never spends any of it. It exists so that a handler which fails
# to notice something fails the test instead of hanging the suite -- the failure
# this file cares most about, a socket left blocked in `receive`, is otherwise
# indistinguishable from a test runner that stopped.
#
# It covers the `FakeSocket` half of this file only, because it is spelled with
# `asyncio.wait_for` and the `TestClient` half has no event loop of its own to
# hang one off. That half's deadline is `timeout = 60` in the root pytest
# configuration, which is a signal and interrupts a blocked thread -- three
# separate one-line edits to `ws_daemon.py` were each measured wedging a
# `TestClient` test for as long as it was allowed to run, because
# `starlette.testclient`'s `receive` blocks on a future with no timeout.
PROMPTLY = 5.0


def build(tmp_path, name: str = "pi-rack"):
    app = create_app(AppSettings(data_dir=tmp_path))
    daemon_id, token = mint_token(app.state.database, name)
    return app, daemon_id, token


def client_for(tmp_path, name: str = "pi-rack"):
    app, daemon_id, token = build(tmp_path, name)
    return TestClient(app), daemon_id, token


def hello(credential: str, **fields) -> str:
    return Hello(
        token=credential, hostname="pi-rack", daemon_version="0.1.0", **fields
    ).model_dump_json()


def add_screen(app, daemon_id: int, name: str = "CPU", position: int = 1) -> int:
    with closing(app.state.database.connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO screen (daemon_id, position, name, display, template, params)"
            " VALUES (?, ?, ?, ?, 'ring-gauge', '{}')",
            (daemon_id, position, name, json.dumps(DISPLAY)),
        )
    return int(cursor.lastrowid)


def daemon_row(app, daemon_id: int):
    with closing(app.state.database.connect()) as connection:
        return connection.execute("SELECT * FROM daemon WHERE id = ?", (daemon_id,)).fetchone()


class WatchfulHub(Hub):
    """A hub that says when it has been told something, so no test has to poll.

    The alternative is a loop yielding to the event loop until the value shows
    up, which passes for the wrong reason as readily as the right one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.recorded = asyncio.Event()

    def record_ack(self, connection, version: int) -> None:
        super().record_ack(connection, version)
        self.recorded.set()


class FakeSocket:
    """A daemon socket, minus the socket and minus the server around it.

    The handler is a coroutine taking a WebSocket, so the whole conversation is
    drivable from a test with no port, no threads and no clock: the script is
    queued before the handler starts, and `None` in it is the daemon going away.

    `TestClient` covers the same route end to end below, but it cannot do this
    much -- its `receive` blocks a thread on a future with no timeout of its
    own, so a handler that should have sent something and did not stops that
    test dead until the suite's own deadline cuts it short, and asserting that
    *nothing* was sent is not expressible at all.
    """

    def __init__(self, app, *script: str, hang_up: bool = True) -> None:
        self.app = app
        self.accepted = False
        self.sent: list[str] = []
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.close_code: int | None = None
        self.close_reason: str | None = None
        for line in script:
            self.inbox.put_nowait(line)
        if hang_up:
            self.inbox.put_nowait(None)

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        raw = await self.inbox.get()
        if raw is None:
            raise WebSocketDisconnect(1006)
        return raw

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)
        self.outbox.put_nowait(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code
        self.close_reason = reason

    def say(self, raw: str) -> None:
        self.inbox.put_nowait(raw)

    def hang_up(self) -> None:
        self.inbox.put_nowait(None)

    async def next_message(self) -> dict:
        return json.loads(await asyncio.wait_for(self.outbox.get(), PROMPTLY))

    @property
    def messages(self) -> list[dict]:
        return [json.loads(payload) for payload in self.sent]


async def run(socket: FakeSocket) -> None:
    await asyncio.wait_for(daemon_socket(socket), PROMPTLY)


async def connected(app, credential: str) -> tuple[FakeSocket, asyncio.Task]:
    """A daemon past its hello, with the handler still reading.

    Waiting for the push is what makes the rest deterministic: by the time it
    has been sent the connection is registered, and every later `say` is read in
    the order it was queued.
    """
    socket = FakeSocket(app, hello(credential), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    await socket.next_message()
    return socket, handler


async def finish(socket: FakeSocket, handler: asyncio.Task) -> None:
    socket.hang_up()
    await asyncio.wait_for(handler, PROMPTLY)


async def paired_key(app, token: str) -> str:
    """Pair once and hand back the key, the way the daemon's first connect does."""
    socket = FakeSocket(app, hello(token))
    await run(socket)
    return socket.messages[0]["key"]


# --- the credential, end to end -------------------------------------------


def test_a_valid_token_is_accepted_and_answered_with_a_key_and_a_snapshot(tmp_path):
    client, _, token = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        first = json.loads(socket.receive_text())
        second = json.loads(socket.receive_text())

    assert first["type"] == "paired", "the key has to arrive before anything can be refused"
    assert first["key"]
    assert second["type"] == "config"
    assert second["snapshot"]["timezone"]


def test_a_bad_token_is_refused_and_the_socket_closes(tmp_path):
    client, _, _ = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello("not-the-token"))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == CLOSE_UNAUTHORIZED


def test_no_credential_at_all_is_refused_exactly_like_a_wrong_one(tmp_path):
    """Same code, same reason. Which of the two it was is the attacker's to know.

    A distinguishable refusal is a probe: it tells whoever is scanning the LAN
    whether the string they sent was a credential the server has heard of.
    """
    client, _, _ = client_for(tmp_path)

    codes = []
    for credential in ("", "not-the-token"):
        with client.websocket_connect("/ws/daemon") as socket:
            socket.send_text(hello(credential))
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
            codes.append((closed.value.code, closed.value.reason))

    assert codes[0] == codes[1] == (CLOSE_UNAUTHORIZED, "unauthorized")


def test_a_token_cannot_be_claimed_twice(tmp_path):
    client, _, token = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == CLOSE_UNAUTHORIZED, "a spent token is a second daemon"


def test_a_hostname_is_not_a_credential(tmp_path):
    """The one that decides whether this is pairing or theatre.

    The token's hash is gone once it is spent, so a reconnect has to be
    authenticated by something else. If that something is the hostname in
    `hello`, then anyone on the LAN who connects saying `pi-rack` is handed the
    rack's whole configuration -- every integration, every URL -- and can push
    frames onto its panels. That is not weaker pairing; after the first connect
    it is none.
    """
    client, daemon_id, token = client_for(tmp_path)
    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

    with client.websocket_connect("/ws/daemon") as impostor:
        impostor.send_text(hello("i-know-the-hostname"))
        with pytest.raises(WebSocketDisconnect) as closed:
            impostor.receive_text()

    assert closed.value.code == CLOSE_UNAUTHORIZED
    assert client.app.state.hub.is_online(daemon_id) is False


def test_the_key_gets_the_daemon_back_in(tmp_path):
    client, daemon_id, token = client_for(tmp_path)
    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        key = json.loads(socket.receive_text())["key"]
        socket.receive_text()

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(key))
        message = json.loads(socket.receive_text())

        assert message["type"] == "config", "a paired daemon is not handed a second key"
        assert client.app.state.hub.is_online(daemon_id) is True


def test_a_message_before_hello_is_refused(tmp_path):
    client, _, _ = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(Ack(config_version=1).model_dump_json())
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == CLOSE_HELLO_FIRST, "nothing is accepted from an unidentified socket"


def test_a_first_message_that_is_not_a_message_at_all_is_refused(tmp_path):
    client, _, _ = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text("{not json")
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == CLOSE_MALFORMED


def test_hello_records_what_the_daemon_says_it_is(tmp_path):
    client, daemon_id, token = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(
            Hello(
                token=token,
                hostname="pi-rack",
                daemon_version="0.1.0",
                capabilities={"spi": [0, 1]},
            ).model_dump_json()
        )
        socket.receive_text()

    row = daemon_row(client.app, daemon_id)
    assert row["status"] == "paired"
    assert row["version"] == "0.1.0"
    assert json.loads(row["capabilities"]) == {"spi": [0, 1]}
    assert row["last_seen"]


def test_a_connected_daemon_is_online_and_a_disconnected_one_is_not(tmp_path):
    client, daemon_id, token = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

        assert client.app.state.hub.is_online(daemon_id) is True

    assert client.app.state.hub.is_online(daemon_id) is False


# --- what is pushed, and when ---------------------------------------------


async def test_a_daemon_with_no_config_is_pushed_to(tmp_path):
    app, daemon_id, token = build(tmp_path)
    add_screen(app, daemon_id)

    socket = FakeSocket(app, hello(token))
    await run(socket)

    types = [message["type"] for message in socket.messages]
    assert types == ["paired", "config"]
    assert socket.messages[1]["snapshot"]["screens"][0]["name"] == "CPU"


async def test_a_daemon_already_running_this_version_is_not_pushed_to_again(tmp_path):
    """The reconnect that must not repaint the rack.

    Applying a snapshot revokes every panel, joins every worker and reopens
    them, so an unnecessary push turns a wifi blip into a rack-wide flicker.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)

    socket = FakeSocket(app, hello(key, config_version=0))
    await run(socket)

    assert socket.messages == [], "the daemon said it already had this one"


async def test_a_daemon_running_something_else_is_pushed_to(tmp_path):
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)

    socket = FakeSocket(app, hello(key, config_version=6))
    await run(socket)

    assert [message["type"] for message in socket.messages] == ["config"]
    assert socket.messages[0]["version"] == 0, "the version on the row, not the daemon's claim"


async def test_a_daemon_claiming_no_version_is_always_pushed_to(tmp_path):
    """None never matches, and 0 is a real version -- the one a fresh server is on.

    A daemon whose cache is gone says None. If that were read as 0 it would
    match an untouched server exactly, nothing would be pushed, and the rack
    would stay blank against a server that believed it was up to date.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)

    socket = FakeSocket(app, hello(key))
    await run(socket)

    assert [message["type"] for message in socket.messages] == ["config"]


async def test_connecting_does_not_bump_the_config_version(tmp_path):
    """A connect changes no configuration, so it may not advance the counter.

    Bumping here would be self-defeating twice over: the daemon's claim could
    never match a number minted after it spoke, so the comparison above would be
    dead code and every reconnect a full repaint -- and a flapping link would
    walk the counter up forever.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    before = daemon_row(app, daemon_id)["config_version"]

    await run(FakeSocket(app, hello(key)))
    await run(FakeSocket(app, hello(key)))

    assert daemon_row(app, daemon_id)["config_version"] == before


async def test_a_snapshot_no_daemon_could_run_closes_the_socket_rather_than_crashing(tmp_path):
    """A column the database holds and no daemon could be given.

    Unhandled, it escapes the route as a 500 on a socket -- which is a close
    with no code anyone can act on, and a traceback in the log of a server that
    was asked a perfectly ordinary question.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    with closing(app.state.database.connect()) as connection:
        connection.execute(
            "INSERT INTO screen (daemon_id, position, name, display, template, params)"
            " VALUES (?, 1, 'CPU', 'not json at all', 'ring-gauge', '{}')",
            (daemon_id,),
        )

    socket = FakeSocket(app, hello(key))
    await run(socket)

    assert socket.close_code == CLOSE_UNSERVABLE
    assert app.state.hub.is_online(daemon_id) is False, "it never became a connection"


# --- what the daemon says back --------------------------------------------


async def test_an_ack_is_recorded_for_the_connection_that_sent_it(tmp_path):
    """Recorded against the `Connection`, not the id.

    A superseded handler is still a reader, and the ack it is holding was read
    from a socket the daemon has already left; under the id it would describe
    the previous boot. The hub refuses that -- but only if it is handed
    something it can check the identity of.
    """
    app, daemon_id, token = build(tmp_path)
    hub = app.state.hub = WatchfulHub()
    socket = FakeSocket(app, hello(token), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    await socket.next_message()
    push = await socket.next_message()

    socket.say(Ack(config_version=push["version"]).model_dump_json())
    await asyncio.wait_for(hub.recorded.wait(), PROMPTLY)

    assert hub.acked_version(daemon_id) == push["version"]
    socket.hang_up()
    await asyncio.wait_for(handler, PROMPTLY)


async def test_a_heartbeat_keeps_the_daemon_from_looking_lost(tmp_path):
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    # After the hello, which stamps it too: otherwise this passes on the
    # strength of a write that has nothing to do with heartbeats.
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET last_seen = 'long ago' WHERE id = ?", (daemon_id,))

    socket.say(Heartbeat(uptime_s=12).model_dump_json())
    await finish(socket, handler)

    assert daemon_row(app, daemon_id)["last_seen"] != "long ago"


async def test_a_frame_does_not_write_a_row_every_time_it_arrives(tmp_path):
    """Frames come at 2 fps per screen. `last_seen` is a coarse liveness field.

    A write per frame buys nothing the heartbeat does not already say, and puts
    a SQLite write on the event loop several times a second per rack.
    """
    app, daemon_id, token = build(tmp_path)
    screen_id = add_screen(app, daemon_id)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET last_seen = 'long ago' WHERE id = ?", (daemon_id,))

    socket.say(Frame(screen_id=screen_id, seq=1, webp=b"RIFF").model_dump_json())
    await finish(socket, handler)

    assert daemon_row(app, daemon_id)["last_seen"] == "long ago"


async def test_one_malformed_message_does_not_kill_a_healthy_link(tmp_path):
    """A whole rack's link is not the right price for one unreadable frame.

    The alternative -- closing -- turns a single bad message into a reconnect
    loop that re-pushes the config every time round. It is logged instead, and
    `extra="forbid"` on every model is what stops a malformed message being
    silently mistaken for a well-formed one of another type.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    # Stamped after the hello, which writes `last_seen` itself: reading the
    # column without this passes whether or not anything after the bad message
    # was ever read, which is the one thing this test is about.
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET last_seen = 'long ago' WHERE id = ?", (daemon_id,))

    socket.say('{"type": "not-a-message-type"}')
    socket.say(SourceStatus(integration="prom", state="ok").model_dump_json())
    socket.say(Nack(config_version=1, reason="no").model_dump_json())
    socket.say(Heartbeat().model_dump_json())
    await finish(socket, handler)

    assert daemon_row(app, daemon_id)["last_seen"] != "long ago", "it kept reading"


# --- frames, and who owns a screen ----------------------------------------


async def test_a_frame_for_an_owned_screen_reaches_the_browsers_watching_it(tmp_path):
    app, daemon_id, token = build(tmp_path)
    screen_id = add_screen(app, daemon_id)
    key = await paired_key(app, token)
    queue: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(screen_id, queue)

    frame = Frame(screen_id=screen_id, seq=7, webp=b"RIFF").model_dump_json()
    await run(FakeSocket(app, hello(key), frame))

    assert queue.get_nowait().seq == 7


async def test_a_frame_for_another_racks_screen_is_refused(tmp_path):
    """The only place in the system that can check this.

    `relay_frame` trusts `frame.screen_id` and the hub reads no rows by design,
    so a daemon -- or anyone holding a daemon's key -- could otherwise paint
    over a panel belonging to a rack it has nothing to do with, and every
    browser watching that screen would show it.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    with closing(app.state.database.connect()) as connection:
        stranger = int(
            connection.execute(
                "INSERT INTO daemon (name, status, created_at) VALUES ('other', 'paired', 'x')"
            ).lastrowid
        )
    theirs = add_screen(app, stranger, name="Theirs")
    mine = add_screen(app, daemon_id, name="Mine")
    watched: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(theirs, watched)
    ours: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(mine, ours)

    await run(
        FakeSocket(
            app,
            hello(key),
            Frame(screen_id=theirs, seq=1, webp=b"RIFF").model_dump_json(),
            Frame(screen_id=mine, seq=2, webp=b"RIFF").model_dump_json(),
        )
    )

    assert ours.get_nowait().seq == 2, "and the link survived being lied to"
    assert watched.empty(), "a frame for a screen this daemon does not own"


async def test_a_screen_added_after_hello_is_not_a_stranger(tmp_path):
    """Ownership is read at hello, and rows change under a live connection.

    The interface adds a screen and pushes the new config through the hub, which
    knows nothing about this handler's idea of who owns what. A set captured at
    hello and never revisited would refuse the first frames of every screen
    added while the rack was connected.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)

    screen_id = add_screen(app, daemon_id, name="Added later")
    queue: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(screen_id, queue)
    socket.say(Frame(screen_id=screen_id, seq=3, webp=b"RIFF").model_dump_json())
    frame = await asyncio.wait_for(queue.get(), PROMPTLY)

    assert frame.seq == 3
    await finish(socket, handler)


async def test_frame_streaming_resumes_only_for_the_screens_this_daemon_owns(tmp_path):
    """`watched_screens()` is every screen anyone is watching, across all racks.

    Resumed wholesale, a reconnecting daemon is asked to render screens that
    belong to another Pi -- ids it has never heard of, at 2 fps.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    with closing(app.state.database.connect()) as connection:
        stranger = int(
            connection.execute(
                "INSERT INTO daemon (name, status, created_at) VALUES ('other', 'paired', 'x')"
            ).lastrowid
        )
    mine = add_screen(app, daemon_id, name="Mine")
    theirs = add_screen(app, stranger, name="Theirs")
    for screen_id in (mine, theirs):
        app.state.hub.subscribe_frames(screen_id, asyncio.Queue())

    socket = FakeSocket(app, hello(key))
    await run(socket)

    requests = [message for message in socket.messages if message["type"] == "frames"]
    assert requests == [{"type": "frames", "enabled": True, "screen_ids": [mine], "fps": 2.0}]


async def test_a_daemon_nobody_is_watching_is_not_asked_for_frames(tmp_path):
    app, daemon_id, token = build(tmp_path)
    add_screen(app, daemon_id)
    key = await paired_key(app, token)

    socket = FakeSocket(app, hello(key))
    await run(socket)

    assert [message["type"] for message in socket.messages] == ["config"]


# --- the connection this one replaced --------------------------------------


async def test_a_second_connection_closes_the_first_without_waiting_for_a_timeout(tmp_path):
    """The carried obligation from the hub: the hub cannot close a socket.

    It holds a `send` callable and sets `Connection.closed` instead. A handler
    that only awaits `receive` sits there until uvicorn's ping timeout -- tens
    of seconds during which it is still a reader, still able to hand the hub an
    ack from a daemon that has already gone.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    first = FakeSocket(app, hello(key), hang_up=False)
    first_handler = asyncio.create_task(daemon_socket(first))
    await first.next_message()

    second = FakeSocket(app, hello(key), hang_up=False)
    second_handler = asyncio.create_task(daemon_socket(second))
    await second.next_message()

    await asyncio.wait_for(first_handler, PROMPTLY)
    assert first.close_code == CLOSE_SUPERSEDED
    assert app.state.hub.is_online(daemon_id) is True, "the live one stays live"

    second.hang_up()
    await asyncio.wait_for(second_handler, PROMPTLY)
    assert app.state.hub.is_online(daemon_id) is False


async def test_a_superseded_handler_leaving_does_not_take_the_live_daemon_offline(tmp_path):
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    first = FakeSocket(app, hello(key), hang_up=False)
    first_handler = asyncio.create_task(daemon_socket(first))
    await first.next_message()
    second = FakeSocket(app, hello(key), hang_up=False)
    second_handler = asyncio.create_task(daemon_socket(second))
    await second.next_message()

    await asyncio.wait_for(first_handler, PROMPTLY)

    assert app.state.hub.is_online(daemon_id) is True
    second.hang_up()
    await asyncio.wait_for(second_handler, PROMPTLY)


async def test_a_message_arriving_as_the_connection_is_replaced_is_not_acted_on(tmp_path):
    """The tie in the race, which is the case the race exists for.

    A daemon reboots and reconnects; its old socket still holds a frame read
    from the boot before. If the reader is allowed to win a pass in which the
    connection was also replaced, that stale image is fanned out to every
    browser watching the panel -- over the top of the live daemon's.

    The frame and the replacement are queued with no await between them, so both
    halves of the race really do complete in the same pass.
    """
    app, daemon_id, token = build(tmp_path)
    screen_id = add_screen(app, daemon_id)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    watching: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(screen_id, watching)

    socket.say(Frame(screen_id=screen_id, seq=9, webp=b"RIFF").model_dump_json())
    app.state.hub.register(daemon_id, nowhere)

    await asyncio.wait_for(handler, PROMPTLY)
    assert socket.close_code == CLOSE_SUPERSEDED
    assert watching.empty(), "a frame from a socket the hub had already replaced"


async def nowhere(payload: str) -> None:
    """The send of the connection that supersedes one, going nowhere."""


# --- the route, where the app puts it --------------------------------------


def test_the_daemon_socket_is_at_the_root_where_the_design_puts_it(tmp_path):
    app = create_app(AppSettings(data_dir=tmp_path))

    paths = {
        context.path or context.original_route.path for context in iter_route_contexts(app.routes)
    }

    assert "/ws/daemon" in paths, "under /api it would be /api/ws/daemon, which no daemon dials"


def test_the_app_seeds_the_templates_a_snapshot_has_to_name(tmp_path):
    """Without this a fresh server pushes screens naming templates it does not have.

    `build_snapshot` refuses a screen whose template is missing, so the whole
    rack's first push would fail rather than one panel.
    """
    app = create_app(AppSettings(data_dir=tmp_path))

    with closing(app.state.database.connect()) as connection:
        count = connection.execute("SELECT count(*) FROM template WHERE builtin = 1").fetchone()[0]

    assert count > 0
