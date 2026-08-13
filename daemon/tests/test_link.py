from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.link import (
    RECV_TIMEOUT_S,
    LinkClient,
    LinkError,
    LinkSettings,
    load_link_settings,
    websocket_url,
    write_link_settings,
)
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    Ack,
    Command,
    ConfigPush,
    FramesRequest,
    Heartbeat,
    Hello,
    LogLine,
    Nack,
    Paired,
    parse_daemon_message,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""

CONFIG = {
    "version": 1,
    "timezone": "UTC",
    "integrations": [],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "text-only",
            "params": {"big": "hi"},
        }
    ],
}

TIMEOUT = object()
"""Scripted into `inbound` for a `recv` that reaches its deadline saying nothing.

Which is what a healthy but quiet server looks like from in here, and what makes
every test below sleepless: the fake advances the injected clock by exactly the
deadline the client asked for, so a heartbeat interval passes in no time at all.
"""


class FakeSocket:
    """A scripted server: hands out messages, records what the client sends.

    `recv` takes a `timeout` because the real one does -- the client must never
    park in it indefinitely, or a SIGTERM on the rack waits for the server to
    say something -- and honouring that deadline here is what lets these tests
    drive the heartbeat and the stop event without a single `sleep`.
    """

    def __init__(
        self,
        inbound: list[Any] | None = None,
        clock: FakeClock | None = None,
        on_recv: Callable[[FakeSocket], None] | None = None,
    ) -> None:
        self.inbound = list(inbound or [])
        self.sent: list[str] = []
        self.timeouts: list[float | None] = []
        self.recvs = 0
        self.closed = False
        self.url: str | None = None
        self.send_error: Exception | None = None
        self._clock = clock
        self._on_recv = on_recv

    def send(self, payload: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    def recv(self, timeout: float | None = None) -> str:
        self.recvs += 1
        self.timeouts.append(timeout)
        if self._on_recv is not None:
            self._on_recv(self)
        if not self.inbound:
            raise ConnectionError("server went away")
        item = self.inbound.pop(0)
        if item is TIMEOUT:
            if self._clock is not None and timeout:
                self._clock.advance(timeout)
            raise TimeoutError("nothing yet")
        return str(item)

    def close(self) -> None:
        self.closed = True


def make(
    tmp_path: Path,
    inbound: list[Any] | None = None,
    applied: Callable[[DaemonConfig, int], None] | None = None,
    sockets: list[FakeSocket] | None = None,
    clock: FakeClock | None = None,
    settings: LinkSettings | None = None,
    **kwargs: Any,
) -> tuple[LinkClient, FakeSocket]:
    clock = clock or FakeClock(NOW)
    scripted = sockets if sockets is not None else [FakeSocket(inbound, clock)]
    handed: list[FakeSocket] = []

    def connect(url: str) -> FakeSocket:
        socket = scripted[min(len(handed), len(scripted) - 1)]
        socket.url = url
        handed.append(socket)
        return socket

    client = LinkClient(
        settings=settings
        or LinkSettings(
            server_url="http://server:8080",
            cache_path=tmp_path / "cache.json",
            token="tok",
        ),
        settings_path=kwargs.pop("settings_path", tmp_path / "link.json"),
        on_snapshot=applied if applied is not None else (lambda snapshot, version: None),
        stop=kwargs.pop("stop", threading.Event()),
        clock=clock,
        connect_factory=kwargs.pop("connect_factory", connect),
        **kwargs,
    )
    return client, scripted[0]


def push(version: int = 7, name: str = "CPU") -> str:
    snapshot = {**CONFIG, "screens": [{**CONFIG["screens"][0], "name": name}]}  # type: ignore[dict-item]
    return ConfigPush(
        version=version, snapshot=DaemonConfig.model_validate(snapshot)
    ).model_dump_json()


def paired(key: str = "k9", daemon_id: int = 42) -> str:
    return Paired(daemon_id=daemon_id, key=key).model_dump_json()


def said(socket: FakeSocket) -> list[Any]:
    return [parse_daemon_message(raw) for raw in socket.sent]


def only(socket: FakeSocket, kind: type) -> list[Any]:
    return [message for message in said(socket) if isinstance(message, kind)]


# --- hello ------------------------------------------------------------------


def test_the_client_says_hello_with_its_token(tmp_path: Path) -> None:
    client, socket = make(tmp_path, [push()])

    client.tick_once()

    first = said(socket)[0]
    assert isinstance(first, Hello)
    assert first.token == "tok"


def test_a_paired_daemon_presents_its_key_and_never_the_spent_token(tmp_path: Path) -> None:
    """The token is one-time; the server deleted its hash the moment it was spent."""
    settings = LinkSettings(
        server_url="http://server:8080",
        cache_path=tmp_path / "cache.json",
        token="tok",
        key="k9",
    )
    client, socket = make(tmp_path, [push()], settings=settings)

    client.tick_once()

    assert said(socket)[0].token == "k9"


def test_a_daemon_with_no_credential_at_all_does_not_dial(tmp_path: Path) -> None:
    settings = LinkSettings(server_url="http://server:8080", cache_path=tmp_path / "cache.json")
    client, socket = make(tmp_path, [push()], settings=settings)

    client.tick_once()

    assert socket.sent == []
    assert socket.url is None


def test_the_hello_says_it_is_running_nothing_when_it_has_no_config(tmp_path: Path) -> None:
    """None, never 0: 0 is a real version and the one an empty server counts from."""
    client, socket = make(tmp_path, [push()])

    client.tick_once()

    assert said(socket)[0].config_version is None


def test_the_hello_reports_the_version_the_daemon_booted_with(tmp_path: Path) -> None:
    client, socket = make(tmp_path, [push()], config_version=5)

    client.tick_once()

    assert said(socket)[0].config_version == 5


def test_the_hello_reports_the_version_of_the_last_applied_push(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    sockets = [FakeSocket([push(7)], clock), FakeSocket([], clock)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock)

    client.tick_once()
    client.tick_once()

    assert said(sockets[1])[0].config_version == 7


def test_a_refused_push_does_not_change_what_the_daemon_claims_to_run(tmp_path: Path) -> None:
    def explode(snapshot: DaemonConfig, version: int) -> None:
        raise ValueError("template 'nope' is not defined")

    clock = FakeClock(NOW)
    sockets = [FakeSocket([push(9)], clock), FakeSocket([], clock)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock, applied=explode, config_version=5)

    client.tick_once()
    client.tick_once()

    assert said(sockets[1])[0].config_version == 5


def test_the_hello_names_the_protocol_and_this_build(tmp_path: Path) -> None:
    from ors_daemon import __version__

    client, socket = make(tmp_path, [push()])

    client.tick_once()

    hello = said(socket)[0]
    assert hello.daemon_version == __version__
    assert hello.hostname


# --- applying a push --------------------------------------------------------


def test_a_snapshot_is_applied_and_acked(tmp_path: Path) -> None:
    applied: list[tuple[DaemonConfig, int]] = []
    client, socket = make(tmp_path, [push(7)], applied=lambda s, v: applied.append((s, v)))

    client.tick_once()

    assert applied and applied[0][1] == 7
    assert applied[0][0].screens[0].name == "CPU"
    assert [ack.config_version for ack in only(socket, Ack)] == [7]


def test_an_applied_snapshot_is_written_to_the_cache(tmp_path: Path) -> None:
    client, _ = make(tmp_path, [push(7)])

    client.tick_once()

    cached = json.loads((tmp_path / "cache.json").read_text())
    assert cached["version"] == 7
    assert DaemonConfig.model_validate(cached["snapshot"]).screens[0].name == "CPU"


def test_a_cache_that_cannot_be_written_still_leaves_the_push_acked(tmp_path: Path) -> None:
    """The config is running; the cache is only what the next boot starts from.

    Losing the ack instead would have the server re-push on every connect, and a
    push is a teardown and repaint of the whole rack.
    """
    (tmp_path / "wall").write_text("not a directory")
    settings = LinkSettings(
        server_url="http://server:8080",
        cache_path=tmp_path / "wall" / "cache.json",
        token="tok",
    )
    applied: list[int] = []
    client, socket = make(
        tmp_path, [push(7)], settings=settings, applied=lambda s, v: applied.append(v)
    )

    client.tick_once()

    assert applied == [7]
    assert [ack.config_version for ack in only(socket, Ack)] == [7]


# --- the version skip -------------------------------------------------------


def test_a_push_of_the_version_already_running_is_not_applied(tmp_path: Path) -> None:
    """Applying it again is a teardown and repaint of the whole rack.

    Asserted on a list rather than by raising out of the handler: `_config`
    catches everything the apply path throws and turns it into a nack, so a
    handler that raised would report a re-apply as a passing test.
    """
    applied: list[int] = []
    client, socket = make(
        tmp_path, [push(7)], applied=lambda s, v: applied.append(v), config_version=7
    )

    client.tick_once()

    assert applied == []
    assert not only(socket, Nack)


def test_a_push_of_the_version_already_running_is_still_acked(tmp_path: Path) -> None:
    """Or the server never learns, and re-pushes on every connect forever."""
    client, socket = make(tmp_path, [push(7)], config_version=7)

    client.tick_once()

    assert [ack.config_version for ack in only(socket, Ack)] == [7]


def test_a_push_of_a_different_version_is_applied(tmp_path: Path) -> None:
    applied: list[int] = []
    client, _ = make(tmp_path, [push(8)], applied=lambda s, v: applied.append(v), config_version=7)

    client.tick_once()

    assert applied == [8]


# --- pairing ----------------------------------------------------------------


def test_the_key_a_pairing_hands_back_is_persisted(tmp_path: Path) -> None:
    """Without this the daemon holds a spent token and can never reconnect."""
    client, _ = make(tmp_path, [paired("k9", 42)])

    client.tick_once()

    written = json.loads((tmp_path / "link.json").read_text())
    assert written["key"] == "k9"
    assert written["daemon_id"] == 42
    assert written["token"] is None, "the token is spent; keeping it is a dead credential on disk"


def test_the_next_connect_after_a_pairing_presents_the_key(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    sockets = [FakeSocket([paired("k9")], clock), FakeSocket([], clock)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock)

    client.tick_once()
    client.tick_once()

    assert said(sockets[1])[0].token == "k9"


def test_a_pairing_makes_the_daemon_forget_what_it_thought_it_was_running(tmp_path: Path) -> None:
    """The claim is about a server. Pairing is a new one, so the claim is void.

    The server always pushes on the connect that spends a token, precisely
    because a re-imaged Pi's stale cache can name a version the server also
    holds. Skipping that push leaves the rack showing the old rack's config.
    """
    applied: list[int] = []
    client, _ = make(
        tmp_path,
        [paired("k9"), push(3)],
        applied=lambda s, v: applied.append(v),
        config_version=3,
    )

    client.tick_once()

    assert applied == [3]


def test_a_key_that_cannot_be_written_still_pairs_this_session(tmp_path: Path) -> None:
    applied: list[int] = []
    client, _ = make(
        tmp_path,
        [paired("k9"), push(3)],
        applied=lambda s, v: applied.append(v),
        settings_path=tmp_path,  # a directory: every write to it fails
    )

    client.tick_once()

    assert client.settings.key == "k9"
    assert applied == [3], "a link that cannot save its key must still run the rack"


# --- refusals ---------------------------------------------------------------


def test_a_snapshot_the_schema_refuses_is_nacked_with_the_reason(tmp_path: Path) -> None:
    broken = json.dumps(
        {"type": "config", "version": 8, "snapshot": {"version": 1, "screens": [{"nope": 1}]}}
    )

    def explode(snapshot: DaemonConfig, version: int) -> None:
        raise AssertionError("an invalid snapshot must not reach the apply path")

    client, socket = make(tmp_path, [broken], applied=explode)

    client.tick_once()

    nacks = only(socket, Nack)
    assert nacks, "the server is told why, or it pushes the same broken config forever"
    assert nacks[0].config_version == 8
    assert "screens" in nacks[0].reason


def test_a_snapshot_that_the_apply_path_rejects_is_also_nacked(tmp_path: Path) -> None:
    def explode(snapshot: DaemonConfig, version: int) -> None:
        raise ValueError("template 'nope' is not defined")

    client, socket = make(tmp_path, [push(9)], applied=explode)

    client.tick_once()

    assert "nope" in only(socket, Nack)[0].reason
    assert not only(socket, Ack), "a refused snapshot is not also an applied one"


def test_a_refused_snapshot_is_not_cached(tmp_path: Path) -> None:
    def explode(snapshot: DaemonConfig, version: int) -> None:
        raise ValueError("nope")

    client, _ = make(tmp_path, [push(9)], applied=explode)

    client.tick_once()

    assert not (tmp_path / "cache.json").exists()


def test_the_link_survives_a_refusal(tmp_path: Path) -> None:
    applied: list[int] = []

    def sometimes(snapshot: DaemonConfig, version: int) -> None:
        if version == 9:
            raise ValueError("nope")
        applied.append(version)

    client, _ = make(tmp_path, [push(9), push(10)], applied=sometimes)

    client.tick_once()

    assert applied == [10], "one bad push must not cost the rack its link"


def test_a_message_this_build_cannot_read_is_skipped_not_nacked(tmp_path: Path) -> None:
    """A nack answers a config push. Anything else is version skew, not a refusal."""
    applied: list[int] = []
    client, socket = make(
        tmp_path,
        [json.dumps({"type": "teleport", "where": "home"}), push(7)],
        applied=lambda s, v: applied.append(v),
    )

    client.tick_once()

    assert not only(socket, Nack)
    assert applied == [7], "one unreadable message is not worth the whole link"


def test_a_config_push_with_no_version_at_all_is_still_answered(tmp_path: Path) -> None:
    client, socket = make(tmp_path, [json.dumps({"type": "config"})])

    client.tick_once()

    assert only(socket, Nack)[0].config_version == 0


# --- the other messages -----------------------------------------------------


def test_a_command_reaches_its_handler(tmp_path: Path) -> None:
    seen: list[Command] = []
    client, _ = make(
        tmp_path,
        [Command(command="identify", screen_id=3).model_dump_json()],
        on_command=seen.append,
    )

    client.tick_once()

    assert [(c.command, c.screen_id) for c in seen] == [("identify", 3)]


def test_a_frames_request_reaches_its_handler(tmp_path: Path) -> None:
    seen: list[FramesRequest] = []
    client, _ = make(
        tmp_path,
        [FramesRequest(enabled=True, screen_ids=[1, 2]).model_dump_json()],
        on_frames_request=seen.append,
    )

    client.tick_once()

    assert [request.screen_ids for request in seen] == [[1, 2]]


def test_a_message_nobody_is_listening_for_costs_nothing(tmp_path: Path) -> None:
    applied: list[int] = []
    client, _ = make(
        tmp_path,
        [Command(command="reload").model_dump_json(), push(7)],
        applied=lambda s, v: applied.append(v),
    )

    client.tick_once()

    assert applied == [7]


def test_a_handler_that_raises_does_not_cost_the_link(tmp_path: Path) -> None:
    def explode(command: Command) -> None:
        raise RuntimeError("the identify blinker is on fire")

    applied: list[int] = []
    client, _ = make(
        tmp_path,
        [Command(command="identify").model_dump_json(), push(7)],
        applied=lambda s, v: applied.append(v),
        on_command=explode,
    )

    client.tick_once()

    assert applied == [7]


def test_nothing_is_sent_up_a_link_that_is_down(tmp_path: Path) -> None:
    """What task 11 needs: a frame offered while the server is away is dropped."""
    client, socket = make(tmp_path, [])

    assert client.send(LogLine(level="INFO", message="hi")) is False
    assert socket.sent == []


def test_a_message_can_be_sent_up_a_live_link(tmp_path: Path) -> None:
    holder: list[LinkClient] = []

    def on_command(command: Command) -> None:
        holder[0].send(LogLine(level="INFO", message="blinking"))

    client, socket = make(
        tmp_path, [Command(command="identify").model_dump_json()], on_command=on_command
    )
    holder.append(client)

    client.tick_once()

    assert [line.message for line in only(socket, LogLine)] == ["blinking"]


# --- the heartbeat ----------------------------------------------------------


def test_the_heartbeat_interval_is_not_below_the_servers_send_bound(tmp_path: Path) -> None:
    """`ws_daemon.SEND_TIMEOUT` is 5.0s. Beating faster makes that bound decoration."""
    client, _ = make(tmp_path, [])

    assert client.heartbeat >= 5.0


def test_nothing_is_sent_before_the_interval_has_passed(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    quiet = [TIMEOUT] * int(14.0 / RECV_TIMEOUT_S)
    client, socket = make(tmp_path, quiet, clock=clock, heartbeat=15.0)

    client.tick_once()

    assert not only(socket, Heartbeat)


def test_a_heartbeat_goes_up_a_quiet_link_once_the_interval_has_passed(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    quiet = [TIMEOUT] * int(16.0 / RECV_TIMEOUT_S)
    client, socket = make(tmp_path, quiet, clock=clock, heartbeat=15.0)

    client.tick_once()

    beats = only(socket, Heartbeat)
    assert len(beats) == 1
    assert beats[0].uptime_s == 15


def test_the_heartbeat_keeps_its_pace_over_a_long_session(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    quiet = [TIMEOUT] * int(46.0 / RECV_TIMEOUT_S)
    client, socket = make(tmp_path, quiet, clock=clock, heartbeat=15.0)

    client.tick_once()

    assert [beat.uptime_s for beat in only(socket, Heartbeat)] == [15, 30, 45]


def test_a_busy_link_beats_too(tmp_path: Path) -> None:
    """The interval is a promise about the link, not about the quiet parts of it.

    Beating only where `recv` reaches its deadline means a server that says
    anything at all more often than once a second is a server this daemon never
    tells it is alive.
    """
    clock = FakeClock(NOW)
    chatter = [Command(command="wake").model_dump_json() for _ in range(20)]
    socket = FakeSocket(chatter, clock)
    socket._on_recv = lambda _: clock.advance(1.0)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, heartbeat=15.0)

    client.tick_once()

    assert only(socket, Heartbeat)


def test_a_daylight_saving_jump_is_not_fifteen_seconds_of_link(tmp_path: Path) -> None:
    """The clock is timezone-aware, and the interval is elapsed time, not wall time.

    Two readings of one `system_clock` carry the same `tzinfo` object, and
    subtracting those is documented to answer the wall-clock difference -- an
    hour out on the two nights a year the offset moves. `ors_daemon.clock` has
    the same trap written up for the night window.
    """
    clock = FakeClock(datetime(2026, 3, 29, 1, 30, tzinfo=ZoneInfo("Europe/Amsterdam")))
    socket = FakeSocket([TIMEOUT], clock)
    # One hour of elapsed time across the spring-forward jump, which the wall
    # clock records as two: 01:30 CET to 03:30 CEST.
    socket._on_recv = lambda _: clock.advance(3600.0)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, heartbeat=5400.0)

    client.tick_once()

    assert not only(socket, Heartbeat), "an hour passed, not an hour and a half"


def test_a_clock_that_steps_backwards_does_not_stall_the_heartbeat(tmp_path: Path) -> None:
    """An NTP correction must not buy the server hours of silence."""
    clock = FakeClock(NOW)
    socket = FakeSocket([TIMEOUT, TIMEOUT], clock)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, heartbeat=15.0)
    socket._on_recv = lambda _: clock.advance(-3600.0)

    client.tick_once()

    assert only(socket, Heartbeat)


# --- the receive loop and the stop event ------------------------------------


def test_the_receive_loop_never_parks_without_a_deadline(tmp_path: Path) -> None:
    """`recv()` with no timeout only looks at the stop event when a message arrives."""
    clock = FakeClock(NOW)
    client, socket = make(tmp_path, [TIMEOUT, TIMEOUT], clock=clock)

    client.tick_once()

    assert socket.timeouts == [RECV_TIMEOUT_S] * 3, "every recv is bounded, not just the first"


def test_the_stop_event_ends_the_receive_loop_before_the_next_message(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    stop = threading.Event()
    applied: list[int] = []
    socket = FakeSocket([TIMEOUT, TIMEOUT, push(7)], clock)
    socket._on_recv = lambda s: stop.set() if s.recvs == 2 else None
    client, _ = make(
        tmp_path, sockets=[socket], clock=clock, stop=stop, applied=lambda s, v: applied.append(v)
    )

    client.tick_once()

    assert socket.recvs == 2, "a stopped link must not wait for the server to speak"
    assert applied == []
    assert socket.closed is True


def test_a_server_that_goes_away_leaves_the_client_disconnected_not_dead(tmp_path: Path) -> None:
    client, socket = make(tmp_path, [])

    client.tick_once()

    assert client.connected is False
    assert socket.closed is True


def test_a_socket_that_will_not_even_take_the_hello_is_survivable(tmp_path: Path) -> None:
    socket = FakeSocket([push(7)])
    socket.send_error = ConnectionError("reset by peer")
    client, _ = make(tmp_path, sockets=[socket])

    client.tick_once()

    assert client.connected is False
    assert socket.closed is True


def test_a_socket_that_will_not_close_is_survivable(tmp_path: Path) -> None:
    class Stubborn(FakeSocket):
        def close(self) -> None:
            raise OSError("already gone")

    client, _ = make(tmp_path, sockets=[Stubborn([])])

    client.tick_once()

    assert client.connected is False


# --- the backoff ------------------------------------------------------------


def test_the_first_retry_after_a_failure_is_prompt_and_only_then_doubles(tmp_path: Path) -> None:
    """M2's poller doubled on the *first* failure and gave 10/20/30 instead of 5/10/20."""
    client, _ = make(tmp_path, [])

    delays = []
    for _ in range(4):
        client.tick_once()
        delays.append(client.retry_in)

    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_the_backoff_is_capped(tmp_path: Path) -> None:
    client, _ = make(tmp_path, [], backoff_cap=10.0)

    for _ in range(10):
        client.tick_once()

    assert client.retry_in == 10.0


def test_a_connection_that_lasted_earns_a_prompt_reconnect(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    sockets = [FakeSocket([], clock), FakeSocket([TIMEOUT] * 20, clock), FakeSocket([], clock)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock, heartbeat=15.0)

    client.tick_once()
    client.tick_once()
    assert client.retry_in == 1.0, "a session that ran is not evidence of a broken server"

    client.tick_once()
    assert client.retry_in == 1.0, "the first retry after a real session is prompt"


def test_a_link_that_is_accepted_and_dropped_at_once_still_backs_off(tmp_path: Path) -> None:
    """A rejected credential closes the socket the moment it is opened.

    Resetting the delay on `connect` rather than on a session that lasted turns
    that into a reconnect every two seconds, forever, against a server that has
    already said no.
    """
    clock = FakeClock(NOW)
    sockets = [FakeSocket([], clock) for _ in range(4)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock)

    delays = []
    for _ in range(4):
        client.tick_once()
        delays.append(client.retry_in)

    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_run_waits_the_backoff_between_attempts(tmp_path: Path) -> None:
    stop = threading.Event()
    delays: list[float] = []

    def sleeper(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 3:
            stop.set()

    client, _ = make(tmp_path, [], stop=stop, sleeper=sleeper)

    client.run()

    assert delays == [1.0, 2.0, 4.0]


def test_run_does_nothing_at_all_once_stopped(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    client, socket = make(tmp_path, [push(7)], stop=stop)

    client.run()

    assert socket.url is None


def test_the_wait_between_attempts_ends_the_moment_the_stop_event_is_set(tmp_path: Path) -> None:
    """The default wait is on the event, not a sleep: SIGTERM cannot wait a minute."""
    stop = threading.Event()
    socket = FakeSocket([], on_recv=lambda s: stop.set())
    client, _ = make(
        tmp_path, sockets=[socket], stop=stop, backoff_floor=60.0, backoff_cap=60.0, sleeper=None
    )

    client.start()
    client.join(WAIT)

    assert client.is_alive() is False


def test_the_thread_survives_a_connect_that_raises(tmp_path: Path) -> None:
    stop = threading.Event()
    attempts: list[str] = []

    def connect(url: str) -> FakeSocket:
        attempts.append(url)
        raise OSError("no route to host")

    def sleeper(seconds: float) -> None:
        if len(attempts) == 2:
            stop.set()

    client, _ = make(tmp_path, [], stop=stop, connect_factory=connect, sleeper=sleeper)

    client.run()

    assert len(attempts) == 2


# --- the URL ----------------------------------------------------------------


def test_the_client_dials_the_daemon_socket(tmp_path: Path) -> None:
    client, socket = make(tmp_path, [push()])

    client.tick_once()

    assert socket.url == "ws://server:8080/ws/daemon"


def test_an_http_server_is_dialled_over_ws_and_an_https_one_over_wss() -> None:
    assert websocket_url("http://rack:8080") == "ws://rack:8080/ws/daemon"
    assert websocket_url("https://rack.example") == "wss://rack.example/ws/daemon"


def test_a_websocket_url_is_taken_as_it_is() -> None:
    assert websocket_url("ws://rack:8080") == "ws://rack:8080/ws/daemon"
    assert websocket_url("wss://rack.example") == "wss://rack.example/ws/daemon"


def test_a_server_behind_a_path_prefix_keeps_its_prefix() -> None:
    assert websocket_url("https://example/ors/") == "wss://example/ors/ws/daemon"


def test_only_the_scheme_is_rewritten_and_not_the_rest_of_the_url() -> None:
    """Two `str.replace` calls rewrite every occurrence, wherever it appears."""
    assert websocket_url("http://rack/http://x") == "ws://rack/http://x/ws/daemon"


def test_a_url_with_no_scheme_or_no_host_is_refused() -> None:
    # `http:8080` and `https:/rack` are the ones a scheme check alone lets
    # through: both parse with a scheme this module knows and no host at all.
    for bad in ("rack:8080", "ftp://rack", "/ws", "", "http:8080", "https:/rack"):
        with pytest.raises(LinkError):
            websocket_url(bad)


# --- the settings file ------------------------------------------------------


def test_the_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "link.json"
    write_link_settings(
        path,
        LinkSettings(server_url="http://s", cache_path=path, token="t", key="k", daemon_id=4),
    )

    loaded = load_link_settings(path)

    assert loaded is not None
    assert (loaded.server_url, loaded.token, loaded.key, loaded.daemon_id) == (
        "http://s",
        "t",
        "k",
        4,
    )
    assert loaded.cache_path == path


def test_a_settings_file_with_no_cache_path_gets_one_beside_it(tmp_path: Path) -> None:
    path = tmp_path / "link.json"
    path.write_text(json.dumps({"server_url": "http://s", "token": "t"}))

    loaded = load_link_settings(path)

    assert loaded is not None
    assert loaded.cache_path == tmp_path / "snapshot.json"


def test_no_link_settings_means_an_unpaired_daemon(tmp_path: Path) -> None:
    assert load_link_settings(tmp_path / "absent.json") is None


def test_an_unreadable_settings_file_means_an_unpaired_daemon(tmp_path: Path) -> None:
    path = tmp_path / "link.json"
    path.write_text("{ not json")

    assert load_link_settings(path) is None


def test_settings_with_no_credential_are_no_pairing(tmp_path: Path) -> None:
    path = tmp_path / "link.json"
    path.write_text(json.dumps({"server_url": "http://s", "token": None, "key": None}))

    assert load_link_settings(path) is None


def test_settings_with_no_server_are_no_pairing(tmp_path: Path) -> None:
    path = tmp_path / "link.json"
    path.write_text(json.dumps({"token": "t"}))

    assert load_link_settings(path) is None


def test_the_credential_is_the_key_once_there_is_one(tmp_path: Path) -> None:
    settings = LinkSettings(server_url="http://s", cache_path=tmp_path, token="t", key="k")

    assert settings.credential == "k"
    assert LinkSettings(server_url="http://s", cache_path=tmp_path, token="t").credential == "t"
    assert LinkSettings(server_url="http://s", cache_path=tmp_path).credential is None


def test_the_settings_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "link.json"

    write_link_settings(path, LinkSettings(server_url="http://s", cache_path=path, token="t"))

    assert path.stat().st_mode & 0o077 == 0


def test_the_settings_file_is_replaced_and_never_truncated(tmp_path: Path, monkeypatch) -> None:
    """A power cut mid-write must not leave a rack that cannot pair."""
    path = tmp_path / "link.json"
    write_link_settings(path, LinkSettings(server_url="http://s", cache_path=path, token="first"))

    def die(source, target):  # noqa: ANN001
        raise OSError("the power went")

    monkeypatch.setattr(os, "replace", die)
    with pytest.raises(OSError):
        write_link_settings(
            path, LinkSettings(server_url="http://s", cache_path=path, token="second")
        )

    assert load_link_settings(path).token == "first"  # type: ignore[union-attr]
    assert list(tmp_path.glob(".*")) == [], "no temporary file is left behind"


def test_writing_settings_creates_the_directory_they_go_in(tmp_path: Path) -> None:
    path = tmp_path / "etc" / "ors" / "link.json"

    write_link_settings(path, LinkSettings(server_url="http://s", cache_path=path, token="t"))

    assert load_link_settings(path) is not None
