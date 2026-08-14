from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import closing

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_schema.link import (
    MAX_FRAME_BYTES,
    MAX_WATCHED_SCREENS,
    PROTOCOL_VERSION,
    Ack,
    Frame,
    Heartbeat,
    Hello,
    Nack,
    SourceStatus,
)
from ors_server.app import AppSettings, create_app
from ors_server.link import ws_daemon
from ors_server.link.hub import Hub
from ors_server.link.ws_daemon import (
    CLOSE_HELLO_FIRST,
    CLOSE_MALFORMED,
    CLOSE_PROTOCOL_SKEW,
    CLOSE_SLOW_HELLO,
    CLOSE_SUPERSEDED,
    CLOSE_UNAUTHORIZED,
    CLOSE_UNSERVABLE,
    _authenticate,
    _handle,
    _serve,
    _Session,
    daemon_socket,
)
from ors_server.pairing import _fingerprint, mint_token
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
        self.idle = asyncio.Event()
        for line in script:
            self.inbox.put_nowait(line)
        if hang_up:
            self.inbox.put_nowait(None)

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if self.inbox.empty():
            # Asking for a message there is nothing to answer with is the one
            # moment that says everything queued has been handled: the loop
            # reads, handles, and only then reads again. See `handled`.
            self.idle.set()
        raw = await self.inbox.get()
        if raw is None:
            raise WebSocketDisconnect(1006)
        return raw

    async def handled(self) -> None:
        """Wait until everything said so far has been read and acted on.

        A test that changes a row between two frames has to know which frame saw
        the change, and a refused frame produces nothing else to wait on -- that
        is what being refused means. Yielding to the loop a fixed number of
        times would pass for the wrong reason as readily as the right one.
        """
        self.idle.clear()
        await asyncio.wait_for(self.idle.wait(), PROMPTLY)

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


class GatedSocket(FakeSocket):
    """A socket whose first send suspends and never finishes.

    The window between `hub.register` and `_hello` returning a session is
    otherwise not reachable from a test: everything in it is either synchronous
    or a send that completes at once. The config push is the await that holds it
    open, which is also the one a real wedged Pi holds open.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()

    async def send_text(self, payload: str) -> None:
        self.started.set()
        await asyncio.Event().wait()


class VanishingSocket(FakeSocket):
    """A socket whose first send is the moment this daemon is deleted.

    Not contrived. `Paired` goes on the wire between the read that authenticates
    a daemon and the read that gets its configuration version, and that write is
    the only place in `_hello` that yields to the event loop -- so a
    `DELETE /api/daemons/{id}` landing in the gap really does leave the second
    read with no row.
    """

    async def send_text(self, payload: str) -> None:
        await super().send_text(payload)
        with closing(self.app.state.database.connect()) as connection:
            connection.execute(
                "DELETE FROM daemon WHERE id = ?", (json.loads(payload)["daemon_id"],)
            )


async def run(socket: FakeSocket) -> None:
    await asyncio.wait_for(daemon_socket(socket), PROMPTLY)


def running_tasks() -> set[asyncio.Task]:
    """Everything still on this loop, which is how a leaked reader is visible.

    `asyncio.wait` does not cancel the awaitables it was handed, so a handler
    cancelled mid-race leaves its `receive_text()` behind and nothing else in
    the test can see it -- the socket looks closed and the handler looks gone.
    """
    return {task for task in asyncio.all_tasks() if not task.done()}


def counted(monkeypatch) -> list[int]:
    """Every ownership query, so a bound on them can be asserted rather than hoped."""
    calls: list[int] = []
    real = ws_daemon._owned_screens

    def counting(database, daemon_id: int) -> set[int]:
        calls.append(daemon_id)
        return real(database, daemon_id)

    monkeypatch.setattr(ws_daemon, "_owned_screens", counting)
    return calls


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
    add_screen(app, daemon_id)
    key = await paired_key(app, token)

    socket = FakeSocket(app, hello(key, config_version=0))
    await run(socket)

    assert socket.messages == [], "the daemon said it already had this one"


async def test_the_connect_that_pairs_is_pushed_to_whatever_it_claims(tmp_path):
    """A first pairing cannot be talked out of its configuration.

    The version comparison asks "does the daemon already have what I would
    send?", and at the moment a token is spent the answer is known outright:
    this server has given this daemon nothing. So on that one connect the claim
    is not admissible, and it is the connect where believing it is worst -- a
    re-imaged Pi with a stale cache claims 0, an untouched server is on the
    schema default of 0, and the two match exactly.

    Nothing recovers from that. `Hub.register` has just cleared the ack, so the
    server's record of what this daemon is running stays "unknown" rather than
    becoming "0", and no push is retried until an unrelated edit bumps the
    counter. The rack sits blank against a server that thinks it is up to date.
    """
    app, daemon_id, token = build(tmp_path)
    add_screen(app, daemon_id)

    socket = FakeSocket(app, hello(token, config_version=0))
    await run(socket)

    assert [message["type"] for message in socket.messages] == ["paired", "config"]
    assert socket.messages[1]["snapshot"]["screens"][0]["name"] == "CPU"


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
    """The happy path: a live daemon answers its push and the hub believes it.

    Only that. What the ack is recorded *against* is the next test's -- this one
    passes whether the handler hands the hub a `Connection` or a bare id.
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


async def test_an_ack_from_a_superseded_handler_is_discarded(tmp_path):
    """The guard the previous test cannot reach, and the reason it takes a `Connection`.

    A handler whose socket the hub has replaced is still a reader, and the ack
    it is holding was read from the boot the daemon has already left. Recorded
    under the daemon id it describes that dead boot -- so a caller comparing
    versions before pushing sees a match, pushes nothing, and leaves a freshly
    rebooted Pi blank against a server that believes it is up to date. Which is
    exactly what `Hub.register` clears the ack to prevent.

    Whitebox, because the loop is built so this cannot be provoked from
    outside: `superseded` is checked before the message, so an ack read in the
    same pass as the replacement is never handled at all. That is the belt; this
    is the braces, and braces nothing pulls on are not braces.
    """
    app, daemon_id, token = build(tmp_path)
    hub = app.state.hub
    stale = _Session(daemon_id=daemon_id, connection=hub.register(daemon_id, nowhere), owned=set())
    hub.register(daemon_id, nowhere)  # the daemon rebooted; this is the live socket

    await _handle(app.state, stale, Ack(config_version=7))

    assert hub.acked_version(daemon_id) is None, "an ack from a boot that is over"


async def test_a_hello_does_not_get_to_say_what_this_daemon_s_standing_is(tmp_path):
    """`status` is pairing's to write, and connecting is not pairing.

    `_record_hello` writes everything the daemon says about itself, and its
    standing with the server is the one thing that is not that. A hello that
    could set it would let a rack an operator has just taken out of service put
    itself back the moment its link came up -- on no authority beyond having
    reconnected with a key it already had.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET status = 'disabled' WHERE id = ?", (daemon_id,))

    await run(FakeSocket(app, hello(key)))

    row = daemon_row(app, daemon_id)
    assert row["status"] == "disabled"
    assert row["version"] == "0.1.0", "and it did record the things that are the daemon's to say"


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


async def test_a_frame_too_large_to_read_names_the_screen_that_went_quiet(tmp_path, caplog):
    """The only place in the system that can say which panel stopped.

    An oversized frame is dropped here and the browser watching that screen is
    told nothing whatever -- the queue simply stops and the page keeps showing
    the image it last drew -- so `frame.webp: bytes_too_long` on its own leaves
    a four-panel rack with three suspects. The socket survives either way, which
    is the other half of the bargain and is what the reads after it prove.
    """
    app, daemon_id, token = build(tmp_path)
    screen_id = add_screen(app, daemon_id)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)

    oversized = json.dumps(
        {
            "type": "frame",
            "screen_id": screen_id,
            "seq": 1,
            "webp": base64.urlsafe_b64encode(b"x" * (MAX_FRAME_BYTES + 1)).decode(),
        }
    )
    with caplog.at_level(logging.WARNING):
        socket.say(oversized)
        socket.say(Frame(screen_id=screen_id, seq=2, webp=b"RIFF").model_dump_json())
        await finish(socket, handler)

    [dropped] = [record for record in caplog.records if "unreadable message" in record.message]
    assert dropped.screen == screen_id
    assert "webp" in dropped.error


async def test_an_unreadable_message_that_names_no_screen_still_logs(tmp_path, caplog):
    """The id is read out of a payload that has already failed validation, so
    nothing about its shape may be assumed: anything that is not an object with
    an integer `screen_id` gets no field rather than a guess."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)

    with caplog.at_level(logging.WARNING):
        socket.say('{"type": "frame", "screen_id": "not-a-number", "seq": 1, "webp": "AA=="}')
        socket.say("[]")
        socket.say("not json at all")
        await finish(socket, handler)

    dropped = [record for record in caplog.records if "unreadable message" in record.message]
    assert len(dropped) == 3
    assert all(not hasattr(record, "screen") for record in dropped)


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


async def test_frames_for_screens_nobody_owns_cost_one_query_and_one_line_a_window(
    tmp_path, monkeypatch, caplog
):
    """`screen_id` is an unbounded int, and frames arrive as fast as the socket.

    Measured before this bound: 500 frames naming 500 *distinct* ids produced
    500 ownership queries -- synchronous SQLite, on the event loop, at 74
    microseconds each -- 500 entries in a per-connection set nothing ever
    cleared, and 500 WARNING lines. All three are attacker-chosen, from a
    daemon that only has to be able to send frames.

    A window rather than a memo of the ids already refused: one re-read per
    window bounds the queries whatever ids arrive, the memory is a counter, and
    the log gets one line carrying how many frames it stands for.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    queries = counted(monkeypatch)
    socket, handler = await connected(app, key)
    caplog.set_level("WARNING", logger="ors_server.link.ws_daemon")
    del queries[:]  # the hello's read, which is not what is being bounded

    for screen_id in range(1000, 1500):
        socket.say(Frame(screen_id=screen_id, seq=1, webp=b"RIFF").model_dump_json())
    # A message the handler answers, so the test knows all 500 have been read
    # without polling for something that is supposed not to happen.
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET last_seen = 'long ago' WHERE id = ?", (daemon_id,))
    socket.say(Heartbeat().model_dump_json())
    await finish(socket, handler)

    assert daemon_row(app, daemon_id)["last_seen"] != "long ago", "all of them were read"
    assert queries == [daemon_id], "one window, one query, however many ids arrive"
    assert len(caplog.records) == 1, "one line, however many frames it stands for"
    assert caplog.records[0].dropped == 1


async def test_the_frames_a_window_swallowed_are_counted_into_the_next_line(
    tmp_path, monkeypatch, caplog
):
    """Rate-limited, not thrown away: the count is what the line is worth reading for."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    caplog.set_level("WARNING", logger="ors_server.link.ws_daemon")

    for screen_id in range(1000, 1004):
        socket.say(Frame(screen_id=screen_id, seq=1, webp=b"RIFF").model_dump_json())
        await socket.handled()
    # Standing in for the window elapsing, which a test may not spend seconds on.
    monkeypatch.setattr(ws_daemon, "OWNERSHIP_REFRESH_S", 0.0)
    socket.say(Frame(screen_id=1004, seq=1, webp=b"RIFF").model_dump_json())
    await socket.handled()
    await finish(socket, handler)

    assert [record.dropped for record in caplog.records] == [1, 4]


async def test_a_screen_handed_to_this_daemon_stops_being_refused(tmp_path, monkeypatch):
    """A refusal that never expires is a panel that stays blank until a reconnect.

    Screens move between racks -- it is a field in the interface. The set was
    re-read once on a miss and the id then memoised as refused forever, so the
    daemon that was *given* the screen went on dropping its frames for the life
    of the connection, with a log line blaming it each time. Re-reading on a
    schedule fixes that for free, which is most of why it is a schedule.
    """
    app, daemon_id, token = build(tmp_path)
    with closing(app.state.database.connect()) as connection:
        stranger = int(
            connection.execute(
                "INSERT INTO daemon (name, status, created_at) VALUES ('other', 'paired', 'x')"
            ).lastrowid
        )
    screen_id = add_screen(app, stranger, name="Moving")
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    watching: asyncio.Queue = asyncio.Queue()
    app.state.hub.subscribe_frames(screen_id, watching)

    socket.say(Frame(screen_id=screen_id, seq=1, webp=b"RIFF").model_dump_json())
    await socket.handled()
    assert watching.empty(), "it was another rack's when the first frame arrived"

    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE screen SET daemon_id = ? WHERE id = ?", (daemon_id, screen_id))
    monkeypatch.setattr(ws_daemon, "OWNERSHIP_REFRESH_S", 0.0)
    socket.say(Frame(screen_id=screen_id, seq=2, webp=b"RIFF").model_dump_json())
    frame = await asyncio.wait_for(watching.get(), PROMPTLY)

    assert frame.seq == 2, "the first was another rack's; the second was this one's"
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


async def test_a_rack_with_more_watched_screens_than_a_request_may_name_reconnects(tmp_path):
    """The bound is one rule, and a reconnect is the other place it applies.

    `ws_ui` truncates a set this large and logs; this end built the request
    straight from the watched set, so the same list that a browser survives
    killed a hello. The `ValidationError` is neither `SnapshotError` nor
    `WebSocketDisconnect`, so nothing caught it -- and it was raised *after*
    `hub.register`, with `session` still unassigned, so the `finally` never
    dropped the registration either.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    screens = [
        add_screen(app, daemon_id, name=f"panel {position}", position=position)
        for position in range(1, MAX_WATCHED_SCREENS + 2)
    ]
    for screen_id in screens:
        app.state.hub.subscribe_frames(screen_id, asyncio.Queue())

    socket = FakeSocket(app, hello(key))
    await run(socket)

    requests = [message for message in socket.messages if message["type"] == "frames"]
    assert [len(request["screen_ids"]) for request in requests] == [MAX_WATCHED_SCREENS]
    assert requests[0]["screen_ids"] == sorted(screens[-MAX_WATCHED_SCREENS:])


async def test_a_rack_too_big_to_resume_is_not_left_online_for_ever(tmp_path):
    """What the loop above cost every open tab, which is the worse half.

    The hello dies after `register` on every attempt, so the rack reconnects,
    is killed, and reconnects again -- while `register`/`drop` never see a set
    change and every browser goes on showing a green dot over four panels
    frozen on their last image.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    screens = [
        add_screen(app, daemon_id, name=f"panel {position}", position=position)
        for position in range(1, MAX_WATCHED_SCREENS + 2)
    ]
    for screen_id in screens:
        app.state.hub.subscribe_frames(screen_id, asyncio.Queue())

    socket = FakeSocket(app, hello(key), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    await socket.next_message()
    await socket.handled()

    assert app.state.hub.is_online(daemon_id) is True
    await finish(socket, handler)
    assert app.state.hub.is_online(daemon_id) is False


@pytest.mark.parametrize(
    "stage", ["_record_hello", "_push_for", "_owned_screens", "_resume_frames"]
)
async def test_a_hello_that_fails_leaves_no_rack_online(tmp_path, monkeypatch, stage):
    """Whatever goes wrong, and wherever, a rack that never became a session
    must not be left online.

    `session = await _hello(...)` never completes when `_hello` raises, so the
    handler's `finally` has no connection to drop -- and past `hub.register`
    there is one. The stages before it are here to say that the registration is
    the thing being tested rather than the assertion being vacuous: a hello that
    dies before registering leaves nothing behind either.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    monkeypatch.setattr(ws_daemon, stage, _explode)

    with pytest.raises(RuntimeError):
        await run(FakeSocket(app, hello(key)))

    assert app.state.hub.is_online(daemon_id) is False, (
        "a registration nothing will ever drop is a rack the interface shows"
        " online for the life of the process"
    )


async def test_a_hello_whose_push_fails_leaves_no_rack_online_either(tmp_path, monkeypatch):
    """The one stage between `register` and the session that is not this
    module's own function: the hub send the whole rest of the hello is behind."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    monkeypatch.setattr(app.state.hub, "push_config", _explode)

    with pytest.raises(RuntimeError):
        await run(FakeSocket(app, hello(key)))

    assert app.state.hub.is_online(daemon_id) is False


async def test_a_hello_cancelled_after_registering_leaves_no_rack_online(tmp_path):
    """A shutdown, or any outer deadline ever put around this handler.

    A cancel is one of the ways out of the window between `hub.register` and the
    session existing, and it leaves exactly the same wreckage as an exception:
    the caller's `finally` holds None and drops nothing, so the rack is online
    for the life of the process with no socket behind it.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)

    socket = GatedSocket(app, hello(key), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    await asyncio.wait_for(socket.started.wait(), PROMPTLY)
    assert app.state.hub.is_online(daemon_id) is True

    handler.cancel()
    await asyncio.gather(handler, return_exceptions=True)

    assert app.state.hub.is_online(daemon_id) is False


def _explode(*args, **kwargs):
    raise RuntimeError("something this handler does not expect")


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


async def test_a_superseded_handler_leaves_no_reader_behind(tmp_path):
    """`asyncio.wait` does not cancel the awaitables it was handed.

    So the receive that lost the race is still pending when the handler
    returns: one orphaned read per supersede, holding a socket object, a
    coroutine frame and a place in the loop's ready queue for as long as the
    server runs. Invisible from anywhere else in a test -- the socket is closed
    and the handler is gone -- which is why this counts tasks.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    before = running_tasks()
    socket, handler = await connected(app, key)

    app.state.hub.register(daemon_id, nowhere)
    await asyncio.wait_for(handler, PROMPTLY)

    assert socket.close_code == CLOSE_SUPERSEDED
    assert running_tasks() <= before, "the receive the superseded handler lost the race with"


async def test_cancelling_a_handler_leaves_no_reader_behind_either(tmp_path):
    """The other exit, and the one that happens to every connection at shutdown.

    Cancelling the handler raises inside `asyncio.wait`, which cancels neither
    of the two tasks it was given -- so a server going down orphans one receive
    per connected rack, and so does any outer deadline that ever wraps this.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    before = running_tasks()
    socket, handler = await connected(app, key)

    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    assert running_tasks() <= before


async def test_serve_cleans_up_the_tasks_it_made_without_help_from_its_caller(tmp_path):
    """The waiter, which from outside looks like it cleans itself up.

    It does, today, and by accident: the handler's own `finally` drops the
    connection, and dropping it sets the very event the waiter is blocked on. So
    every test above passes whether or not `_serve` cancels it, and the one line
    that does reads as decoration.

    It is not decoration, it is ownership -- and the way to say so is to run
    `_serve` without the caller whose bookkeeping is covering for it. A second
    exit path in `daemon_socket`, or any other caller, and the cover is gone.
    """
    app, daemon_id, token = build(tmp_path)
    await paired_key(app, token)
    socket = FakeSocket(app, hang_up=False)
    session = _Session(
        daemon_id=daemon_id, connection=app.state.hub.register(daemon_id, nowhere), owned=set()
    )
    before = running_tasks()

    serving = asyncio.create_task(_serve(app.state, socket, session))
    await socket.handled()
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert running_tasks() <= before, "the reader, and the waiter nobody else was going to wake"


async def nowhere(payload: str) -> None:
    """The send of the connection that supersedes one, going nowhere."""


# --- what an unauthenticated socket is allowed to cost -----------------------


async def test_a_socket_that_never_says_hello_does_not_park_a_handler(tmp_path, monkeypatch):
    """Accepting a socket is free; waiting on it forever is not.

    Nothing else ever wakes this handler: the daemon is not obliged to send
    anything after connecting, uvicorn's ping timeout is about a socket that has
    gone away rather than one that is deliberately quiet, and there is no
    credential yet to hold anyone to account with. Forty of these were held open
    against the server with no authentication of any kind.
    """
    monkeypatch.setattr(ws_daemon, "HELLO_TIMEOUT_S", 0.01)
    app, daemon_id, _ = build(tmp_path)
    socket = FakeSocket(app, hang_up=False)

    await run(socket)

    assert (socket.close_code, socket.close_reason) == (CLOSE_SLOW_HELLO, "no hello")
    assert app.state.hub.is_online(daemon_id) is False


def test_a_daemon_speaking_another_protocol_version_is_told_which(tmp_path):
    """`PROTOCOL_VERSION` exists so a server meeting an older daemon can say so.

    A code of its own, because the whole reason the 4000 range is split up is
    that a client has to tell "retry this" from "stop". Skew is neither: it is
    "this build cannot talk to that build", and a daemon that reads it can say
    so on its own console instead of reconnecting forever against a server that
    will never understand it.

    Checked before the credential is looked at, and this asserts as much: a
    message this build cannot interpret is not one to act on, whoever sent it.
    """
    client, daemon_id, token = client_for(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token, protocol_version=PROTOCOL_VERSION + 1))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == CLOSE_PROTOCOL_SKEW
    row = daemon_row(client.app, daemon_id)
    assert row["token_hash"] is not None, "and its token was not spent on the way"
    assert row["status"] == "unpaired"


def test_a_key_is_tried_before_a_pairing_token(tmp_path):
    """Both orders authenticate. Only one of them does so without side effects.

    Two rows, one holding a string as its key and another holding the same
    string as an unspent token. Astronomically improbable and entirely
    constructible, and the schema's new CHECK is about one row rather than two.

    Trying the token first would make an ordinary reconnect -- which the key
    already answers -- spend an unrelated daemon's pairing token as a side
    effect. That daemon is then holding a token the server has forgotten, and
    the only way back is a new one from the interface.
    """
    app, _, _ = build(tmp_path)
    database = app.state.database
    credential = "one-string-two-rows"
    with closing(database.connect()) as connection:
        keyed = int(
            connection.execute(
                "INSERT INTO daemon (name, key_hash, status, created_at)"
                " VALUES ('paired-one', ?, 'paired', 'now')",
                (_fingerprint(credential),),
            ).lastrowid
        )
        tokened = int(
            connection.execute(
                "INSERT INTO daemon (name, token_hash, created_at) VALUES ('unpaired-one', ?, ?)",
                (_fingerprint(credential), "now"),
            ).lastrowid
        )

    assert _authenticate(database, credential) == (keyed, None)

    with closing(database.connect()) as connection:
        still_there = connection.execute(
            "SELECT token_hash FROM daemon WHERE id = ?", (tokened,)
        ).fetchone()["token_hash"]
    assert still_there is not None, "and nobody else's pairing was spent on the way"


async def test_a_daemon_deleted_while_it_is_pairing_is_closed_rather_than_crashed(tmp_path):
    """The row is read twice, and the interface can delete it in between.

    `Paired` is written to the wire between those two reads and is the only
    thing in `_hello` that yields, so the gap is real rather than theoretical.
    The second read then raises `KeyError`, which unhandled leaves the route by
    raising -- on a socket that is a close with no code the daemon can act on,
    and a traceback in the log of a server that was asked an ordinary question.
    """
    app, daemon_id, token = build(tmp_path)

    socket = VanishingSocket(app, hello(token))
    await run(socket)

    assert socket.close_code == CLOSE_UNSERVABLE
    assert [message["type"] for message in socket.messages] == ["paired"]
    assert app.state.hub.is_online(daemon_id) is False, "it never became a connection"


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


# --- what the status panel is told about the link ---------------------------
#
# `daemon_event` was written only by `changes.Change.record`, which is to say
# only by API mutations. Nothing on the link path wrote one at all: a nack was a
# `log.error` and nothing more, and a connect and a disconnect wrote nothing --
# so after a rack refused a snapshot, `GET /api/events` showed the server's own
# entries and `GET /api/daemons` reported the version it had minted beside a
# green dot. The status panel is the only place a person looks.


def events(app, daemon_id: int | None = None) -> list[dict]:
    with closing(app.state.database.connect()) as connection:
        if daemon_id is None:
            rows = connection.execute("SELECT * FROM daemon_event ORDER BY id").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM daemon_event WHERE daemon_id = ? ORDER BY id", (daemon_id,)
            ).fetchall()
    return [dict(row) for row in rows]


def kinds(app, daemon_id: int | None = None) -> list[str]:
    return [event["kind"] for event in events(app, daemon_id)]


async def test_a_refused_snapshot_is_written_where_a_person_will_see_it(tmp_path):
    """The reason travels, because it is the only thing anyone can act on.

    A validation failure that stays in the server's log is a rack that quietly
    ignored an edit: the person who saved it is looking at the interface, and
    every other thing that goes wrong with their edit already reaches them
    there.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)

    socket.say(Nack(config_version=4, reason="ring-gauge: no such template").model_dump_json())
    await finish(socket, handler)

    nack = next(event for event in events(app, daemon_id) if event["kind"] == "nack")
    assert nack["level"] == "error"
    assert "ring-gauge: no such template" in nack["message"]
    assert "4" in nack["message"], "and which push it is about"


async def test_a_rack_connecting_and_going_away_are_both_recorded(tmp_path):
    """A connect with no disconnect beside it says nothing about a rack that is
    flapping, and `daemon.last_seen` is one timestamp that cannot show a
    sequence at all."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket, handler = await connected(app, key)
    await finish(socket, handler)

    assert kinds(app, daemon_id)[-2:] == ["connected", "disconnected"]


async def test_the_connect_event_says_what_the_rack_claims_to_be_running(tmp_path):
    """Which is the fact that decides whether it was pushed to, and the one that
    is otherwise only in a log line."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    socket = FakeSocket(app, hello(key, config_version=9), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    await asyncio.sleep(0)
    await finish(socket, handler)

    said = [event for event in events(app, daemon_id) if event["kind"] == "connected"][-1]
    assert "9" in said["message"]


async def test_a_socket_that_was_never_identified_records_nothing(tmp_path):
    """There is no rack to record it against: the credential matched nothing, and
    an event needs a `daemon_id`. Writing one anyway would let anybody who can
    reach the port fill the table."""
    app, _, _ = build(tmp_path)

    socket = FakeSocket(app, hello("not-a-credential"))
    await run(socket)

    assert kinds(app) == [], "the daemon row here is minted directly, not through the API"


async def test_a_superseded_socket_does_not_report_the_rack_as_gone(tmp_path):
    """The identity guard again, and the reason it has to reach the events too.

    A handler whose socket was replaced still runs its own cleanup, seconds
    later. Recording a disconnect there would put "the link closed" into the
    history of a rack that is online and streaming -- which is the same wrong
    answer `Hub.drop` refuses to give the interface, arriving by another road.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    first, first_handler = await connected(app, key)
    second, second_handler = await connected(app, key)

    # From here, because `paired_key` above is itself a connect and a
    # disconnect: what is being counted is what these two sockets add.
    before = kinds(app, daemon_id).count("disconnected")

    await finish(first, first_handler)
    superseded = kinds(app, daemon_id).count("disconnected")

    await finish(second, second_handler)

    assert superseded == before, "the old socket left, the rack did not"
    assert kinds(app, daemon_id).count("disconnected") == before + 1


async def test_the_link_path_keeps_a_racks_history_to_the_same_ring(tmp_path):
    """A rack in a reconnect loop writes two events a lap, so the bound that
    stops one rack's history swamping the table has to hold here as well."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    for _ in range(6):
        socket, handler = await connected(app, key)
        await finish(socket, handler)

    with closing(app.state.database.connect()) as connection:
        connection.execute(
            "DELETE FROM daemon_event WHERE daemon_id = ? AND id NOT IN"
            " (SELECT id FROM daemon_event WHERE daemon_id = ? ORDER BY id DESC LIMIT 3)",
            (daemon_id, daemon_id),
        )
    assert len(events(app, daemon_id)) == 3, "the same ring the API's own record keeps"


# --- the reconnect that the server used to learn nothing from ---------------


async def test_a_reconnect_with_a_matching_version_still_tells_the_server_what_is_running(
    tmp_path,
):
    """Spec section 8's two sentences, and only one of them was built.

    "Nothing is re-pushed if the versions already match" is `_push_for`
    returning None, and it is right. "Daemons reconnect and re-ack their
    version" is the half that was missing, and without it the skip was total
    silence: `register` had just cleared what this daemon confirmed, and there
    was no push left for an ack to answer -- so after any reconnect, and a wifi
    blip is a reconnect, the server could report the version it had minted and
    nothing about the glass.

    The claim in the hello is still only ever allowed to skip a push. This is
    the same fact arriving in the message the server treats as evidence, which
    is what `ors_daemon.link.LinkClient._reack` sends.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    running = daemon_row(app, daemon_id)["config_version"]

    socket = FakeSocket(app, hello(key, config_version=running), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    socket.say(Ack(config_version=running).model_dump_json())
    await socket.handled()

    assert app.state.hub.acked_version(daemon_id) == running
    assert [message["type"] for message in socket.messages] == [], "and it was not re-pushed"
    await finish(socket, handler)


async def test_the_ack_a_reconnect_carries_is_replaced_by_the_one_a_push_earns(tmp_path):
    """A rack that reconnects claiming an old version and is then pushed to
    reports the new one, because the push really was applied and acked. The
    re-ack is what it was running *before*, which is the honest answer for the
    moment between the two."""
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    add_screen(app, daemon_id)
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET config_version = 9 WHERE id = ?", (daemon_id,))

    socket = FakeSocket(app, hello(key, config_version=4), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    pushed = await socket.next_message()
    assert pushed["type"] == "config"

    socket.say(Ack(config_version=4).model_dump_json())
    await socket.handled()
    assert app.state.hub.acked_version(daemon_id) == 4

    socket.say(Ack(config_version=pushed["version"]).model_dump_json())
    await socket.handled()
    assert app.state.hub.acked_version(daemon_id) == pushed["version"]
    await finish(socket, handler)


async def test_a_rack_that_nacks_is_left_reported_as_running_what_it_re_acked(tmp_path):
    """The failure the whole of this is for, end to end on this socket.

    The rack reconnects running 4, the server has minted 9 and pushes it, the
    rack refuses it. Before, the interface showed a green dot and 9 while the
    panels drew 4, and the person who saved the edit was told nothing at all.
    Now the applied version is 4, the reason is in the rack's history, and the
    two numbers disagree where somebody can see them.
    """
    app, daemon_id, token = build(tmp_path)
    key = await paired_key(app, token)
    add_screen(app, daemon_id)
    with closing(app.state.database.connect()) as connection:
        connection.execute("UPDATE daemon SET config_version = 9 WHERE id = ?", (daemon_id,))

    socket = FakeSocket(app, hello(key, config_version=4), hang_up=False)
    handler = asyncio.create_task(daemon_socket(socket))
    pushed = await socket.next_message()
    socket.say(Ack(config_version=4).model_dump_json())
    socket.say(
        Nack(
            config_version=pushed["version"], reason="ring-gauge: no such template"
        ).model_dump_json()
    )
    await socket.handled()

    assert app.state.hub.acked_version(daemon_id) == 4
    assert daemon_row(app, daemon_id)["config_version"] == pushed["version"]
    assert any(
        "no such template" in event["message"]
        for event in events(app, daemon_id)
        if event["kind"] == "nack"
    )
    await finish(socket, handler)
