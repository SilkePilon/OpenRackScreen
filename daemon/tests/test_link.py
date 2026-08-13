from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import socket as socket_module
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.link import (
    BACKOFF_CAP_S,
    CLOSE_PROTOCOL_SKEW,
    CLOSE_UNAUTHORIZED,
    MAX_MESSAGE_BYTES,
    RECV_TIMEOUT_S,
    LinkClient,
    LinkClosed,
    LinkError,
    LinkSettings,
    _default_connect,
    _Socket,
    load_link_settings,
    websocket_url,
    write_link_settings,
)
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    PROTOCOL_VERSION,
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

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""RFC 6455's handshake constant, for the deaf peer below."""

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

    An `Exception` in `inbound` is raised rather than returned, which is how a
    close arrives on this transport: the server's 4000-series code reaches the
    client as a `LinkClosed` out of `recv`, not as a message.

    `parks_after` models the one thing `websockets.sync` will not do for itself:
    from that many sends onward, every send parks. Its `Connection.send` holds
    the protocol mutex across a blocking `sendall` with no timeout on it, so a
    server that is TCP-alive and has stopped reading parks the sending thread
    until something else takes the socket away. Here that something else is
    `abort`, and a `send` that is never rescued fails the test rather than
    hanging the suite. It is a count and not a flag because the hello, the acks
    and heartbeats, and task 11's cross-thread frames are three different
    callers of three different code paths, and each has to be bounded on its own.
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
        self.aborted = False
        self.url: str | None = None
        self.send_error: Exception | None = None
        self.parks_after: int | None = None
        self._released = threading.Event()
        self._clock = clock
        self._on_recv = on_recv

    def send(self, payload: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if self.parks_after is not None and len(self.sent) >= self.parks_after:
            if not self._released.wait(WAIT):
                raise AssertionError("a parked send was never rescued")
            raise ConnectionError("the socket was taken out from under this send")
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
        if isinstance(item, Exception):
            raise item
        return str(item)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
        self._released.set()


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


def test_an_unpaired_daemon_backs_off_rather_than_saying_so_once_a_second(
    tmp_path: Path,
) -> None:
    """`run` waits `retry_in` between attempts, and this branch has to set it.

    Without the settle, the delay stays at the floor for as long as the daemon
    is unpaired: `run` ticks once a second, and each tick logs at ERROR. That is
    ~86,400 lines a day onto the SD card of a Pi whose only fault is that nobody
    has paired it yet.
    """
    settings = LinkSettings(server_url="http://server:8080", cache_path=tmp_path / "cache.json")
    client, _ = make(tmp_path, [], settings=settings)

    delays = []
    for _ in range(4):
        client.tick_once()
        delays.append(client.retry_in)

    assert delays == [1.0, 2.0, 4.0, 8.0]


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


def test_a_cache_in_a_directory_that_does_not_exist_yet_is_still_written(
    tmp_path: Path,
) -> None:
    """`/var/lib/ors/` on a freshly imaged Pi is a directory nothing has made yet.

    Without the `mkdir`, the first push is never cached and the next boot has
    nothing to boot from -- so the rack comes up on its local config file, or on
    nothing, while the server believes it acked.
    """
    settings = LinkSettings(
        server_url="http://server:8080",
        cache_path=tmp_path / "var" / "lib" / "ors" / "cache.json",
        token="tok",
    )
    client, _ = make(tmp_path, [push(7)], settings=settings)

    client.tick_once()

    assert json.loads(settings.cache_path.read_text())["version"] == 7


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

    A second push follows the skipped one so that the assertion cannot pass
    vacuously. `applied == []` is also what a client that received nothing at
    all produces -- a `recv` never reached, a message never parsed -- so on its
    own it says nothing about the skip. `applied == [8]` says the loop ran, read
    both, and declined exactly one.
    """
    applied: list[int] = []
    client, socket = make(
        tmp_path, [push(7), push(8)], applied=lambda s, v: applied.append(v), config_version=7
    )

    client.tick_once()

    assert applied == [8]
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


def test_a_settings_path_with_no_filename_is_reported_rather_than_unwound(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`write_link_settings` derives its temporary with `Path.with_name`.

    That raises `ValueError`, not `OSError`, for a path with no filename -- the
    same reason `_write_cache` catches both. Caught only as `OSError`, the
    failure unwinds past the one log line that says the daemon is a reboot away
    from permanent lockout, takes the link down, and drops the `ConfigPush` that
    follows `Paired` with nothing but an INFO line to show for it.
    """
    applied: list[int] = []
    with caplog.at_level(logging.ERROR):
        client, _ = make(
            tmp_path,
            [paired("k9"), push(3)],
            applied=lambda s, v: applied.append(v),
            settings_path=Path("/"),  # `Path("/").with_name(...)` raises ValueError
        )

        client.tick_once()

    assert any("reconnects with" in record.message for record in caplog.records), (
        "the one failure that locks a rack out of its server must say so"
    )
    assert applied == [3], "and the push that follows the pairing must still be applied"


def test_a_failure_nobody_anticipated_when_saving_the_key_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch is two named exceptions, not `Exception`, and that is the point.

    `OSError` and `ValueError` are everything this write is known to do, and
    each has an answer: keep the session, say so loudly. Anything else is a bug
    rather than a disk, and swallowing it would leave the link running on a
    guess. It ends the connection instead, which costs one backoff and reconnects
    on the key already in memory.
    """
    from ors_daemon import link as link_module

    def die(path: Path, settings: LinkSettings) -> None:
        raise RuntimeError("something nobody wrote a branch for")

    monkeypatch.setattr(link_module, "write_link_settings", die)
    applied: list[int] = []
    client, socket = make(tmp_path, [paired("k9"), push(3)], applied=lambda s, v: applied.append(v))

    client.tick_once()

    assert applied == [], "an unexpected failure ends the connection rather than being logged"
    assert socket.closed is True
    assert client.connected is False


def test_a_second_pairing_never_overwrites_a_key_this_daemon_already_has(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing on this socket authenticates the server. The URL is what was typed.

    A healthy server never sends `Paired` to a daemon it has already keyed, so
    one arriving is either a wrong URL or somebody answering on that address --
    and writing their key over the real one on *disk* locks the rack out of its
    real server permanently, with no recovery short of minting a new token.
    """
    settings = LinkSettings(
        server_url="http://server:8080",
        cache_path=tmp_path / "cache.json",
        key="real",
        daemon_id=7,
    )
    written = tmp_path / "link.json"
    write_link_settings(written, settings)
    applied: list[int] = []
    with caplog.at_level(logging.ERROR):
        client, _ = make(
            tmp_path,
            [paired("stolen", 99), push(3)],
            settings=settings,
            applied=lambda s, v: applied.append(v),
        )

        client.tick_once()

    assert client.settings.key == "real"
    assert json.loads(written.read_text())["key"] == "real"
    assert any("already" in record.message for record in caplog.records)
    assert applied == [3], "and the session carries on, on the key it already had"


def test_pairing_invalidates_the_cached_snapshot_on_disk(tmp_path: Path) -> None:
    """Clearing the version claim in memory only is a clear that a power cut undoes.

    Anything between `Paired` and the first successful apply -- a reboot, a
    power cut, the link drop a failed key write can produce -- leaves the
    previous server's snapshot in the cache with its version. The next boot
    loads it, claims that version, and if the new server's counter collides the
    push is skipped: the rack shows the previous server's configuration, the
    server holds no ack, and nothing re-pushes.
    """
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"version": 3, "snapshot": CONFIG}))
    client, _ = make(tmp_path, [paired("k9")])

    client.tick_once()

    assert not cache.exists(), "a claim voided in memory has to be voided on disk too"


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


def test_the_heartbeat_interval_is_long_enough_to_be_evidence_a_session_ran(
    tmp_path: Path,
) -> None:
    """`_settle` spends this number as the bar for "a connection that lasted".

    Nothing on either end times out on daemon silence, so there is no liveness
    deadline this has to sit under. What there is, is `_settle`: a session that
    outlives one interval resets the backoff, so an interval short enough for a
    refused connection to reach makes every refusal look like a working session
    and restores the hammering the backoff exists to prevent. A connect and a
    close take milliseconds; seconds are the margin.
    """
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


def test_an_autumn_fall_back_is_not_an_hour_of_silence(tmp_path: Path) -> None:
    """The other night the offset moves, where the wall clock goes *backwards*.

    01:30 CEST to 01:30 CET is a full hour of elapsed time that the wall clock
    records as none at all -- two readings of one `system_clock` carry the same
    `tzinfo` object, and subtracting those is documented to ignore it. So the
    naive answer here is zero, and the heartbeat that is due does not go: the
    guard `0.0 <= since < heartbeat` bounds the harm to one interval, but an
    interval of silence is still an interval the server spends not hearing from
    a rack that is fine. Spring is tested above; this is the other direction.
    """
    clock = FakeClock(datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Amsterdam")))
    # No clock on the socket, so the hour below is the whole of what elapses and
    # `uptime_s` is a number this test can name rather than one it has to derive.
    socket = FakeSocket([TIMEOUT])
    socket._on_recv = lambda _: clock.advance(3600.0)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, heartbeat=1800.0)

    client.tick_once()

    beats = only(socket, Heartbeat)
    assert beats, "an hour elapsed, whatever the clock on the wall says"
    assert beats[0].uptime_s == 3600


def test_the_heartbeat_is_measured_from_the_connect_and_not_from_construction(
    tmp_path: Path,
) -> None:
    """A daemon that waited to be paired must not beat the moment it gets a socket.

    `_last_beat` is stamped at construction and again on connect, and only the
    second one is about this connection. Without it, every reconnect after a
    long outage opens with a heartbeat it does not owe -- and after a thirty
    second backoff, that is every reconnect there is.
    """
    clock = FakeClock(NOW)
    socket = FakeSocket([TIMEOUT], clock)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, heartbeat=15.0)
    clock.advance(600.0)  # ten minutes of backing off against a server that was down

    client.tick_once()

    assert not only(socket, Heartbeat), "the interval is measured from this connection"


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


def test_a_push_that_arrives_as_the_daemon_is_stopping_is_not_applied(tmp_path: Path) -> None:
    """`on_snapshot` is a teardown and repaint of four panels, run on this thread.

    `_serve` looks at the stop event between `recv` calls, so a push that was
    already in flight when SIGTERM landed is read and applied anyway -- and the
    apply then runs against a supervisor that is being torn down underneath it,
    inside a `SHUTDOWN_BUDGET` it can overrun on its own. Either outcome is four
    dark panels: an overrun is a SIGKILL before `Supervisor.stop` sleeps them,
    an abandoned apply is a rack halfway through a repaint.

    Nothing is sent in answer, and that is right. The server clears what it
    believes a daemon has confirmed on every connect, so silence reads as "still
    hasn't got it" -- which is exactly true.
    """
    stop = threading.Event()
    applied: list[int] = []
    socket = FakeSocket([push(7)], on_recv=lambda s: stop.set())
    client, _ = make(tmp_path, sockets=[socket], stop=stop, applied=lambda s, v: applied.append(v))

    client.tick_once()

    assert applied == []
    assert not only(socket, Ack) and not only(socket, Nack)


# --- what the server closed with --------------------------------------------


def test_the_transport_translates_a_close_code_into_this_modules_vocabulary() -> None:
    """`websockets.exceptions` is not imported above `_default_connect`, on purpose.

    The module has to stay importable on a machine with no `websockets`, so the
    package's exception types cannot appear in an `except` clause anywhere the
    receive loop can see. Translating at the boundary is what buys both: the
    transport knows `websockets`, and everything above it knows `LinkClosed`.
    """

    class Closed(Exception):
        def __init__(self) -> None:
            self.rcvd = type("Close", (), {"code": 4426, "reason": "protocol 1"})()

    class Raw:
        def send(self, payload: str) -> None:
            raise Closed()

        def recv(self, timeout: float | None = None) -> str:
            raise Closed()

    transport = _Socket(Raw(), Closed)

    for call in (lambda: transport.send("hi"), lambda: transport.recv(timeout=1.0)):
        with pytest.raises(LinkClosed) as caught:
            call()
        assert caught.value.code == 4426
        assert caught.value.reason == "protocol 1"


def test_a_close_that_carried_no_code_at_all_is_still_a_link_closed() -> None:
    """A dropped TCP connection closes with nothing received. `rcvd` is None."""

    class Closed(Exception):
        rcvd = None

    class Raw:
        def send(self, payload: str) -> None:
            raise Closed()

    with pytest.raises(LinkClosed) as caught:
        _Socket(Raw(), Closed).send("hi")

    assert caught.value.code is None


def test_a_refused_credential_is_an_error_and_goes_straight_to_the_cap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """4401 cannot self-heal. Dialling every second until it does is pure noise.

    It is also the one refusal a person has to act on, and a daemon that answers
    it with the same INFO line a wifi blip gets is a locked-out rack nobody can
    find in a log.
    """
    clock = FakeClock(NOW)
    closed = LinkClosed(CLOSE_UNAUTHORIZED, "unauthorized")
    client, _ = make(tmp_path, sockets=[FakeSocket([closed], clock)], clock=clock)

    with caplog.at_level(logging.ERROR):
        client.tick_once()

    assert client.retry_in == BACKOFF_CAP_S
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert "credential" in caplog.records[0].message


def test_a_refused_credential_is_still_retried_forever(tmp_path: Path) -> None:
    """A re-paired daemon has to come back on its own; nothing else will fetch it."""
    clock = FakeClock(NOW)
    sockets = [
        FakeSocket([LinkClosed(CLOSE_UNAUTHORIZED, "unauthorized")], clock) for _ in range(3)
    ]
    client, _ = make(tmp_path, sockets=sockets, clock=clock)

    for _ in range(3):
        client.tick_once()

    assert client.retry_in == BACKOFF_CAP_S
    assert [socket.url for socket in sockets[:1]] != [None], "capped is not stopped"


def test_a_protocol_skew_close_names_both_versions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The server documents 4426 as the one a daemon must not retry blindly.

    Reconnecting against a build that cannot understand you never gets anywhere,
    so the log line has to carry what an operator needs to upgrade one end: the
    version this build speaks and the one the server said it does.
    """
    clock = FakeClock(NOW)
    closed = LinkClosed(CLOSE_PROTOCOL_SKEW, "protocol 9")
    client, _ = make(tmp_path, sockets=[FakeSocket([closed], clock)], clock=clock)

    with caplog.at_level(logging.ERROR):
        client.tick_once()

    assert client.retry_in == BACKOFF_CAP_S
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert caplog.records[0].protocol == PROTOCOL_VERSION
    assert caplog.records[0].server_said == "protocol 9"


def test_an_ordinary_close_is_not_treated_as_hopeless(tmp_path: Path) -> None:
    """A superseded socket or a server restarting is exactly what backing off is for."""
    clock = FakeClock(NOW)
    sockets = [FakeSocket([LinkClosed(4409, "superseded")], clock) for _ in range(2)]
    client, _ = make(tmp_path, sockets=sockets, clock=clock)

    client.tick_once()
    assert client.retry_in == 1.0

    client.tick_once()
    assert client.retry_in == 2.0


# --- the send deadline ------------------------------------------------------


def test_a_send_that_parks_is_taken_apart_within_its_deadline(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Measured, not argued: without this the link thread never comes back.

    `websockets.sync.Connection.send` holds the protocol mutex across a blocking
    `sendall` and only ever calls `settimeout` when a close deadline is set,
    which is never during normal operation. Its own keepalive thread cannot
    rescue it either -- on a ping timeout that thread needs `send_context()`,
    which blocks on the mutex the wedged sender is holding. So a server that is
    TCP-alive and has stopped reading leaves this thread parked forever with
    `connected` still true, no heartbeat and no reconnect, and every cross-thread
    caller of `send` parked behind it.
    """
    socket = FakeSocket([TIMEOUT] * 5)
    socket.parks_after = 0  # the hello itself never lands
    client, _ = make(tmp_path, sockets=[socket], send_deadline=0.2)

    with caplog.at_level(logging.ERROR):
        started = time.monotonic()
        client.tick_once()
        took = time.monotonic() - started

    assert socket.aborted is True, "nothing else can free a thread parked in sendall"
    assert took < WAIT, f"the send was bounded, not parked: {took:.2f}s"
    assert any("stopped reading" in record.message for record in caplog.records)
    assert client.connected is False


def test_the_link_threads_own_ack_is_bounded_like_every_other_write(tmp_path: Path) -> None:
    """The hello, an ack and a frame are three callers and three code paths.

    This one is the worst of the three to leave unbounded: it is the thread that
    reads the socket and the thread that consults the stop event, so an ack that
    parks is a link that has stopped receiving, stopped beating, stopped
    noticing SIGTERM and will never reconnect -- with `connected` still true.
    """
    socket = FakeSocket([push(7)])
    socket.parks_after = 1  # the hello lands; the ack that follows the apply does not
    client, _ = make(tmp_path, sockets=[socket], send_deadline=0.2)

    started = time.monotonic()
    client.tick_once()
    took = time.monotonic() - started

    assert socket.aborted is True
    assert took < WAIT, f"the ack was bounded, not parked: {took:.2f}s"


def test_a_link_that_is_not_parked_is_never_taken_apart(tmp_path: Path) -> None:
    """The deadline is a deadline, not a session limit: a quiet link outlives it."""
    clock = FakeClock(NOW)
    socket = FakeSocket([TIMEOUT] * 4, clock)
    client, _ = make(tmp_path, sockets=[socket], clock=clock, send_deadline=0.2, heartbeat=1.0)

    client.tick_once()

    assert socket.aborted is False
    assert only(socket, Heartbeat), "and it kept beating throughout"


@contextlib.contextmanager
def deaf_peer() -> Iterator[int]:
    """A real socket that completes the handshake and then never reads again.

    Not a `websockets` server: one of those keeps draining the socket into its
    own buffers long after the handler has stopped asking for messages, which is
    a different failure. This is the one the module is about -- a peer that is
    TCP-alive, that has said nothing wrong, and whose receive window has closed.
    A four-kilobyte `SO_RCVBUF` on the listener is inherited by the accepted
    socket and turns off autotuning, so the window closes after kilobytes rather
    than after the megabytes a tuned loopback connection will take.
    """
    listener = socket_module.socket()
    listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_RCVBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    held: list[Any] = []

    def accept() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return  # the listener was closed; the test is over
            held.append(connection)
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            key = ""
            for line in request.decode().split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            digest = hashlib.sha1((key + _WS_GUID).encode()).digest()  # noqa: S324
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + base64.b64encode(digest) + b"\r\n\r\n"
            )

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()
        for connection in held:
            connection.close()


def test_a_real_peer_that_stops_reading_does_not_park_the_link_thread(tmp_path: Path) -> None:
    """The same measurement again, over a real socket and the real transport.

    With the deadline removed this run does not finish: `client.send` parks in
    `sendall` and stays there, `connected` stays true, no heartbeat goes out and
    no reconnect is ever attempted -- measured at forty-five seconds and killed,
    not observed to recover. With it, the socket is taken apart and `send`
    answers False, which is what task 11's frame path is written to expect.
    """
    with deaf_peer() as port:
        stop = threading.Event()
        client = LinkClient(
            settings=LinkSettings(
                server_url=f"http://127.0.0.1:{port}",
                cache_path=tmp_path / "cache.json",
                token="tok",
            ),
            settings_path=tmp_path / "link.json",
            on_snapshot=lambda snapshot, version: None,
            stop=stop,
            clock=lambda: datetime.now(UTC),
            connect_factory=_default_connect,
            send_deadline=0.5,
        )
        client.start()
        try:
            started = time.monotonic()
            while not client.connected and time.monotonic() - started < WAIT:
                time.sleep(0.01)
            assert client.connected, "the hello never reached the peer"

            filler = LogLine(level="INFO", message="x" * 60_000)
            started = time.monotonic()
            while client.send(filler) and time.monotonic() - started < WAIT:
                pass
            wedged = time.monotonic() - started
        finally:
            stop.set()
            client.join(WAIT)

    assert wedged < WAIT, f"the send parked rather than raising: {wedged:.2f}s"
    assert client.is_alive() is False, "and the link thread came back to be joined"


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
    that into a reconnect every second, forever, against a server that has
    already said no: the reset clears `_backing_off` too, so the close that
    follows takes the plain-floor branch rather than the doubling one and the
    delay never leaves the floor. Measured on the reconstructed draft.
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


def test_a_pairing_this_user_cannot_open_is_said_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sudo ors-daemon connect` is how a rack unpairs itself silently.

    The file lands root-owned and 0600, the daemon runs as `User=openrackscreen`,
    and every read of it is a `PermissionError`. Answering that with the same
    silent None as "there is no pairing" leaves the rack running unpaired for
    ever behind one INFO line saying it was never paired -- which is the one
    thing the person who just ran `connect` knows to be false. A file that is
    *there* and cannot be read is a different fact, and it is reported as one.
    """
    path = tmp_path / "link.json"
    path.write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    def refuse(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with caplog.at_level(logging.ERROR):
        assert load_link_settings(path) is None

    assert [record.getMessage() for record in caplog.records] == [
        "this rack has a pairing file it cannot read; it is running unpaired"
    ]
    assert "PermissionError" in caplog.records[0].error


def test_no_pairing_at_all_is_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rack that has never been paired is the M2 rack, and it is not a fault.

    The distinction only says anything if the ordinary case stays quiet.
    """
    with caplog.at_level(logging.WARNING):
        assert load_link_settings(tmp_path / "absent.json") is None

    assert caplog.records == []


def test_settings_whose_fields_are_the_wrong_shape_are_no_pairing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The docstring promises None for every kind of wrong. Three kinds raised.

    A hand-edited file, a half-written one, or one from a build that spelled a
    field differently, and the caller's answer to all of them is the same: run
    the rack from the local config file and do not dial. Raising instead takes
    the daemon down at startup over a file it does not need to boot -- which is
    four dark panels for a corrupt *pairing*.
    """
    path = tmp_path / "link.json"
    corrupt = [
        {"server_url": "http://s", "token": "t", "daemon_id": "abc"},  # ValueError
        {"server_url": "http://s", "token": "t", "daemon_id": [1]},  # TypeError
        {"server_url": "http://s", "token": "t", "cache_path": ["a"]},  # TypeError
    ]
    for raw in corrupt:
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        with caplog.at_level(logging.WARNING):
            assert load_link_settings(path) is None, raw

    assert len(caplog.records) == len(corrupt), "and each one says what was wrong with it"
    assert all("TypeError" in r.error or "ValueError" in r.error for r in caplog.records)


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


# --- the transport ----------------------------------------------------------


def test_every_number_the_real_transport_runs_on_is_one_this_module_chose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inherited defaults are decisions nobody made, in a module full of made ones.

    Each of these has a rack-visible consequence -- `max_size` caps how large a
    `ConfigPush` this daemon can accept at all, `open_timeout` and `close_timeout`
    are both time a SIGTERM waits out -- so a `websockets` release that moves one
    must move something here, not something on the rack.
    """
    import websockets.sync.client

    seen: dict[str, Any] = {}

    def record(url: str, **kwargs: Any) -> str:
        seen.update(kwargs, url=url)
        return "connection"

    monkeypatch.setattr(websockets.sync.client, "connect", record)

    transport = _default_connect("ws://rack:8080/ws/daemon")

    assert isinstance(transport, _Socket)
    assert seen["url"] == "ws://rack:8080/ws/daemon"
    assert seen["max_size"] == MAX_MESSAGE_BYTES
    assert seen["open_timeout"] is not None
    assert seen["close_timeout"] is not None
    assert seen["ping_interval"] is not None
    assert seen["ping_timeout"] is not None
