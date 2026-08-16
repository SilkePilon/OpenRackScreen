from __future__ import annotations

import asyncio
import logging
from inspect import signature

import pytest
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    MAX_PROBE_HOLD_S,
    MAX_WATCHED_SCREENS,
    Command,
    ConfigPush,
    DetectRequest,
    DetectResult,
    Frame,
    FramesRequest,
    PanelCandidate,
    ProbeRequest,
    ProbeResult,
)
from ors_server.link.hub import (
    REQUEST_TIMEOUT,
    SEND_TIMEOUT,
    Connection,
    DaemonsOnline,
    Hub,
)


class FakeSocket:
    """A daemon socket, minus the socket: the hub only ever holds `send`."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[str | bytes] = []
        self.fails = fails

    async def send(self, payload: str | bytes) -> None:
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(payload)


class GatedSocket:
    """A socket whose `send` really suspends, and finishes when the test says so.

    `FakeSocket.send` reaches no await, so nothing else on the loop can run
    while the hub is inside it -- which is exactly the window the identity
    guards in `drop` and `record_ack` exist for, and a suite built only on
    `FakeSocket` never opens it. This one holds the send open until `release`
    is set, so a test can land a reconnect or a drop in the middle of one.
    """

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fails = False

    async def send(self, payload: str | bytes) -> None:
        self.started.set()
        await self.release.wait()
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(payload)


class Ticker:
    """Counts the loop's trips back to it, so a test can prove it really yielded.

    A concurrency test that never suspends is a sequential test with `async` in
    front of it, and it passes against an implementation with the concurrency
    removed. Entering resets the count, so `ticks` reads as "since here".
    """

    def __init__(self) -> None:
        self.ticks = 0
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Ticker:
        self._task = asyncio.create_task(self._run())
        # Let it reach its first await, then discount that first scheduling.
        await asyncio.sleep(0)
        self.ticks = 0
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0)
            self.ticks += 1


def frame(screen_id: int, seq: int) -> Frame:
    return Frame(screen_id=screen_id, seq=seq, webp=b"x")


async def test_a_registered_daemon_is_online_and_receives_a_command():
    hub, socket = Hub(), FakeSocket()
    hub.register(1, socket.send)

    assert hub.is_online(1) is True
    assert hub.online_ids() == {1}
    await hub.send_command(1, Command(command="identify"))

    assert '"identify"' in socket.sent[0]


async def test_a_config_push_and_a_frames_request_reach_the_daemon():
    hub, socket = Hub(), FakeSocket()
    hub.register(1, socket.send)

    await hub.push_config(1, ConfigPush(version=3, snapshot=DaemonConfig()))
    await hub.request_frames(1, FramesRequest(enabled=True, screen_ids=[7]))

    assert '"version":3' in socket.sent[0].replace(" ", "")
    assert '"screen_ids":[7]' in socket.sent[1].replace(" ", "")


async def test_a_config_push_reports_whether_it_reached_a_socket():
    """`POST /api/daemons/{id}/push` has to say whether anything left the server,
    and `is_online` cannot answer it: a rack with no servable configuration is
    online and is sent nothing, and a rack whose send timed out is dropped only
    after the fact. So the send is what reports."""
    hub, socket = Hub(), FakeSocket()
    hub.register(1, socket.send)

    reached = await hub.push_config(1, ConfigPush(version=3, snapshot=DaemonConfig()))
    missed = await hub.push_config(99, ConfigPush(version=3, snapshot=DaemonConfig()))

    assert reached is True
    assert missed is False
    assert len(socket.sent) == 1


async def test_a_config_push_that_wedges_reports_that_it_did_not_arrive():
    """Dropped rather than raised -- the edit is committed and the next connect
    pushes again -- so the return is the only thing that says so."""
    hub = Hub(send_timeout=0.01)
    hub.register(1, GatedSocket().send)

    reached = await asyncio.wait_for(
        hub.push_config(1, ConfigPush(version=3, snapshot=DaemonConfig())), 1
    )

    assert reached is False


async def test_sending_to_an_offline_daemon_is_not_an_error():
    # An edit made while the Pi is unplugged must save; the daemon picks it up
    # when it reconnects. Raising here would make the API 500 on a normal state.
    hub = Hub()

    await hub.send_command(99, Command(command="reload"))

    # And the attempt must not have invented the daemon it could not reach: a
    # `setdefault`-shaped bug here would report an unplugged rack as online for
    # the rest of the server's life.
    assert hub.is_online(99) is False
    assert hub.online_ids() == set()


async def test_a_dropped_connection_stops_being_online():
    hub, socket = Hub(), FakeSocket()
    connection = hub.register(1, socket.send)

    hub.drop(connection)

    assert hub.is_online(1) is False
    assert hub.online_ids() == set()


async def test_a_second_connection_for_one_daemon_replaces_the_first():
    hub, first, second = Hub(), FakeSocket(), FakeSocket()
    hub.register(1, first.send)
    hub.register(1, second.send)

    await hub.send_command(1, Command(command="reload"))

    assert second.sent and not first.sent, "a reconnect must not leave a stale socket receiving"


async def test_the_replaced_connection_is_told_it_is_finished():
    # The hub holds a `send`, not a socket, so it cannot close the old one. It
    # can say so, and the handler blocked on `receive` is what acts on it.
    hub = Hub()
    superseded = hub.register(1, FakeSocket().send)
    replacement = hub.register(1, FakeSocket().send)

    await asyncio.wait_for(superseded.closed.wait(), 1)
    assert replacement.closed.is_set() is False


async def test_dropping_a_connection_tells_it_it_is_finished():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)

    hub.drop(connection)

    assert connection.closed.is_set() is True


async def test_a_late_drop_from_a_replaced_connection_leaves_the_new_one_online():
    # The order a reconnect really arrives in: the new socket registers before
    # the old handler has noticed its own socket is gone. Its `drop` must not
    # take the live daemon offline on the way out.
    hub, second = Hub(), FakeSocket()
    superseded = hub.register(1, FakeSocket().send)
    hub.register(1, second.send)

    hub.drop(superseded)

    assert hub.is_online(1) is True
    await hub.send_command(1, Command(command="reload"))
    assert second.sent


async def test_a_send_that_fails_takes_the_daemon_offline():
    hub = Hub()
    hub.register(1, FakeSocket(fails=True).send)

    await hub.send_command(1, Command(command="reload"))

    assert hub.is_online(1) is False


async def test_a_send_that_fails_leaves_every_other_daemon_alone():
    hub, healthy = Hub(), FakeSocket()
    hub.register(1, FakeSocket(fails=True).send)
    hub.register(2, healthy.send)

    await hub.send_command(1, Command(command="reload"))

    assert hub.online_ids() == {2}
    await hub.send_command(2, Command(command="reload"))
    assert healthy.sent


async def test_acks_are_recorded_per_daemon():
    hub = Hub()
    one = hub.register(1, FakeSocket().send)
    hub.register(2, FakeSocket().send)
    assert hub.acked_version(1) is None

    hub.record_ack(one, 7)

    assert hub.acked_version(1) == 7
    assert hub.acked_version(2) is None, "one daemon's ack says nothing about another's"


async def test_a_reconnecting_daemon_has_acked_nothing_until_it_says_so():
    # The blank-rack sequence: the Pi restarts with no config, the server never
    # noticed, and a caller that compares versions before pushing sees a match
    # and pushes nothing. A new connection means unknown state.
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.record_ack(connection, 7)
    hub.drop(connection)

    reconnected = hub.register(1, FakeSocket().send)

    assert hub.acked_version(1) is None
    hub.record_ack(reconnected, 8)
    assert hub.acked_version(1) == 8


async def test_a_reconnect_that_overtakes_the_old_drop_still_forgets_the_ack():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.record_ack(connection, 7)

    hub.register(1, FakeSocket().send)

    assert hub.acked_version(1) is None


async def test_a_daemon_that_goes_offline_is_not_still_running_its_last_ack():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.record_ack(connection, 7)

    hub.drop(connection)

    assert hub.acked_version(1) is None


async def test_an_ack_from_a_replaced_connection_is_not_the_new_ones():
    # The stale handler is still a reader, and the message it is holding was
    # read before the daemon went away. Recording it under the id would tell a
    # caller comparing versions that the Pi which just rebooted is running
    # version 7, so nothing is pushed and the rack stays blank.
    hub = Hub()
    superseded = hub.register(1, FakeSocket().send)
    hub.register(1, FakeSocket().send)

    hub.record_ack(superseded, 7)

    assert hub.acked_version(1) is None


async def test_an_ack_from_a_dropped_connection_does_not_bring_it_back():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.drop(connection)

    hub.record_ack(connection, 7)

    assert hub.acked_version(1) is None, "`_acked` must not outlive the connection"
    assert hub.is_online(1) is False


async def test_an_ack_from_the_live_connection_of_another_daemon_is_not_confused():
    hub = Hub()
    one = hub.register(1, FakeSocket().send)
    hub.register(2, FakeSocket().send)

    hub.record_ack(one, 7)

    assert hub.acked_version(2) is None


async def test_a_frame_reaches_every_subscriber_of_that_screen_and_no_other():
    hub = Hub()
    watching: asyncio.Queue[Frame] = asyncio.Queue()
    also_watching: asyncio.Queue[Frame] = asyncio.Queue()
    other: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, watching)
    hub.subscribe_frames(1, also_watching)
    hub.subscribe_frames(2, other)

    await hub.relay_frame(frame(1, seq=1))

    assert (await asyncio.wait_for(watching.get(), 1)).seq == 1
    assert (await asyncio.wait_for(also_watching.get(), 1)).seq == 1
    assert other.empty()


async def test_the_first_subscriber_is_reported_and_the_next_one_is_not():
    hub = Hub()

    assert hub.subscribe_frames(1, asyncio.Queue()) is True, "the first watcher starts the daemon"
    assert hub.subscribe_frames(1, asyncio.Queue()) is False, "the second must not ask again"
    assert hub.subscribe_frames(2, asyncio.Queue()) is True, "a screen at a time, not a hub"


async def test_unsubscribing_stops_delivery_and_reports_the_last_one_out():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, queue)

    assert hub.unsubscribe_frames(1, queue) is True, "the last subscriber leaving is what stops it"
    await hub.relay_frame(frame(1, seq=1))

    assert queue.empty()
    assert hub.watched_screens() == set(), "an unwatched screen must not be remembered forever"


async def test_one_of_two_leaving_keeps_the_frames_coming_for_the_other():
    hub = Hub()
    leaving: asyncio.Queue[Frame] = asyncio.Queue()
    staying: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, leaving)
    hub.subscribe_frames(1, staying)

    assert hub.unsubscribe_frames(1, leaving) is False, "someone is still watching"
    await hub.relay_frame(frame(1, seq=1))

    assert leaving.empty()
    assert staying.qsize() == 1
    assert hub.watched_screens() == {1}


async def test_unsubscribing_from_a_screen_nobody_watches_is_not_an_error():
    # A browser socket closing twice, or closing after the server forgot it.
    assert Hub().unsubscribe_frames(9, asyncio.Queue()) is True


async def test_a_full_subscriber_queue_drops_the_oldest_frame_not_the_newest():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=1)
    hub.subscribe_frames(1, queue)

    await hub.relay_frame(frame(1, seq=1))
    await asyncio.wait_for(hub.relay_frame(frame(1, seq=2)), 1)

    assert queue.qsize() == 1, "a slow browser must not stall the daemon's socket"
    assert queue.get_nowait().seq == 2, "a live view shows the newest frame, never a stale one"


async def test_only_the_oldest_frame_is_evicted_and_the_rest_keep_their_order():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=2)
    hub.subscribe_frames(1, queue)

    for seq in (1, 2, 3):
        await hub.relay_frame(frame(1, seq=seq))

    assert [queue.get_nowait().seq, queue.get_nowait().seq] == [2, 3]


async def test_a_frame_for_an_unwatched_screen_goes_nowhere():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, queue)

    await hub.relay_frame(frame(2, seq=1))

    assert queue.empty()


async def test_a_watcher_arriving_mid_fan_out_does_not_kill_the_daemons_socket():
    # `_watchers` really can grow under this loop: FastAPI runs any `def` route
    # in a threadpool, and a browser subscribing from one lands between two
    # iterations. Iterating the live set raises `RuntimeError: Set changed size
    # during iteration`, which is neither a disconnect nor a validation error,
    # so the socket handler does not catch it -- one subscribe would take the
    # whole rack offline. The subscribing queue stands in for the interleaving.
    hub = Hub()
    late: asyncio.Queue[Frame] = asyncio.Queue()

    class SubscribingQueue(asyncio.Queue):  # type: ignore[type-arg]
        def put_nowait(self, item: Frame) -> None:
            hub.subscribe_frames(1, late)
            super().put_nowait(item)

    watching: asyncio.Queue[Frame] = SubscribingQueue()
    hub.subscribe_frames(1, watching)

    await hub.relay_frame(frame(1, seq=1))

    assert watching.qsize() == 1
    assert late.empty(), "a watcher that arrives mid-frame catches the next one, not this one"


async def test_eviction_stays_atomic_against_a_watcher_reading_the_queue():
    # The claim the eviction rests on: nothing can take the space between the
    # `get_nowait` and the `put_nowait`. A consumer parked in `queue.get()` is
    # the thing that would, so the burst below must reach it as one frame.
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=1)
    hub.subscribe_frames(1, queue)
    received: list[int] = []

    async def watch() -> None:
        while True:
            received.append((await queue.get()).seq)

    consumer = asyncio.create_task(watch())
    try:
        async with Ticker() as ticker:
            await asyncio.sleep(0)  # the consumer is now parked inside `get`
            parked = ticker.ticks

            for seq in (1, 2, 3):
                await hub.relay_frame(frame(1, seq=seq))

            assert ticker.ticks == parked, "relay_frame must not yield mid-eviction"
            assert received == [], "nothing could have been consumed while it did not yield"

            for _ in range(10):
                await asyncio.sleep(0)
                if received:
                    break
            assert ticker.ticks > parked, "the consumer really did suspend and resume"
    finally:
        consumer.cancel()

    assert received == [3], "the watcher sees the newest frame, and the stale ones are gone"


async def test_a_reconnect_landing_while_a_send_is_suspended_keeps_the_new_socket():
    # The window a `FakeSocket` cannot open: the hub is inside `connection.send`
    # when the daemon reconnects, and the send it is inside fails afterwards.
    # That failure belongs to a connection the hub has already replaced.
    hub, first, second = Hub(), GatedSocket(), FakeSocket()
    hub.register(1, first.send)

    async with Ticker() as ticker:
        pending = asyncio.create_task(hub.send_command(1, Command(command="reload")))
        await asyncio.wait_for(first.started.wait(), 1)

        hub.register(1, second.send)
        first.fails = True
        first.release.set()
        await asyncio.wait_for(pending, 1)

        assert ticker.ticks > 0, "the send must really have suspended"

    assert hub.is_online(1) is True
    await hub.send_command(1, Command(command="reload"))
    assert second.sent, "the reconnected daemon must keep receiving"


async def test_a_drop_landing_while_a_send_is_suspended_offlines_it_once():
    hub, socket = Hub(), GatedSocket()
    connection = hub.register(1, socket.send)

    async with Ticker() as ticker:
        pending = asyncio.create_task(hub.send_command(1, Command(command="reload")))
        await asyncio.wait_for(socket.started.wait(), 1)

        hub.drop(connection)
        socket.fails = True
        socket.release.set()
        await asyncio.wait_for(pending, 1)

        assert ticker.ticks > 0, "the send must really have suspended"

    assert hub.is_online(1) is False
    replacement = FakeSocket()
    hub.register(1, replacement.send)
    await hub.send_command(1, Command(command="reload"))
    assert replacement.sent, "a drop that already happened must not follow the daemon back"


async def test_a_send_failing_after_a_drop_and_a_reconnect_spares_the_new_socket():
    # Drop, reconnect, then the in-flight send fails: three events in the order
    # a wifi blip really delivers them, with the failure arriving last.
    hub, first, second = Hub(), GatedSocket(), FakeSocket()
    connection = hub.register(1, first.send)

    async with Ticker() as ticker:
        pending = asyncio.create_task(hub.send_command(1, Command(command="reload")))
        await asyncio.wait_for(first.started.wait(), 1)

        hub.drop(connection)
        hub.register(1, second.send)
        first.fails = True
        first.release.set()
        await asyncio.wait_for(pending, 1)

        assert ticker.ticks > 0, "the send must really have suspended"

    assert hub.is_online(1) is True
    await hub.send_command(1, Command(command="reload"))
    assert second.sent


async def test_a_send_that_never_returns_takes_the_daemon_offline():
    # A Pi that is TCP-alive but not reading blocks the send forever, and every
    # later send to it queues behind that one -- including the push a PATCH is
    # awaiting inside its request handler, with the row already committed.
    hub, socket = Hub(send_timeout=0.01), GatedSocket()
    connection = hub.register(1, socket.send)

    await asyncio.wait_for(hub.send_command(1, Command(command="reload")), 1)

    assert hub.is_online(1) is False
    assert connection.closed.is_set() is True, "the handler is what closes the wedged socket"
    # And the next caller is not made to wait for the same dead socket again.
    await asyncio.wait_for(hub.push_config(1, ConfigPush(version=3, snapshot=DaemonConfig())), 1)
    assert socket.sent == []


async def test_a_wedged_daemon_does_not_take_a_healthy_one_with_it():
    hub, healthy = Hub(send_timeout=0.01), FakeSocket()
    hub.register(1, GatedSocket().send)
    hub.register(2, healthy.send)

    await asyncio.wait_for(hub.send_command(1, Command(command="reload")), 1)

    assert hub.online_ids() == {2}
    await hub.send_command(2, Command(command="reload"))
    assert healthy.sent


async def test_a_connection_carries_the_daemon_it_belongs_to():
    connection = Hub().register(4, FakeSocket().send)

    assert isinstance(connection, Connection)
    assert connection.daemon_id == 4


# --- who is online, for whoever is looking at the interface ------------------


async def test_a_browser_watching_for_racks_is_told_when_one_connects():
    hub = Hub()
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)

    hub.register(1, FakeSocket().send)

    assert (await asyncio.wait_for(watching.get(), 1)) == DaemonsOnline(online=frozenset({1}))


async def test_an_announcement_names_everyone_online_and_not_just_who_changed():
    """The interface paints a list of racks from this, so a message carrying
    only the daemon that moved would leave it having to remember the rest --
    and a tab that connected in between would never learn about the others."""
    hub = Hub()
    hub.register(1, FakeSocket().send)
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)

    hub.register(2, FakeSocket().send)

    assert (await asyncio.wait_for(watching.get(), 1)).online == frozenset({1, 2})


async def test_a_daemon_going_offline_is_announced_too():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)

    hub.drop(connection)

    assert (await asyncio.wait_for(watching.get(), 1)).online == frozenset()


async def test_a_reconnect_of_a_rack_that_is_already_online_announces_nothing():
    """Nothing about the list changed, and a wifi blip is several of these a
    minute against every open tab. The announcement is about the set, not about
    the socket."""
    hub = Hub()
    hub.register(1, FakeSocket().send)
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)

    hub.register(1, FakeSocket().send)

    assert watching.empty()


async def test_a_late_drop_from_a_replaced_connection_announces_nothing():
    """The identity guard's failure, seen from the interface: a superseded
    handler arriving at `drop` seconds after its daemon reconnected would
    otherwise paint the rack as unplugged while it is streaming frames."""
    hub = Hub()
    superseded = hub.register(1, FakeSocket().send)
    hub.register(1, FakeSocket().send)
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)

    hub.drop(superseded)

    assert watching.empty()
    assert hub.online_ids() == {1}


async def test_a_tab_that_stopped_watching_is_not_announced_to():
    hub = Hub()
    watching: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(watching)
    hub.unwatch_daemons(watching)

    hub.register(1, FakeSocket().send)

    assert watching.empty()


async def test_unwatching_something_that_was_never_watching_is_not_an_error():
    # A browser socket whose `finally` runs after an accept that never got
    # as far as registering.
    Hub().unwatch_daemons(asyncio.Queue())


async def test_a_full_announcement_queue_drops_the_oldest_rather_than_blocking():
    """The same bargain frames get, and for a stronger reason: `register` is
    called from inside the daemon socket's hello, so a put that blocked here
    would hold a rack's whole connect on a browser that stopped reading."""
    hub = Hub()
    watching: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
    hub.watch_daemons(watching)

    hub.register(1, FakeSocket().send)
    hub.register(2, FakeSocket().send)

    assert watching.qsize() == 1
    assert watching.get_nowait().online == frozenset({1, 2}), "the newest list wins"


async def test_one_stalled_tab_does_not_cost_another_one_its_announcement():
    hub = Hub()
    stalled: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
    reading: asyncio.Queue[object] = asyncio.Queue()
    hub.watch_daemons(stalled)
    hub.watch_daemons(reading)
    hub.register(1, FakeSocket().send)

    hub.register(2, FakeSocket().send)

    assert [event.online for event in (reading.get_nowait(), reading.get_nowait())] == [
        frozenset({1}),
        frozenset({1, 2}),
    ]


# --- what one rack is asked to stream ---------------------------------------


def watching(hub: Hub, *screen_ids: int) -> None:
    for screen_id in screen_ids:
        hub.subscribe_frames(screen_id, asyncio.Queue())


async def test_a_rack_is_asked_for_every_screen_of_its_that_anybody_watches():
    hub = Hub()
    watching(hub, 1, 2, 3)

    request = hub.frames_for(7, owned={1, 3})

    assert request.enabled is True
    assert request.screen_ids == [1, 3], "screen 2 belongs to another rack"


async def test_a_rack_nobody_is_watching_is_asked_to_stop():
    """`enabled=False` and nothing else: `FrameStream.disable()` clears the lot,
    and there is no per-screen off switch to send instead."""
    hub = Hub()

    request = hub.frames_for(7, owned={1, 2})

    assert (request.enabled, request.screen_ids) == (False, [])


async def test_the_screens_are_named_in_ascending_order_however_they_were_opened():
    """Ascending, not set order, and the ids are chosen so the two differ.

    `list({1, 8})` is `[8, 1]` in CPython, and every fixture that numbers its
    screens from one contiguously hides that -- set iteration happens to be
    ascending for those. This is the same identity-fixture trap the base64
    alphabet fell into.
    """
    hub = Hub()
    watching(hub, 8, 1)

    request = hub.frames_for(7, owned={1, 8})

    assert request.screen_ids == [1, 8]
    assert list({1, 8}) != [1, 8], "a fixture that cannot tell the two apart proves nothing"


async def test_more_watched_screens_than_one_request_may_name_keeps_the_newest():
    """Bounded here, once, for both callers.

    `FramesRequest` refuses a longer list, so building one unchecked raises --
    inside a browser's subscribe, which tears down a tab, and inside a
    reconnecting daemon's hello, which leaves the rack looping and online.
    Truncating is the lesser failure; *which* sixty-four survive is the
    question this answers, and the answer is the most recently opened, because
    the panel somebody just opened is the one they are looking at.
    """
    hub = Hub()
    screens = list(range(1, MAX_WATCHED_SCREENS + 3))
    watching(hub, *screens)

    request = hub.frames_for(7, owned=set(screens))

    assert request.screen_ids == sorted(screens[-MAX_WATCHED_SCREENS:])
    assert len(request.screen_ids) == MAX_WATCHED_SCREENS


async def test_the_screen_that_was_opened_last_survives_the_bound():
    """The failure the ordering exists for: truncating the *lowest* ids drops
    the panel a tab just opened, which is a panel frozen from the moment it
    appears -- with no `daemons` change and no stall event to say so."""
    hub = Hub()
    screens = list(range(1, MAX_WATCHED_SCREENS + 2))
    watching(hub, *screens[1:], screens[0])

    request = hub.frames_for(7, owned=set(screens))

    assert screens[0] in request.screen_ids
    assert screens[1] not in request.screen_ids, "the oldest subscription is the casualty"


async def test_the_screens_that_lost_the_bound_are_named_in_the_log(caplog):
    """A count and a limit say a panel went quiet; they do not say which one.

    Opened here oldest-last-but-one, so that the order they were subscribed in
    and the order they are logged in differ: a line whose ids are in whatever
    order they happened to be dropped in is a line somebody has to sort by hand.
    """
    hub = Hub()
    screens = list(range(1, MAX_WATCHED_SCREENS + 3))
    watching(hub, screens[1], screens[0], *screens[2:])

    with caplog.at_level(logging.ERROR, logger="ors_server.link.hub"):
        hub.frames_for(7, owned=set(screens))

    assert [record.daemon for record in caplog.records] == [7]
    assert caplog.records[0].dropped == screens[:2]
    assert caplog.records[0].watched == len(screens)
    assert caplog.records[0].allowed == MAX_WATCHED_SCREENS


async def test_the_bound_counts_one_rack_s_screens_and_not_every_rack_s(caplog):
    hub = Hub()
    mine = list(range(1, MAX_WATCHED_SCREENS + 1))
    theirs = list(range(1000, 1000 + MAX_WATCHED_SCREENS))
    watching(hub, *mine, *theirs)

    with caplog.at_level(logging.ERROR, logger="ors_server.link.hub"):
        request = hub.frames_for(7, owned=set(mine))

    assert request.screen_ids == mine
    assert caplog.records == [], "a rack at the bound is not a rack over it"


# --- asking a rack something, and waiting for the answer ---------------------

# Correlation ids that look like nothing else in this file: not a daemon id, not
# a screen id, not a position in any list, and not each other. The failure they
# are chosen against is the one the whole table exists to prevent -- an answer
# handed to the wrong caller -- and an id that coincides with an index or with
# the rack it was sent to is an id that cannot show it.
DETECTING = "detect-9f3a"
PROBING = "probe-4c7b"
ELSEWHERE = "detect-1d05"
NEVER_ASKED = "probe-0000ffff"

# Two racks, neither of them numbered 1, so that "the first daemon registered"
# and "daemon 1" cannot stand in for each other.
RACK = 7
OTHER_RACK = 12

# bus, cs and the index of this candidate in the list are three different
# numbers, for the reason above.
PANEL = PanelCandidate(bus=1, cs=2, claimed_by="CPU")


def detect(request_id: str = DETECTING) -> DetectRequest:
    return DetectRequest(request_id=request_id)


def probe(request_id: str = PROBING) -> ProbeRequest:
    return ProbeRequest(
        request_id=request_id, bus=1, cs=2, dc=23, rst=24, hz=40_000_000, hold_s=3.0
    )


async def until_sent(socket: FakeSocket, count: int) -> None:
    """Step the loop until the hub has put `count` messages on this socket.

    Steps rather than sleeps: `asyncio.sleep(0)` gives the loop one turn and
    comes straight back, so this costs no wall-clock time and cannot pass
    because a machine happened to be quick. The bound is what makes a failure
    legible -- a request that never leaves stops here, saying so, instead of at
    whichever assertion came next.
    """
    for _ in range(10):
        if len(socket.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {len(socket.sent)} of {count} requests reached the socket")


async def test_a_reply_reaches_the_caller_that_asked_for_it():
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)

    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))

    reply = await asyncio.wait_for(asking, 1)
    assert DETECTING in socket.sent[0], "the question really went to the rack"
    assert isinstance(reply, DetectResult)
    assert reply.request_id == DETECTING
    assert [(panel.bus, panel.cs, panel.claimed_by) for panel in reply.panels] == [(1, 2, "CPU")]


async def test_two_requests_in_flight_do_not_cross():
    """Different request_ids, replies delivered out of order.

    One rack, because that is where the mix-up is invisible: a table keyed by
    the daemon rather than by the question holds one entry for both of these,
    and a reply matched by arrival order hands each caller the other's answer.
    Answering the second one first is what tells those apart from a table that
    works.
    """
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)

    detecting = asyncio.create_task(hub.request(RACK, detect()))
    probing = asyncio.create_task(hub.request(RACK, probe()))
    await until_sent(socket, 2)

    hub.deliver_reply(RACK, ProbeResult(request_id=PROBING, ok=True))
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))

    probed = await asyncio.wait_for(probing, 1)
    detected = await asyncio.wait_for(detecting, 1)
    assert isinstance(probed, ProbeResult) and probed.request_id == PROBING
    assert isinstance(detected, DetectResult) and detected.request_id == DETECTING
    assert detected.panels == [PANEL], "and the answer arrived whole, not just the right shape"


async def test_a_rack_that_never_answers_times_out_rather_than_holding_the_handler():
    """The bound `SEND_TIMEOUT` exists for, one message further on: an HTTP
    handler is parked on this call, and a rack that has stopped answering must
    not be able to hold it open for as long as it likes.

    `timeout=0` because no test here may spend wall-clock time waiting for a
    clock: `asyncio.wait_for` takes its expired-deadline path and raises the
    same `TimeoutError` the full forty seconds would. The one-second deadline
    around it is the real assertion -- a wait that is not bounded at all fails
    there rather than hanging the suite.
    """
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)

    answer = await asyncio.wait_for(hub.request(RACK, probe(), timeout=0), 1)

    assert answer is None
    assert socket.sent, "the question did leave; it is the answer that never came"
    assert hub.is_online(RACK) is True, "a rack that is slow to answer has not gone away"


async def test_a_reply_for_a_request_nobody_is_waiting_on_is_dropped(caplog):
    """A late reply after a timeout must not raise.

    Two ways this arrives: an answer to a question whose caller gave up, and one
    naming an id this server has no record of at all. Both come out of the
    daemon's socket handler, which catches a disconnect and a validation error
    and nothing else -- so a `KeyError` here closes the link and takes the whole
    rack offline over a message it answered correctly.
    """
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)
    assert await asyncio.wait_for(hub.request(RACK, probe(), timeout=0), 1) is None
    # The giving-up warning is the previous test's subject and would be counted
    # here too: `caplog.records` is the whole test, not the block below.
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="ors_server.link.hub"):
        hub.deliver_reply(RACK, ProbeResult(request_id=PROBING, ok=True))
        hub.deliver_reply(RACK, DetectResult(request_id=NEVER_ASKED, panels=[]))

    assert [record.request for record in caplog.records] == [PROBING, NEVER_ASKED]
    assert hub.is_online(RACK) is True, "and the rack is no worse off for having answered"


async def test_one_racks_answer_does_not_resolve_another_racks_question():
    """A reply is matched on the id *and* on the rack it came from.

    The ids are the caller's to mint and this class cannot make them unique --
    which is why `request`'s own cleanup checks identity rather than the key.
    Trusting them to be unique *across* racks as well is that same assumption
    made twice and defended once: a route minting `f"detect-{n}"` per rack, or
    reusing a per-rack counter, has rack 12 answering rack 7's question as a
    matter of routine. The caller then gets a panel list, believes it describes
    rack 7, and the wizard offers an operator wiring for hardware on another Pi.

    The two answers differ in their panels and not only in their rack, so a
    crossed one fails on what it *said* and not merely on where it came from.
    """
    hub, socket, other_socket = Hub(), FakeSocket(), FakeSocket()
    hub.register(RACK, socket.send)
    hub.register(OTHER_RACK, other_socket.send)
    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)

    hub.deliver_reply(OTHER_RACK, DetectResult(request_id=DETECTING, panels=[]))
    await asyncio.sleep(0)

    assert not asking.done(), "another rack's answer is not an answer to this question"
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))
    answer = await asyncio.wait_for(asking, 1)
    assert answer is not None and answer.panels == [PANEL], "and the rack's own answer still lands"


async def test_a_dropped_connection_fails_every_request_in_flight():
    """Otherwise the handler waits out the full timeout for a rack that has gone.

    Every one of them and only that rack's: the other rack below is still
    connected, still able to answer, and its caller has nothing to do with this
    disconnect.
    """
    hub, socket, other_socket = Hub(), FakeSocket(), FakeSocket()
    connection = hub.register(RACK, socket.send)
    hub.register(OTHER_RACK, other_socket.send)
    detecting = asyncio.create_task(hub.request(RACK, detect()))
    probing = asyncio.create_task(hub.request(RACK, probe()))
    elsewhere = asyncio.create_task(hub.request(OTHER_RACK, detect(ELSEWHERE)))
    await until_sent(socket, 2)
    await until_sent(other_socket, 1)

    hub.drop(connection)

    assert await asyncio.wait_for(detecting, 1) is None
    assert await asyncio.wait_for(probing, 1) is None, "every one of them, not the first"
    assert not elsewhere.done(), "another rack's request is not this rack's to fail"
    hub.deliver_reply(OTHER_RACK, DetectResult(request_id=ELSEWHERE, panels=[]))
    answered = await asyncio.wait_for(elsewhere, 1)
    assert answered is not None and answered.request_id == ELSEWHERE


async def test_an_offline_rack_is_refused_immediately():
    """There is nothing to ask, so there is nothing to wait for. Registering the
    wait anyway would spend a whole `REQUEST_TIMEOUT` of an HTTP handler proving
    what `is_online` already knew."""
    hub = Hub()

    async with Ticker() as ticker:
        answer = await hub.request(99, detect())
        assert ticker.ticks == 0, "it must not have suspended at all"

    assert answer is None
    assert hub.is_online(99) is False, "and it must not have invented the rack it could not reach"
    assert hub.online_ids() == set()


async def test_the_wait_outlasts_the_longest_probe_a_rack_may_be_asked_for():
    """The one bound here that is not free to be chosen on its own.

    `ProbeRequest.hold_s` is bounded by `MAX_PROBE_HOLD_S`, so a probe this
    server will happily send can legally keep a panel lit for thirty seconds
    before the daemon has anything to report about it. A wait shorter than that
    is a probe that *always* times out -- reported honestly and wrong every
    time: the panel lights, the operator watches it, and the interface says the
    rack did not answer. Worse, the timeout is indistinguishable from a real
    failure, so the wizard's advice would be to check the wiring that just
    worked.

    Past it by a round trip as well, because the hold is only the part the
    daemon spends deliberately: the request still has to get there and the
    answer still has to come back, and `SEND_TIMEOUT` is this file's own
    statement of how long one leg of that is allowed to take.
    """
    assert REQUEST_TIMEOUT > MAX_PROBE_HOLD_S, "a legal probe outlives its own answer"
    assert REQUEST_TIMEOUT - MAX_PROBE_HOLD_S > SEND_TIMEOUT, "and a round trip past it"
    # The formula and not only the direction. The two lines above are satisfied
    # by any number above thirty-five, so raising this one changes nothing that
    # fails -- and nothing else would either: both callers of `request` pass
    # their own timeout, so the default is a park no test in this suite spends.
    # What it costs is paid by whoever omits the argument next, which is why the
    # parameter's docstring says so and why the number is pinned to what it is
    # built from rather than to a bound it happens to clear.
    assert REQUEST_TIMEOUT == MAX_PROBE_HOLD_S + 2 * SEND_TIMEOUT
    # The pin is the two lines above: they compare symbols from two packages, so
    # a schema that raises the hold fails here rather than in front of an
    # operator. The line below is not that and should not be read as it -- the
    # default is *spelled* `REQUEST_TIMEOUT`, so this compares the constant with
    # itself. What it does catch, which is the whole of its job, is somebody
    # replacing that default with a literal and cutting the relationship loose.
    assert signature(Hub.request).parameters["timeout"].default == REQUEST_TIMEOUT, (
        "and that is what a caller who names no timeout gets"
    )


async def test_a_late_drop_from_a_replaced_connection_leaves_the_live_request_waiting():
    """The identity guard, arriving at the waits.

    A superseded handler runs its own cleanup seconds after the rack
    reconnected, and it reaches `drop` holding a connection the hub has already
    forgotten. Failing that rack's in-flight requests from there refuses a
    question the *live* socket can still answer -- and when the answer does
    land, there is nothing left waiting for it.
    """
    hub, second = Hub(), FakeSocket()
    superseded = hub.register(RACK, FakeSocket().send)
    hub.register(RACK, second.send)
    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(second, 1)

    hub.drop(superseded)
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))

    answer = await asyncio.wait_for(asking, 1)
    assert answer is not None, "a superseded cleanup must not fail the live socket's waits"
    assert answer.request_id == DETECTING


async def test_neither_an_answer_nor_a_timeout_leaves_a_wait_behind():
    """The table is keyed by an id minted per request, so an entry nothing
    removes is never looked at again and never freed: one dead future and one
    dead message per question asked, for the life of the process.

    Read off the table itself, because there is nothing else to observe it by --
    a rack that answers and a rack that does not look exactly the same from
    outside whether the entry was cleaned up or not. The alternative is an
    accessor no production code would ever call, which is the defect this
    project's last review found rather than a way of avoiding one.
    """
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)

    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))
    await asyncio.wait_for(asking, 1)

    assert hub._pending == {}, "an answered question is finished with"
    assert await asyncio.wait_for(hub.request(RACK, probe(), timeout=0), 1) is None
    assert hub._pending == {}, "and so is one that nobody answered"


async def test_a_caller_that_gives_up_leaves_no_wait_behind():
    """A shutdown, or a browser that closed the tab: the task holding this call
    is cancelled while it is inside the wait. A cleanup written as
    `except TimeoutError` misses that entirely -- a cancel carries no error of
    its own -- which is why it is a `finally`."""
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)
    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)

    asking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asking

    assert hub._pending == {}
    # And the answer that arrives for it anyway is dropped rather than raising.
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))


async def test_a_request_that_never_left_the_server_is_not_waited_on():
    """`_send` already says whether anything left, and a socket that failed is
    not a rack that is thinking about it. Waiting the full timeout for an answer
    to a question that was never asked is the whole of what this avoids."""
    hub = Hub()
    hub.register(RACK, FakeSocket(fails=True).send)

    answer = await asyncio.wait_for(hub.request(RACK, detect()), 1)

    assert answer is None
    assert hub.is_online(RACK) is False, "the send that failed took the socket with it"
    assert hub._pending == {}


async def test_a_reply_that_beats_the_send_back_into_the_server_is_not_missed():
    """The wait is registered before the question goes out, not after it.

    `_send` awaits, and the socket is not the wire: the bytes can be on the Pi,
    and the Pi's answer can be back and read by the daemon socket's handler,
    while this server is still inside its own `send` waiting for flow control to
    let go. A wait registered afterwards misses exactly that reply -- so the
    rack that answered fastest would be the one reported as not answering.
    """
    hub, socket = Hub(), GatedSocket()
    hub.register(RACK, socket.send)

    asking = asyncio.create_task(hub.request(RACK, detect()))
    await asyncio.wait_for(socket.started.wait(), 1)
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))
    socket.release.set()

    answer = await asyncio.wait_for(asking, 1)
    assert answer is not None and answer.request_id == DETECTING


async def test_an_answer_arriving_after_the_rack_was_dropped_does_not_raise():
    """`drop` resolves the wait; the caller has not woken up yet to remove it.

    Between those two moments the entry is still in the table with a future that
    is already finished, and another socket's handler is free to run in between
    and deliver something for it. Resolving it twice is an `InvalidStateError`
    out of the daemon socket handler -- not a disconnect and not a validation
    error, so nothing catches it and the link closes -- which is why the guard
    is "is anyone still waiting on this", not "is this key present".
    """
    hub, socket = Hub(), FakeSocket()
    connection = hub.register(RACK, socket.send)
    asking = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)

    hub.drop(connection)
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))

    assert await asyncio.wait_for(asking, 1) is None, "the rack had already gone"


async def test_a_reused_request_id_does_not_take_the_live_wait_with_it():
    """The hub does not mint these ids and cannot make them unique. What it can
    do is make a collision cost one question rather than two: the second
    registration replaces the first, and the first must not then delete the
    second's entry on its way out -- which is `drop`'s identity guard, in the
    one other place this class holds something on a caller's behalf."""
    hub, socket = Hub(), FakeSocket()
    hub.register(RACK, socket.send)

    first = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 1)
    second = asyncio.create_task(hub.request(RACK, detect()))
    await until_sent(socket, 2)

    first.cancel()  # the first caller gives up, holding an id it no longer owns
    with pytest.raises(asyncio.CancelledError):
        await first
    hub.deliver_reply(RACK, DetectResult(request_id=DETECTING, panels=[PANEL]))

    answer = await asyncio.wait_for(second, 1)
    assert answer is not None and answer.panels == [PANEL]
