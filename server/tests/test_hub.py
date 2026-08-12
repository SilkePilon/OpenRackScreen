import asyncio

from ors_schema.daemon import DaemonConfig
from ors_schema.link import Command, ConfigPush, Frame, FramesRequest
from ors_server.link.hub import Connection, Hub


class FakeSocket:
    """A daemon socket, minus the socket: the hub only ever holds `send`."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[str | bytes] = []
        self.fails = fails

    async def send(self, payload: str | bytes) -> None:
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(payload)


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
    hub.register(1, FakeSocket().send)
    hub.register(2, FakeSocket().send)
    assert hub.acked_version(1) is None

    hub.record_ack(1, 7)

    assert hub.acked_version(1) == 7
    assert hub.acked_version(2) is None, "one daemon's ack says nothing about another's"


async def test_a_reconnecting_daemon_has_acked_nothing_until_it_says_so():
    # The blank-rack sequence: the Pi restarts with no config, the server never
    # noticed, and a caller that compares versions before pushing sees a match
    # and pushes nothing. A new connection means unknown state.
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.record_ack(1, 7)
    hub.drop(connection)

    hub.register(1, FakeSocket().send)

    assert hub.acked_version(1) is None
    hub.record_ack(1, 8)
    assert hub.acked_version(1) == 8


async def test_a_reconnect_that_overtakes_the_old_drop_still_forgets_the_ack():
    hub = Hub()
    hub.register(1, FakeSocket().send)
    hub.record_ack(1, 7)

    hub.register(1, FakeSocket().send)

    assert hub.acked_version(1) is None


async def test_a_daemon_that_goes_offline_is_not_still_running_its_last_ack():
    hub = Hub()
    connection = hub.register(1, FakeSocket().send)
    hub.record_ack(1, 7)

    hub.drop(connection)

    assert hub.acked_version(1) is None


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


async def test_a_connection_carries_the_daemon_it_belongs_to():
    connection = Hub().register(4, FakeSocket().send)

    assert isinstance(connection, Connection)
    assert connection.daemon_id == 4
