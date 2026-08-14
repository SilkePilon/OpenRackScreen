from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_schema.link import MAX_WATCHED_SCREENS, Frame
from ors_server.app import AppSettings, create_app
from ors_server.link import ws_ui
from ors_server.link.ws_ui import WATCHER_QUEUE, ui_socket
from ors_server.pairing import mint_token
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

DISPLAY = {"backend": "virtual", "out_dir": "/tmp/p"}

PROMPTLY = 5.0
"""A deadline, not a wait: everything below is satisfied by an event, so a
passing run never spends any of it. It exists so that a handler which fails to
notice something fails the test rather than hanging the suite -- the failure
this file cares most about is a socket left parked in `receive`, which is
otherwise indistinguishable from a test runner that stopped.

It covers the `Browser` half of this file only. `TestClient`'s `receive` blocks
a thread on a future with no timeout of its own, so that half's deadline is
`timeout = 10` in the root pytest configuration, which arrives as a signal.
"""

WEBP = b"\xff\xef\xfe\xff\xef\xfe"
"""Six bytes chosen so that the two base64 alphabets disagree about every one.

`base64.b64encode` makes `/+/+/+/+` of this and `urlsafe_b64encode` makes
`_-_-_-_-`, so a test that decodes it can tell which alphabet went on the wire.
A payload of `RIFF` -- or of any ASCII -- cannot, which is how the alphabet got
as far as being a browser's problem rather than a test's.
"""


# --- building a server with racks and panels in it --------------------------


def build(tmp_path):
    return create_app(AppSettings(data_dir=tmp_path))


def rack(app, name: str = "pi-rack", screens: int = 1) -> tuple[int, list[int]]:
    """A daemon row and some screens on it. Returns the ids, which is what the
    browser socket addresses everything by."""
    daemon_id, _ = mint_token(app.state.database, name)
    ids: list[int] = []
    with closing(app.state.database.connect()) as connection:
        for position in range(1, screens + 1):
            cursor = connection.execute(
                "INSERT INTO screen (daemon_id, position, name, display, template, params)"
                " VALUES (?, ?, ?, ?, 'ring-gauge', '{}')",
                (daemon_id, position, f"panel {position}", json.dumps(DISPLAY)),
            )
            ids.append(int(cursor.lastrowid))
    return daemon_id, ids


class Rack:
    """A daemon's end of the hub, with no daemon: the hub only holds a `send`.

    What a test needs from a rack here is the `frames` requests it was given,
    and running a whole `/ws/daemon` conversation to read them would make every
    assertion below depend on the other socket's hello.
    """

    def __init__(self, app, daemon_id: int) -> None:
        self.sent: list[str] = []
        self.arrived = asyncio.Event()
        self.connection = app.state.hub.register(daemon_id, self.send)

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload if isinstance(payload, str) else payload.decode())
        self.arrived.set()

    @property
    def requests(self) -> list[dict]:
        return [message for message in map(json.loads, self.sent) if message["type"] == "frames"]

    @property
    def asked_for(self) -> tuple[bool, list[int]]:
        """The last thing this rack was told to stream, as (enabled, screen ids)."""
        last = self.requests[-1]
        return last["enabled"], last["screen_ids"]

    async def told(self, enabled: bool, screen_ids: list[int]) -> None:
        """Wait until the last thing this rack was told is this.

        A browser's subscribe is no longer one request each: a burst of them is
        coalesced into one send an interval later, so `browser.handled()` --
        which says the reader has acted on everything -- no longer says the rack
        has been told. This is the other half of that barrier, and it is an
        event rather than a sleep, so a passing run waits exactly as long as the
        coalescing window and no longer.

        Safe against the send it is waiting for landing between the check and
        the clear: both are synchronous and the sends are on this loop, so
        nothing can run in between.
        """
        while True:
            if self.requests and self.asked_for == (enabled, screen_ids):
                return
            self.arrived.clear()
            await asyncio.wait_for(self.arrived.wait(), PROMPTLY)


class StalledRack(Rack):
    """A rack whose send really suspends, and finishes when the test says so.

    `Rack.send` reaches no await, so nothing else on the loop can run while a
    request is being written to it -- and that window is where a subscribe that
    lands *during* a send has to be accounted for. A suite built only on `Rack`
    never opens it.
    """

    def __init__(self, app, daemon_id: int) -> None:
        super().__init__(app, daemon_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def send(self, payload: str | bytes) -> None:
        self.started.set()
        await self.release.wait()
        await super().send(payload)

    def stall(self) -> None:
        self.started.clear()
        self.release.clear()


class Browser:
    """A tab, minus the browser and minus the server around it.

    The handler is a coroutine taking a WebSocket, so the whole conversation is
    drivable with no port, no threads and no clock. `None` in the inbox is the
    tab going away.

    `TestClient` covers the same route end to end below, but it cannot do this
    much: its `receive` blocks a thread on a future with no timeout, so a
    handler that should have sent something and did not stops the test dead, and
    asserting that *nothing* was sent is not expressible at all.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.accepted = False
        self.sent: list[str] = []
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.idle = asyncio.Event()
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if self.inbox.empty():
            # Asking for a message there is nothing to answer with is the one
            # moment that says everything queued has been read *and acted on*:
            # the reader handles a message fully before it asks for the next.
            self.idle.set()
        raw = await self.inbox.get()
        if raw is None:
            raise WebSocketDisconnect(1006)
        return raw

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)
        self.outbox.put_nowait(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code

    def say(self, **message) -> None:
        self.inbox.put_nowait(json.dumps(message))

    def says(self, raw: str) -> None:
        self.inbox.put_nowait(raw)

    def hang_up(self) -> None:
        self.inbox.put_nowait(None)

    async def handled(self) -> None:
        """Wait until everything said so far has been read and acted on.

        Yielding to the loop a fixed number of times would pass for the wrong
        reason as readily as the right one, and a subscription that is refused
        produces nothing else to wait on -- which is what being refused means.
        """
        self.idle.clear()
        await asyncio.wait_for(self.idle.wait(), PROMPTLY)

    async def next_message(self) -> dict:
        return json.loads(await asyncio.wait_for(self.outbox.get(), PROMPTLY))

    async def next_frame(self) -> dict:
        """The next panel image, skipping anything else on the way.

        Waited for rather than read off `frames`: the hub's put and this
        socket's send are on either side of a queue, so a frame relayed is not a
        frame sent until the writer has had the loop.
        """
        while (message := await self.next_message())["type"] != "frame":
            pass
        return message

    @property
    def messages(self) -> list[dict]:
        return [json.loads(payload) for payload in self.sent]

    @property
    def frames(self) -> list[dict]:
        return [message for message in self.messages if message["type"] == "frame"]


class StalledBrowser(Browser):
    """A tab whose socket has stopped draining, and can be let go again.

    `Browser.send_text` reaches no await, so a suite built only on it never
    opens the window this class exists for: the one in which frames are arriving
    at the hub and nothing is taking them off the queue.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self.release = asyncio.Event()
        self.release.set()

    async def send_text(self, payload: str) -> None:
        await self.release.wait()
        await super().send_text(payload)


async def browsing(app) -> tuple[Browser, asyncio.Task]:
    """A tab past its handshake, with the handler still reading.

    Waiting for the first message is what makes the rest deterministic: by the
    time the online list has been sent, the socket is registered with the hub
    and every later `say` is read in the order it was queued.
    """
    browser = Browser(app)
    handler = asyncio.create_task(ui_socket(browser))
    assert (await browser.next_message())["type"] == "daemons"
    return browser, handler


async def finish(browser: Browser, handler: asyncio.Task) -> None:
    browser.hang_up()
    await asyncio.wait_for(handler, PROMPTLY)


async def relay(app, screen_id: int, seq: int = 1, webp: bytes = WEBP) -> None:
    await app.state.hub.relay_frame(Frame(screen_id=screen_id, seq=seq, webp=webp))


def logged_in(tmp_path) -> tuple[TestClient, object]:
    app = build(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    return client, app


# --- the guard --------------------------------------------------------------


def test_the_socket_refuses_an_unauthenticated_browser(tmp_path):
    """The whole rack is visible down this socket, live. It is not public."""
    app = build(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/setup", json={"password": "pw"})

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect("/ws/ui") as socket:
            socket.receive_text()


def test_a_forged_cookie_is_not_a_session_here_either(tmp_path):
    client, _ = logged_in(tmp_path)
    client.cookies.set("ors_session", "not-a-session")

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect("/ws/ui") as socket:
            socket.receive_text()


def test_logging_out_closes_the_door_behind_the_tab(tmp_path):
    client, _ = logged_in(tmp_path)
    client.post("/api/auth/logout")

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect("/ws/ui") as socket:
            socket.receive_text()


def test_the_admin_gets_in_and_is_told_who_is_online(tmp_path):
    """The half a guard that refuses everyone would still pass above."""
    client, _ = logged_in(tmp_path)

    with client.websocket_connect("/ws/ui") as socket:
        first = json.loads(socket.receive_text())

    assert first == {"type": "daemons", "online": []}


def test_the_socket_is_at_the_root_and_not_under_the_api_prefix(tmp_path):
    """The spec puts it there, and a prefix is not something a client can guess.

    `/api/ws/ui` is what hanging it off the guarded API router would produce,
    which is the shape this is here to refuse.
    """
    client, _ = logged_in(tmp_path)

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect("/api/ws/ui") as socket:
            socket.receive_text()

    with client.websocket_connect("/ws/ui") as socket:
        assert json.loads(socket.receive_text())["type"] == "daemons"


# --- what a browser is told about the racks ---------------------------------


async def test_a_new_socket_is_told_which_racks_are_online(tmp_path):
    app = build(tmp_path)
    first, _ = rack(app, "one")
    second, _ = rack(app, "two")
    Rack(app, first)

    browser, handler = await browsing(app)

    assert browser.messages[0] == {"type": "daemons", "online": [first]}
    assert second not in browser.messages[0]["online"]
    await finish(browser, handler)


async def test_a_rack_coming_online_is_pushed_to_a_tab_that_is_already_open(tmp_path):
    """Pushed rather than polled. A poll is a timer in every tab forever, and it
    buys a list that is wrong for up to one interval -- which is exactly the
    window in which a rack that has just gone offline still looks live."""
    app = build(tmp_path)
    daemon_id, _ = rack(app)
    browser, handler = await browsing(app)

    Rack(app, daemon_id)

    assert (await browser.next_message()) == {"type": "daemons", "online": [daemon_id]}
    await finish(browser, handler)


async def test_a_rack_going_offline_is_pushed_too(tmp_path):
    """The event that lets the interface say a panel has stopped for a reason.

    A stalled panel is otherwise indistinguishable from a still one, and this is
    the cause that covers a rebooted Pi, a pulled cable and a wifi blip alike --
    which no per-frame event could.
    """
    app = build(tmp_path)
    daemon_id, _ = rack(app)
    connected = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    app.state.hub.drop(connected.connection)

    assert (await browser.next_message()) == {"type": "daemons", "online": []}
    await finish(browser, handler)


async def test_the_online_list_is_in_order_when_the_ids_are_not_contiguous(tmp_path):
    """Sorted, and the ids are chosen so that set order and sorted order differ.

    `list({1, 8})` is `[8, 1]` in CPython, and every fixture here that numbers
    its racks from one contiguously hides that: set iteration happens to be
    ascending for those, so `sorted(online)` and `list(online)` agree and the
    sort is unpinned. The order is load-bearing -- it is what lets the interface
    skip a repaint without comparing sets -- so unpinned means a status strip
    that flickers on every connect and disconnect.
    """
    app = build(tmp_path)
    Rack(app, 8)
    Rack(app, 1)

    browser, handler = await browsing(app)

    assert list({1, 8}) != [1, 8], "a fixture that cannot tell the two apart proves nothing"
    assert browser.messages[0] == {"type": "daemons", "online": [1, 8]}
    await finish(browser, handler)


async def test_a_pushed_online_list_is_in_order_too(tmp_path):
    """The first message and every later one go through the same function, and
    a test of only the first would leave the pushed ones to a mutant."""
    app = build(tmp_path)
    Rack(app, 8)
    browser, handler = await browsing(app)

    Rack(app, 1)

    assert (await browser.next_message()) == {"type": "daemons", "online": [1, 8]}
    await finish(browser, handler)


async def test_a_closed_tab_stops_being_told_about_racks(tmp_path):
    """Asserted against the hub's own set, because nothing else can see it.

    Removing the `unwatch_daemons` changes nothing a browser could observe --
    the writer is cancelled with the handler, so the queue that goes on being
    filled is one nobody reads. What is left behind is a leak of one queue per
    tab ever opened, for the life of the server, and this is the only assertion
    that fails on it.
    """
    app = build(tmp_path)
    daemon_id, _ = rack(app)
    browser, handler = await browsing(app)
    await finish(browser, handler)

    Rack(app, daemon_id)

    assert app.state.hub._daemon_watchers == set(), "one dead queue held per tab ever opened"
    assert len(browser.messages) == 1


# --- subscribing, and the rule that makes a rack canvas work ----------------


async def test_subscribing_arms_that_screen_s_daemon(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=1)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    assert pi.asked_for == (True, [screens[0]])
    await finish(browser, handler)


async def test_a_second_panel_is_added_to_the_request_and_not_swapped_for_it(tmp_path):
    """The four-panel rack canvas, which is the primary screen of the interface.

    `FramesRequest` is whole-daemon state: `FrameStream.enable` does
    `self._enabled = set(screen_ids)`, a replace. So a request naming only the
    screen that was just opened stops every other panel on that rack -- the
    drafted "first watcher -> enabled=True, screen_ids=[this]" rule freezes the
    other three the moment a fourth view is opened.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=4)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await pi.told(True, sorted(screens))

    assert pi.asked_for == (True, sorted(screens))
    assert all(screens[0] in request["screen_ids"] for request in pi.requests), (
        "no request ever named only the panel that was just opened"
    )
    await finish(browser, handler)


async def test_closing_one_panel_leaves_the_others_streaming(tmp_path):
    """The other half of the same rule. `enabled=False` on the last watcher of
    one screen stops all four, because there is no per-screen off switch."""
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=4)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)

    browser.say(action="unsubscribe", screen_id=screens[1])
    await pi.told(True, [screens[0], screens[2], screens[3]])

    assert pi.asked_for == (True, [screens[0], screens[2], screens[3]])
    await finish(browser, handler)


async def test_two_tabs_on_different_screens_of_one_rack_both_get_what_they_asked_for(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    left, left_handler = await browsing(app)
    right, right_handler = await browsing(app)

    left.say(action="subscribe", screen_id=screens[0])
    await left.handled()
    right.say(action="subscribe", screen_id=screens[1])
    await right.handled()
    assert pi.asked_for == (True, sorted(screens))

    await relay(app, screens[0], seq=1)
    await relay(app, screens[1], seq=2)

    assert (await left.next_frame())["screen_id"] == screens[0]
    assert (await right.next_frame())["screen_id"] == screens[1]
    assert [frame["screen_id"] for frame in left.frames] == [screens[0]]
    assert [frame["screen_id"] for frame in right.frames] == [screens[1]]
    await finish(left, left_handler)
    await finish(right, right_handler)


async def test_one_tab_closing_leaves_the_other_one_s_screen_armed(tmp_path):
    """Two tabs, two screens, one rack: the case the corrected rule exists for.

    The closing tab's own set is empty afterwards, so a rule that sent what
    *this connection* was watching would send `enabled=False` and stop the other
    tab's panel as well.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    leaving, leaving_handler = await browsing(app)
    staying, staying_handler = await browsing(app)
    leaving.say(action="subscribe", screen_id=screens[0])
    await leaving.handled()
    staying.say(action="subscribe", screen_id=screens[1])
    await staying.handled()

    await finish(leaving, leaving_handler)

    assert pi.asked_for == (True, [screens[1]])
    await relay(app, screens[1], seq=9)
    assert (await staying.next_frame())["seq"] == 9
    await finish(staying, staying_handler)


async def test_two_tabs_watching_one_screen_both_see_it(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    first, first_handler = await browsing(app)
    second, second_handler = await browsing(app)
    for browser in (first, second):
        browser.say(action="subscribe", screen_id=screens[0])
        await browser.handled()

    await relay(app, screens[0], seq=4)

    assert (await first.next_frame())["seq"] == 4
    assert (await second.next_frame())["seq"] == 4
    await finish(first, first_handler)
    await finish(second, second_handler)


async def test_a_second_tab_on_the_same_screen_still_re_states_the_whole_set(tmp_path):
    """The hub answers "were you the first?" and this module deliberately does
    not ask, which is a decision no other test here can tell apart -- a second
    watcher of an already-watched screen changes the set not at all, so skipping
    the request sends a rack exactly what it already had.

    Pinned because the alternative is a memory of what each rack was last told,
    and that is state which goes wrong precisely when a daemon reconnects having
    forgotten everything. Restating costs a few dozen bytes and cannot.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    first, first_handler = await browsing(app)
    second, second_handler = await browsing(app)

    for browser in (first, second):
        browser.say(action="subscribe", screen_id=screens[0])
        await browser.handled()

    assert [request["screen_ids"] for request in pi.requests] == [screens, screens]
    await finish(first, first_handler)
    await finish(second, second_handler)


async def test_one_of_two_tabs_on_the_same_screen_leaving_does_not_stop_it(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    leaving, leaving_handler = await browsing(app)
    staying, staying_handler = await browsing(app)
    for browser in (leaving, staying):
        browser.say(action="subscribe", screen_id=screens[0])
        await browser.handled()

    leaving.say(action="unsubscribe", screen_id=screens[0])
    await leaving.handled()

    assert pi.asked_for == (True, [screens[0]]), "someone is still watching that panel"
    await relay(app, screens[0], seq=5)
    assert (await staying.next_frame())["seq"] == 5
    assert not leaving.frames, "the tab that let go must not still be being drawn to"
    await finish(leaving, leaving_handler)
    await finish(staying, staying_handler)


async def test_the_last_watcher_leaving_turns_the_rack_off(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    browser.say(action="unsubscribe", screen_id=screens[0])
    await pi.told(False, [])

    assert pi.asked_for == (False, []), "a rack nobody watches must stop encoding"
    assert app.state.hub.watched_screens() == set()
    await finish(browser, handler)


async def test_subscribing_to_one_rack_leaves_another_alone(tmp_path):
    """`watched_screens()` is global -- every screen anyone is watching, across
    every rack -- so a request assembled from it without a per-daemon partition
    asks a Pi to render screen ids belonging to a different one."""
    app = build(tmp_path)
    mine, my_screens = rack(app, "mine", screens=1)
    theirs, their_screens = rack(app, "theirs", screens=1)
    my_pi, their_pi = Rack(app, mine), Rack(app, theirs)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=my_screens[0])
    await browser.handled()

    assert my_pi.asked_for == (True, my_screens)
    assert their_pi.requests == [], "the other rack was never asked for anything"
    assert their_screens[0] not in my_pi.asked_for[1]
    await finish(browser, handler)


async def test_each_rack_is_armed_with_its_own_screens_only(tmp_path):
    app = build(tmp_path)
    first, first_screens = rack(app, "one", screens=2)
    second, second_screens = rack(app, "two", screens=2)
    first_pi, second_pi = Rack(app, first), Rack(app, second)
    browser, handler = await browsing(app)

    for screen_id in first_screens + second_screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await first_pi.told(True, sorted(first_screens))
    await second_pi.told(True, sorted(second_screens))

    assert first_pi.asked_for == (True, sorted(first_screens))
    assert second_pi.asked_for == (True, sorted(second_screens))
    await finish(browser, handler)


async def test_a_screen_that_does_not_exist_is_not_subscribed_to(tmp_path):
    app = build(tmp_path)
    daemon_id, _ = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=99_999)
    await browser.handled()

    assert app.state.hub.watched_screens() == set()
    assert pi.requests == []
    await finish(browser, handler)


async def test_unsubscribing_from_something_never_subscribed_to_changes_nothing(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    browser.say(action="unsubscribe", screen_id=screens[1])
    await browser.handled()

    assert pi.asked_for == (True, [screens[0]])
    assert len(pi.requests) == 1, "nothing changed, so there was nothing to say"
    await finish(browser, handler)


# --- the closed tab, which is what stops a Pi encoding forever --------------


async def test_a_disconnect_releases_the_subscription(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await browser.handled()

    await finish(browser, handler)

    assert app.state.hub.watched_screens() == set(), (
        "a closed tab must stop the daemon encoding, or it encodes forever"
    )


async def test_a_disconnect_tells_the_rack_to_stop(tmp_path):
    """Releasing the hub entry is half of it. The hub cannot notice a dead
    queue, and the Pi is not listening to the hub -- it is waiting to be told."""
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    await finish(browser, handler)

    assert pi.asked_for == (False, [])


async def test_a_cancelled_handler_still_releases_its_subscriptions(tmp_path):
    """Server shutdown, or any outer deadline ever put around this handler.

    The hub bookkeeping in the `finally` is deliberately all synchronous and
    comes before anything awaited, because a cancellation resumes a `finally` at
    its first await point: an `await` before the release would leave the screen
    watched by a connection that no longer exists, and the hub would go on
    handing frames to a queue nobody reads for the life of the server.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=3)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await browser.handled()

    handler.cancel()
    await asyncio.gather(handler, return_exceptions=True)

    assert app.state.hub.watched_screens() == set()


async def test_a_handler_cancelled_at_every_turn_still_releases_its_subscriptions(tmp_path):
    """One cancel is not the case the ordering exists for, and it took a mutant
    to say so: a `CancelledError` is delivered at the await the task is parked
    on, and the `finally` then runs to completion undisturbed, so an extra
    `await` inside it changes nothing. It is the *second* cancel -- a shutdown
    with a grace period, an `asyncio.wait_for` around this handler -- that
    raises again at the first await in the unwind.

    Cancelling until the task is done delivers one at every await point there
    is, which is the strongest form of the question: does anything survive
    being interrupted wherever it can be? The hub bookkeeping must, because a
    subscription that outlives its socket is a Pi encoding WebP forever.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=3)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await browser.handled()

    while not handler.done():
        handler.cancel()
        await asyncio.sleep(0)

    assert app.state.hub.watched_screens() == set()
    assert app.state.hub._daemon_watchers == set()


async def test_a_handler_that_fails_outright_still_stops_the_rack(tmp_path, monkeypatch):
    """A bug in this module must not cost a Pi its CPU for the rest of the day.

    Every other test here ends the socket the way a browser does. This one ends
    it the way a defect does -- an exception that is not a `WebSocketDisconnect`,
    escaping the reader -- and the obligation is the same: the hub lets go, and
    the rack is told, before the failure goes anywhere.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    monkeypatch.setattr(ws_ui, "_daemon_of", _explode)
    browser.say(action="subscribe", screen_id=screens[1])
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(handler, PROMPTLY)

    assert app.state.hub.watched_screens() == set()
    assert pi.asked_for == (False, []), "a rack must not go on encoding for a handler that died"


def _explode(database, screen_id: int) -> int | None:
    raise RuntimeError("something this module does not expect")


async def test_a_screen_deleted_while_a_tab_had_it_open_still_stops_its_rack(tmp_path):
    """Which rack a closing tab has to tell used to be a row lookup, and the row
    can be gone: a screen deleted from the interface while somebody had its
    panel open answered nothing, so that rack was never told to stop and went on
    encoding WebP for a tab that had closed.

    The rack each screen belonged to is remembered when it is subscribed to
    instead, which is what makes the whole release synchronous as well.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await pi.told(True, screens)

    with closing(app.state.database.connect()) as connection:
        connection.execute("DELETE FROM screen WHERE id = ?", (screens[0],))
    await finish(browser, handler)

    assert pi.asked_for == (False, [])


async def test_a_disconnect_mid_stream_does_not_take_another_tab_with_it(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    leaving, leaving_handler = await browsing(app)
    staying, staying_handler = await browsing(app)
    for browser in (leaving, staying):
        browser.say(action="subscribe", screen_id=screens[0])
        await browser.handled()

    await finish(leaving, leaving_handler)
    await relay(app, screens[0], seq=11)

    assert (await staying.next_frame())["seq"] == 11
    assert app.state.hub.watched_screens() == {screens[0]}
    await finish(staying, staying_handler)


# --- what a frame looks like on the wire ------------------------------------


async def test_a_frame_carries_the_screen_the_sequence_and_the_image(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    await relay(app, screens[0], seq=7)

    frame = await browser.next_frame()
    assert frame["screen_id"] == screens[0]
    assert frame["seq"] == 7
    assert base64.b64decode(frame["webp"]) == WEBP
    await finish(browser, handler)


async def test_a_frame_is_base64_a_browser_can_actually_decode(tmp_path):
    """`atob` is the decoder every browser has, and it rejects `-` and `_`.

    Pydantic serialises `bytes` with the URL-safe alphabet, so `Frame.model_dump_json()`
    straight onto this socket produces a payload the SPA cannot read -- and a
    test written with `base64.b64decode`, which accepts both alphabets by
    default, passes while the page shows nothing. This one decodes with
    `validate=True`, which refuses anything outside the standard alphabet.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    await relay(app, screens[0], seq=1)

    payload = (await browser.next_frame())["webp"]
    assert base64.b64decode(payload, validate=True) == WEBP
    assert "-" not in payload and "_" not in payload
    await finish(browser, handler)


def test_the_fixture_this_file_uses_can_tell_the_two_alphabets_apart():
    """Without this, every assertion above passes against either alphabet.

    The identity-fixture trap: a payload of ASCII, or of `RIFF`, encodes
    identically under both, so a suite built on one proves nothing about which
    is on the wire.
    """
    assert base64.b64encode(WEBP) != base64.urlsafe_b64encode(WEBP)
    assert set(b"+/") <= set(base64.b64encode(WEBP))
    assert set(b"-_") <= set(base64.urlsafe_b64encode(WEBP))

    with pytest.raises(binascii.Error):
        base64.b64decode(base64.urlsafe_b64encode(WEBP), validate=True)


def test_pydantic_s_own_serialisation_is_the_thing_this_socket_must_not_send():
    """Pinned so that the next person to reach for `model_dump_json()` fails a
    test rather than shipping a blank panel."""
    emitted = json.loads(Frame(screen_id=1, seq=1, webp=WEBP).model_dump_json())["webp"]

    assert emitted == base64.urlsafe_b64encode(WEBP).decode()
    with pytest.raises(binascii.Error):
        base64.b64decode(emitted, validate=True)


async def test_a_frame_whose_length_needs_padding_keeps_it(tmp_path):
    """`WEBP` is six bytes, so it encodes to a multiple of four and no test
    above says anything about padding at all. A real WebP is any length."""
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()
    odd = WEBP + b"\xfe"

    await relay(app, screens[0], seq=1, webp=odd)

    payload = (await browser.next_frame())["webp"]
    assert payload.endswith("=="), "an unpadded payload is not what this claims to send"
    assert base64.b64decode(payload, validate=True) == odd
    await finish(browser, handler)


# --- a slow or broken tab ---------------------------------------------------


async def test_a_tab_that_stopped_reading_never_stalls_the_hub(tmp_path):
    """The hub relays on the daemon socket's thread of control, so a browser
    that can hold one up holds up the whole rack -- every panel of it, and every
    push behind it.

    Two hundred frames are relayed while this tab's send is suspended. That the
    loop finishes at all is the assertion about blocking; what arrives
    afterwards is the assertion about which frames a stalled tab is owed.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    Rack(app, daemon_id)
    browser = StalledBrowser(app)
    handler = asyncio.create_task(ui_socket(browser))
    await browser.next_message()
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()
    browser.release.clear()

    for seq in range(200):
        # Plain `await`, not `wait_for`: `relay_frame` awaits nothing, so this
        # never yields to the loop and the writer cannot drain behind it. A
        # `wait_for` would schedule a task and hand the loop over, which is a
        # test of the writer keeping up rather than of the queue filling.
        await relay(app, screens[0], seq=seq)

    browser.release.set()
    assert (await browser.next_frame())["seq"] == 200 - WATCHER_QUEUE, "the oldest go first"
    while (drained := await browser.next_frame())["seq"] < 199:
        pass
    assert drained["seq"] == 199, "the newest frame wins, never a stale one"
    assert len(browser.frames) == WATCHER_QUEUE
    await finish(browser, handler)


async def test_a_message_that_is_not_json_is_skipped_and_the_socket_lives(tmp_path):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.says("this is not json")
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    assert pi.asked_for == (True, [screens[0]]), "one bad message is not worth the socket"
    await finish(browser, handler)


@pytest.mark.parametrize(
    "message",
    [
        {"action": "explode", "screen_id": 1},
        {"action": "subscribe"},
        {"action": "subscribe", "screen_id": "all"},
        {"action": "subscribe", "screen_id": 1, "extra": True},
        {"screen_id": 1},
    ],
)
async def test_a_message_this_socket_does_not_understand_is_skipped(tmp_path, message):
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.says(json.dumps(message))
    await browser.handled()

    assert app.state.hub.watched_screens() == set()
    assert pi.requests == []
    assert screens == [1], "the ids these messages name are real, so it is the message refused"
    await finish(browser, handler)


async def test_a_boolean_is_not_a_screen_id(tmp_path):
    """`bool` is an `int` in Python, so `{"screen_id": true}` validates as 1 and
    subscribes to whatever screen has row id 1.

    A buggy build of the SPA sending a flag where an id belongs then opens panel
    A and is shown panel B's image, with nothing anywhere saying so.
    `ws_daemon._about` guards exactly this on the other socket.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.says('{"action": "subscribe", "screen_id": true}')
    await browser.handled()

    assert screens == [1], "a boolean that validated would land on this row"
    assert app.state.hub.watched_screens() == set()
    assert pi.requests == []
    await finish(browser, handler)


@pytest.mark.parametrize("screen_id", [2**63, 10**30, -(2**63) - 1, -(10**30), 0, -1])
async def test_a_screen_id_no_row_could_have_is_refused_rather_than_looked_up(tmp_path, screen_id):
    """An id past SQLite's range is not an unknown screen, it is an
    `OverflowError` from inside `sqlite3.execute` -- measured, `Python int too
    large to convert to SQLite INTEGER`. That is not a `ValidationError`, so
    nothing skips it: it leaves the reader, leaves the handler, and takes the
    socket down over a message a browser chose.

    Both ends of the range, because the overflow is about magnitude and not
    about sign: a bound written as `le` alone lets `-10**30` through to exactly
    the same crash. The two ordinary refusals -- `0` and `-1` -- are here to say
    the bound is on what a row id can be rather than on what SQLite can hold.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=screen_id)
    browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    assert pi.asked_for == (True, screens), "the socket has to survive it, not just refuse it"
    assert app.state.hub.watched_screens() == set(screens)
    await finish(browser, handler)


async def test_an_unreadable_message_says_which_field_in_the_log(tmp_path, caplog):
    app = build(tmp_path)
    rack(app)
    browser, handler = await browsing(app)

    with caplog.at_level(logging.WARNING, logger="ors_server.link.ws_ui"):
        browser.says('{"action": "subscribe", "screen_id": "all"}')
        await browser.handled()

    assert any("screen_id" in record.error for record in caplog.records)
    await finish(browser, handler)


# --- the bound on one request -----------------------------------------------


async def test_a_rack_with_more_watched_screens_than_one_request_may_name(tmp_path):
    """Truncated and logged rather than raised. `FramesRequest` refuses a list
    over the bound, so building one unchecked turns a large rack into a
    `ValidationError` inside a subscribe -- which freezes every panel on it.
    Streaming sixty-four of them is the lesser failure, and it is loud.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=MAX_WATCHED_SCREENS + 2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    for screen_id in screens:
        browser.say(action="subscribe", screen_id=screen_id)
    await pi.told(True, sorted(screens[-MAX_WATCHED_SCREENS:]))

    enabled, asked = pi.asked_for
    assert enabled is True
    assert asked == sorted(screens[-MAX_WATCHED_SCREENS:]), "the newest subscriptions survive"
    assert len(asked) == MAX_WATCHED_SCREENS
    await finish(browser, handler)


async def test_the_panel_a_tab_just_opened_is_not_the_one_that_is_dropped(tmp_path):
    """Truncating the lowest ids keeps the oldest-created panels, so the screen
    somebody has just opened is exactly the one left out -- a panel frozen from
    the moment it appears, with no `daemons` change and no stall event to say
    so, on a rack the interface still shows as online.

    Opened here in an order that has nothing to do with the ids, which is the
    only way to tell "the newest subscription survives" from "the highest id
    survives".
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=MAX_WATCHED_SCREENS + 1)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    opened_last, opened_first = screens[0], screens[1]

    for screen_id in screens[1:] + screens[:1]:
        browser.say(action="subscribe", screen_id=screen_id)
    await pi.told(True, sorted(set(screens) - {opened_first}))

    assert opened_last in pi.asked_for[1], "the panel the tab just opened went quiet"
    assert opened_first not in pi.asked_for[1], "the oldest subscription is the casualty"
    await finish(browser, handler)


async def test_the_rack_that_is_too_big_says_which_panels_went_quiet(tmp_path, caplog):
    """A count and a limit describe every rack over the bound identically.
    Whoever is reading this is trying to find out why one panel is blank."""
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=MAX_WATCHED_SCREENS + 2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    with caplog.at_level(logging.ERROR, logger="ors_server.link.hub"):
        for screen_id in screens:
            browser.say(action="subscribe", screen_id=screen_id)
        await pi.told(True, sorted(screens[-MAX_WATCHED_SCREENS:]))

    assert [record.daemon for record in caplog.records] == [daemon_id]
    assert caplog.records[0].dropped == screens[:2]
    await finish(browser, handler)


async def test_a_rack_with_exactly_as_many_screens_as_are_allowed_is_left_alone(tmp_path, caplog):
    """The case that says which side of the bound the truncation sits on, and
    the only one that can: at `MAX + 1` an off-by-one truncates to the same list
    and logs an error nobody reads, so it passes either way."""
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=MAX_WATCHED_SCREENS)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    with caplog.at_level(logging.ERROR, logger="ors_server.link.hub"):
        for screen_id in screens:
            browser.say(action="subscribe", screen_id=screen_id)
        await pi.told(True, sorted(screens))

    assert pi.asked_for == (True, sorted(screens))
    assert caplog.records == [], "a rack at the bound is not a rack over it"
    await finish(browser, handler)


# --- what one tab may make the server write to a rack -----------------------


async def test_a_tab_churning_its_subscriptions_does_not_churn_the_rack(tmp_path):
    """One message decided what a subscription cost the Pi, and nothing bounded
    how many of them a tab could send.

    Four hundred subscribe/unsubscribe messages from one connection produced
    four hundred `frames` requests -- each replacing the daemon's whole
    streaming state under `FrameStream._condition`, and each costing two
    synchronous SQLite reads on the event loop. The interface legitimately
    churns subscriptions as tabs open and close, so the answer is to coalesce
    the recomputation rather than to refuse the messages: what the rack is owed
    is the *last* of these, not all of them.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    for _ in range(200):
        browser.say(action="subscribe", screen_id=screens[0])
        browser.say(action="unsubscribe", screen_id=screens[0])
    await browser.handled()
    await pi.told(False, [])

    assert len(pi.requests) <= 4, "400 messages, and the rack hears about them a few times"
    assert pi.asked_for == (False, []), "coalescing may not lose the last word"
    await finish(browser, handler)


async def test_two_requests_to_one_rack_are_an_interval_apart(tmp_path):
    """What bounds the rate, measured rather than asserted about.

    The first request goes immediately -- opening a panel must not wait -- and
    everything the same tab asks for within the window after it is coalesced
    into one send at the end of it.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    started = time.monotonic()
    browser.say(action="subscribe", screen_id=screens[0])
    browser.say(action="subscribe", screen_id=screens[1])
    await pi.told(True, sorted(screens))
    elapsed = time.monotonic() - started

    assert len(pi.requests) == 2
    assert elapsed >= ws_ui.ARM_INTERVAL_S, "the second request was not held at all"
    await finish(browser, handler)


async def test_a_tab_subscribing_twice_to_one_panel_says_so_once(tmp_path):
    """A repeat from the *same* connection changes nothing -- the hub holds one
    queue per connection, so the second `subscribe_frames` is a no-op -- and a
    request restating a set nobody moved is a write to a rack for nothing.

    Not the same question as two tabs on one screen, which still restates the
    whole set: there the connection really did acquire a subscription.

    The repeats come after the coalescing window has closed, so a request they
    caused would go out at once rather than being deferred into one this
    assertion cannot see. Counting straight after `handled()` proves nothing:
    everything is deferred inside the window, including the messages that should
    have caused nothing at all.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await pi.told(True, screens)
    await asyncio.sleep(ws_ui.ARM_INTERVAL_S * 1.2)

    for _ in range(3):
        browser.say(action="subscribe", screen_id=screens[0])
    await browser.handled()

    assert len(pi.requests) == 1
    assert pi.asked_for == (True, screens)
    await finish(browser, handler)


async def test_a_panel_opened_while_a_request_is_being_written_is_not_lost(tmp_path):
    """The window a send holds open, which is where a coalesced set can vanish.

    The bookkeeping that says "this rack has been told" has to come *before* the
    write and not after it: done afterwards, a subscribe arriving while the
    coalesced request is on the wire is recorded as owed and then immediately
    un-owed by a send that never carried it, and that panel stays blank until
    something unrelated moves the set again.

    The reader is free during that write and only during that write -- when it
    is the reader doing the arming it is not reading -- so the window belongs to
    the coalescing task, and it is the second request here rather than the first.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=3)
    pi = StalledRack(app, daemon_id)
    browser, handler = await browsing(app)
    browser.say(action="subscribe", screen_id=screens[0])
    await pi.told(True, [screens[0]])

    pi.stall()
    browser.say(action="subscribe", screen_id=screens[1])
    await asyncio.wait_for(pi.started.wait(), PROMPTLY)
    browser.say(action="subscribe", screen_id=screens[2])
    await browser.handled()
    pi.release.set()

    await pi.told(True, sorted(screens))
    await finish(browser, handler)


async def test_a_rack_owed_a_request_by_a_tab_that_closes_is_still_told(tmp_path):
    """The panel opened and closed again inside one window, which is the whole
    of what a tab does when somebody clicks the wrong thing.

    The rack was told to stream it and the coalescing window swallowed the
    correction, so on the way out `_let_go` has to name the racks this tab
    *owes* a request as well as the ones it still holds a screen of -- and it no
    longer holds any.
    """
    app = build(tmp_path)
    daemon_id, screens = rack(app)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=screens[0])
    browser.say(action="unsubscribe", screen_id=screens[0])
    await browser.handled()
    await finish(browser, handler)

    assert pi.asked_for == (False, []), "a rack left streaming a panel nobody has open"


async def test_the_window_is_per_rack_and_not_one_clock_for_all_of_them(tmp_path):
    """Two racks armed at different moments owe their coalesced request at
    different moments. One clock for the connection would write to whichever
    rack was armed most recently before its own window was out."""
    app = build(tmp_path)
    first, first_screens = rack(app, "one", screens=2)
    second, second_screens = rack(app, "two", screens=2)
    first_pi, second_pi = Rack(app, first), Rack(app, second)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=first_screens[0])
    await first_pi.told(True, [first_screens[0]])
    await asyncio.sleep(ws_ui.ARM_INTERVAL_S * 0.6)
    browser.say(action="subscribe", screen_id=second_screens[0])
    await second_pi.told(True, [second_screens[0]])
    armed = time.monotonic()

    browser.say(action="subscribe", screen_id=first_screens[1])
    browser.say(action="subscribe", screen_id=second_screens[1])
    await second_pi.told(True, sorted(second_screens))

    assert time.monotonic() - armed >= ws_ui.ARM_INTERVAL_S, (
        "the second rack's window was cut short by the first rack's"
    )
    await finish(browser, handler)


async def test_a_tab_that_closes_before_the_window_is_out_still_stops_the_rack(tmp_path):
    """The coalescing window must not outlive the socket. A rack owed a request
    by a tab that has gone is a rack encoding WebP for nobody, and the delay is
    exactly the moment the tab is most likely to leave -- a panel opened and
    closed again."""
    app = build(tmp_path)
    daemon_id, screens = rack(app, screens=2)
    pi = Rack(app, daemon_id)
    browser, handler = await browsing(app)

    browser.say(action="subscribe", screen_id=screens[0])
    browser.say(action="subscribe", screen_id=screens[1])
    await browser.handled()
    await finish(browser, handler)

    assert pi.asked_for == (False, [])
    assert app.state.hub.watched_screens() == set()


# --- the same route, through a real socket ----------------------------------


def test_subscribing_delivers_frames_for_that_screen(tmp_path):
    """End to end through the actual route, because everything above talks to
    the handler directly and would not notice the router being unwired."""
    with TestClient(build(tmp_path)) as client:
        app = client.app
        client.post("/api/auth/setup", json={"password": "pw"})
        client.post("/api/auth/login", json={"password": "pw"})
        daemon_id, screens = rack(app, screens=1)
        client.portal.call(app.state.hub.register, daemon_id, _nowhere)

        with client.websocket_connect("/ws/ui") as socket:
            assert json.loads(socket.receive_text())["type"] == "daemons"
            socket.send_text(json.dumps({"action": "subscribe", "screen_id": screens[0]}))
            client.portal.call(
                app.state.hub.relay_frame, Frame(screen_id=screens[0], seq=3, webp=WEBP)
            )
            message = json.loads(socket.receive_text())

    assert message["type"] == "frame"
    assert message["screen_id"] == screens[0]
    assert message["seq"] == 3
    assert base64.b64decode(message["webp"], validate=True) == WEBP


def test_a_tab_closing_abruptly_still_releases_the_screen(tmp_path):
    """A closed tab is not a polite goodbye: the socket goes and the handler
    finds out from a failed read. Nothing else stops the Pi encoding."""
    with TestClient(build(tmp_path)) as client:
        app = client.app
        client.post("/api/auth/setup", json={"password": "pw"})
        client.post("/api/auth/login", json={"password": "pw"})
        daemon_id, screens = rack(app)
        client.portal.call(app.state.hub.register, daemon_id, _nowhere)

        with client.websocket_connect("/ws/ui") as socket:
            socket.send_text(json.dumps({"action": "subscribe", "screen_id": screens[0]}))
            client.portal.call(
                app.state.hub.relay_frame, Frame(screen_id=screens[0], seq=1, webp=WEBP)
            )
            while json.loads(socket.receive_text())["type"] != "frame":
                pass
            assert client.portal.call(_watched, app) == {screens[0]}

        assert client.portal.call(_watched, app) == set(), (
            "a closed tab must stop the daemon encoding, or it encodes forever"
        )


async def _nowhere(payload: str | bytes) -> None:
    """A daemon connection whose sends go nowhere, for the `TestClient` half:
    those tests are about the socket, not about what the rack was told."""


async def _watched(app) -> set[int]:
    """`Hub` is event-loop-affine, so even a read goes through the portal."""
    return app.state.hub.watched_screens()
