"""The daemon's end of the link, and the rule that governs all of it.

*No failure of the server, of this socket or of the network may darken the
rack.* Everything below follows from that. The connection is a source of
configuration, not a dependency: a server that is down, unreachable, speaking a
protocol this build has never heard of, or refusing this daemon's credential
outright, is answered here with a log line and a retry, while four panels keep
drawing from the snapshot they already have. Nothing in this module raises into
the daemon, and the link thread cannot end on its own.

Four things about the protocol are worth having in front of you before reading
the code, because each one is a place a plausible implementation goes wrong.

*One credential, two lifetimes.* `Hello.token` carries either the one-time
pairing token or the persistent key the server hands back in `Paired`. That key
arrives exactly once, on the connect that spends the token, and if it is not
written to disk the daemon holds a credential the server has already deleted:
it can never reconnect, and the only recovery is minting a new token in the
interface. Persisting it is therefore the single most important thing this
module does -- see `_paired`.

*The version the daemon claims is a claim about a server.* `Hello.config_version`
lets the server skip a push, and a push is a teardown and repaint of the whole
rack rather than a cheap message, so the claim is worth making. But it is only
ever true relative to the server that pushed it, which is why pairing voids it,
and it must be reported pessimistically: `None` means "I have nothing", and
getting that wrong in the optimistic direction is a blank rack.

*A skipped apply is still acked.* The server clears what it believes a daemon
has confirmed on every connect, so a push this daemon already runs must still be
answered -- otherwise the server learns nothing and pushes the same
configuration again on the next connect, forever.

*A send can park forever, and nothing in `websockets` will free it.* Its sync
`Connection.send` holds the protocol mutex across a blocking `sendall` and only
applies a socket timeout when a close deadline is set, which never happens
during normal operation; its own keepalive thread cannot rescue a wedged sender
either, because a ping timeout needs `send_context()` and that blocks on the
same mutex. So a server that is TCP-alive and has stopped reading is the one
failure reconnecting does *not* answer -- there is no reconnect, the thread
never returns. `SEND_DEADLINE_S` and the watchdog that enforces it are the
whole reason this module owns a second thread while a socket is open.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket as socket_module
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ors_schema.daemon import DaemonConfig
from ors_schema.errors import first_error
from ors_schema.link import (
    PROTOCOL_VERSION,
    Ack,
    Command,
    ConfigPush,
    DaemonMessage,
    DetectRequest,
    DetectResult,
    FramesRequest,
    Heartbeat,
    Hello,
    Nack,
    Paired,
    ProbeRequest,
    ProbeResult,
    parse_server_message,
)
from pydantic import ValidationError

from ors_daemon import __version__
from ors_daemon.clock import Clock

log = logging.getLogger(__name__)

SnapshotHandler = Callable[[DaemonConfig, int], None]
"""What to do with a pushed configuration. Called as `handler(snapshot, version)`.

Two obligations, and neither is enforceable from here.

*It must bound how long it takes, and the bound is seconds rather than tens of
them.* It runs synchronously on the link thread, which is the thread that reads
this socket and the thread that consults the stop event, so its duration is
added directly to how long a SIGTERM takes to be noticed. That is spent out of
`ors_daemon.supervisor.SHUTDOWN_BUDGET` -- ten seconds for the whole daemon --
alongside `RECV_TIMEOUT_S` and `CLOSE_TIMEOUT_S`. Overrun it and systemd sends
SIGKILL before `Supervisor.stop` runs, which leaves four panels lit until
somebody pulls the power; the alternative, abandoning the apply where it stands,
is four panels halfway through a repaint. The link refuses to *start* an apply
once the stop event is set (see `_config`), which is as much as it can do: it
cannot interrupt one that is already running.

*It must raise on a configuration it cannot serve, rather than partially
applying it.* A raise is a nack, which is how the person who saved the edit
finds out; a half-applied snapshot is a rack that disagrees with the interface
and an ack that says it does not.
"""
CommandHandler = Callable[[Command], None]
FramesHandler = Callable[[FramesRequest], None]
DetectHandler = Callable[[DetectRequest], DetectResult]
ProbeHandler = Callable[[ProbeRequest], ProbeResult]
"""The two handlers that *answer*, which is what makes them a different shape.

`on_command` and `on_frames_request` are told something; these two are asked, and
an HTTP handler on the server is holding a request open waiting for the reply.
So they return the message rather than sending it, and `_answer` puts it on the
socket the question arrived on -- which is this end's only way of being sure the
reply goes back down the connection that asked, rather than whichever one happens
to be current by the time a handler that held a bus for five seconds returns.

The obligations that go with them are the producer's, in `ors_daemon.hardware`:
answer every request, fill `ProbeResult.error` whenever `ok` is false, and keep
every field inside the bounds the schema will refuse. A reply that fails to parse
is answered by the server's wait expiring, so a daemon that knew the reason
reports a timeout instead.
"""

DAEMON_PATH = "/ws/daemon"
"""The route `ors_server.link.ws_daemon` mounts. Appended to the server URL."""

HEARTBEAT_INTERVAL_S = 15.0
"""How often a daemon says it is still there, in seconds.

Nothing on either end times out on daemon silence, so this is not a deadline
anyone has set: the hub knows a rack is online because it is holding its socket,
uvicorn's ping/pong notices a peer that has gone away, and the server's
`SEND_TIMEOUT` bounds an *outbound* send rather than a wait for one. It is the
coarse "when did we last hear anything" the interface shows, and both bounds on
it are local.

The floor is `_settle`, which spends this number as the bar for "a connection
that lasted" -- the only evidence this end can get, without a reply, that the
server did not refuse it. A short interval makes a connection that was accepted
and immediately closed count as a working session, which resets the backoff and
restores exactly the hammering `_settle` exists to prevent. A connect and a
refusal take milliseconds; seconds are the margin, and `_settle` reads the
attribute rather than a constant so the relationship cannot drift.

The ceiling is what the message costs. Every non-frame message makes the server
write `daemon.last_seen`, which is one SQLite write per rack per beat -- four a
minute here, against a link that is otherwise silent for hours at a time -- and
the freshness a person reads off the interface, which nobody can perceive below
a few seconds.

Deliberately unrelated to `RECV_TIMEOUT_S`, which is about shutdown latency.
"""

SEND_DEADLINE_S = 5.0
"""How long one write to the socket may take before the socket is taken apart.

The module docstring says why there has to be one at all: `websockets.sync` puts
no bound on a send during normal operation, and the failure it leaves open --
a server that is TCP-alive and has stopped reading -- is the only one that
reconnecting cannot answer, because the thread that would reconnect is the thread
that is parked. The server bounds this exact failure in the other direction with
its own `SEND_TIMEOUT`; this is the daemon's half.

The floor is the largest thing this end legitimately writes, which is task 11's
frame: a WebP of one 240x240 panel, base64'd, is tens of kilobytes, and even a
Pi 3B+ on congested wifi puts that on the wire in well under a second.

The ceiling is shutdown. A parked send is time a SIGTERM waits out -- the link
thread reaches it at the next beat, and any other thread that called `send`
waits behind it -- against `Supervisor.SHUTDOWN_BUDGET` of ten seconds for the
whole daemon. Five leaves room for the rest of the teardown; the alternative to
picking a number is forever.
"""

# The 4000-4999 codes `ors_server.link.ws_daemon` closes with. Only the two this
# end acts on differently are named here, because a copy of a vocabulary is a
# copy that goes stale: the server owns the list, and everything not named below
# is deliberately handled as "try again". They are duplicated rather than
# imported because the daemon does not depend on the server package -- if a
# third code ever needs acting on, they belong in `ors_schema.link` where both
# ends can read one definition.
CLOSE_UNAUTHORIZED = 4401
"""No credential, or one that matches nothing. Needs a person; cannot self-heal."""
CLOSE_PROTOCOL_SKEW = 4426
"""The two ends do not speak the same version of this protocol. Needs an upgrade."""

RECV_TIMEOUT_S = 1.0
"""How long the receive loop may sit in `recv` before it looks at the stop event.

Not a network timeout -- a quiet server is a healthy server, and reaching this
deadline means nothing at all. It exists because `recv()` without one blocks
until the server speaks, so the stop flag would only be consulted when a message
happened to arrive: a SIGTERM on the rack would leave `run_forever` joining this
thread against a server that may say nothing for hours.

A second, because the supervisor already ticks at 1 Hz and the wake is a
timer expiry that hands back to the loop. Shorter buys nothing anyone can
perceive; much longer is a shutdown that a person watching `systemctl stop`
notices.
"""

BACKOFF_FLOOR_S = 1.0
BACKOFF_CAP_S = 30.0

# `websockets` ships a default for each of the four numbers below, and inherited
# defaults are decisions nobody made -- in a module where `RECV_TIMEOUT_S` gets
# fourteen lines of argument. Each has a consequence on the rack, so each is
# stated here: a release that moves one has to move something in this file
# rather than something on a Pi.

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
"""The largest message this daemon will read. `websockets` defaults to 1 MiB.

It is a cap on how large a `ConfigPush` this rack can accept at all, and
exceeding it is not a soft failure: `websockets` closes the socket, so an
oversized snapshot is a reconnect loop in which the server pushes the same thing
every time and never learns why. Four mebibytes because a snapshot is JSON text
-- screen parameters, template names and integration URLs -- and a rack's worth
is kilobytes; four is three orders of magnitude of headroom while still being a
number a Pi 3B+ can hold several of without swapping.

Residual risk, recorded rather than solved: nothing on the server bounds what it
assembles, so the two ends can disagree about what is servable and only this one
finds out. `DaemonConfig.screens` has no cardinality bound either -- see the
note in `ors_schema.daemon` -- so the honest description of this number is a
memory bound on the daemon, not an agreement between the ends.
"""

OPEN_TIMEOUT_S = 5.0
"""How long a connect attempt may take before it is abandoned. Default is 10.

Two things are being traded. A connect that has not completed in five seconds
against a server on the same LAN -- DNS, TCP, TLS and one HTTP upgrade -- is one
that is not going to, and abandoning it costs a single backoff. Against that,
this is time a SIGTERM waits out: the stop event is not looked at again until
the attempt returns, so a shutdown landing mid-connect pays it in full out of
`Supervisor.SHUTDOWN_BUDGET`.

Kept above the server's `HELLO_TIMEOUT_S` reasoning rather than below it: the
same congested LAN and half-booted Pi have to fit in both.
"""

CLOSE_TIMEOUT_S = 2.0
"""How long `close()` may spend on the closing handshake. Default is 10.

Ten seconds is the entire `SHUTDOWN_BUDGET`, spent on a courtesy. The handshake
is worth attempting -- it is what lets the server drop the connection promptly
rather than waiting for a ping timeout -- but not worth a rack that systemd
SIGKILLs before `Supervisor.stop` can sleep the panels, which is four panels
left lit until someone pulls the power.
"""

PING_INTERVAL_S = 20.0
PING_TIMEOUT_S = 20.0
"""`websockets`' own keepalive, at its defaults, stated so they are a choice.

It answers a peer that has *gone away* -- a Pi whose wifi dropped without a FIN
-- and it is worth having for that: without it a half-open socket is a link that
looks up forever. It answers nothing about a peer that is present and not
reading, because the keepalive thread needs `send_context()` to send its ping
and that blocks on the mutex a parked sender holds. `SEND_DEADLINE_S` is what
covers that case, and these two do not overlap with it.

Twenty and twenty because a dead peer is then noticed inside forty seconds,
which is well under the backoff cap and far above any pause a Pi's wifi takes in
normal service.
"""


class LinkError(Exception):
    """The link cannot be attempted at all -- a server URL nothing can dial."""


class LinkClosed(Exception):
    """The server closed the socket, and said why with a code.

    This module's own, deliberately: `websockets.exceptions` may not be imported
    anywhere above `_default_connect`, because everything else here has to stay
    importable and testable on a machine with no `websockets` at all. `_Socket`
    translates at the boundary so that the receive loop can act on a close
    without either end of that constraint being compromised.

    `code` is None for a close that carried none -- a TCP connection that simply
    dropped -- which is the common case and the one that means nothing.
    """

    def __init__(self, code: int | None, reason: str = "") -> None:
        super().__init__(f"the server closed this link: {code} {reason}".strip())
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class LinkSettings:
    """Everything the daemon needs to find its server and prove who it is.

    `token` and `key` are the same field on the wire and never both usable: the
    server deletes a token's hash in the same statement that mints the key, so a
    spent token authenticates nothing anywhere. It is dropped from this record
    the moment `Paired` arrives, because a dead credential kept on disk is a
    credential in every backup for no benefit at all.

    Frozen, so a rewrite is `replace(...)` and one thread cannot be reading
    half of an update another is making.
    """

    server_url: str
    cache_path: Path
    token: str | None = None
    key: str | None = None
    daemon_id: int | None = None

    @property
    def credential(self) -> str | None:
        """What to present in `hello`: the key if this daemon has one.

        The key first, always. It is what every connect after the first
        presents, and reaching for the token when a key exists would ask the
        server to spend a pairing that has already been spent.
        """
        return self.key or self.token


def load_link_settings(path: Path) -> LinkSettings | None:
    """The link's own state, or None for a daemon nobody has paired.

    None rather than an exception for every kind of wrong, because the caller's
    answer to all of them is the same: run the rack from the local config file
    and do not dial. A corrupt pairing file is not a reason to leave four panels
    dark.

    A record with a server but no credential is *not* a pairing. It is what a
    half-written file or a hand-edited one looks like, and treating it as one
    would have the daemon open a socket it can only be refused on, once per
    backoff, forever.

    *"There is no pairing" and "there is one I cannot read" are the same answer
    and different news.* Only the first is ordinary: an unpaired rack is the M2
    rack, and it runs from its config file exactly as it always did. The second
    is `sudo ors-daemon connect` -- which writes the file root-owned and 0600
    while the daemon runs as `User=openrackscreen` under the shipped unit, so
    every read of it raises `PermissionError`. Reported as absence, that is a
    rack which silently runs unpaired for ever behind one INFO line saying it
    was never paired, which is the one thing the person who just ran `connect`
    knows to be false. So a file that exists and cannot be turned into a pairing
    is an ERROR naming the path, and only `FileNotFoundError` is quiet.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.error(
            "this rack has a pairing file it cannot read; it is running unpaired",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if not isinstance(raw, dict) or not raw.get("server_url"):
        log.warning("link settings name no server; ignoring them", extra={"path": str(path)})
        return None

    mode = _mode_of(path)
    if mode is not None and mode & 0o077:
        # A warning and not a refusal, which is the opposite of what
        # `ors_server.secrets` does with its encryption key, deliberately. That
        # key decrypts every credential the server holds; this one gets one
        # rack's configuration and the right to draw on its panels. Refusing to
        # load it would leave a rack unmanageable over a permission bit, and the
        # daemon would then be dialling nothing while the log said why.
        log.warning(
            "the link settings are readable by more than their owner",
            extra={"path": str(path), "mode": oct(mode)},
        )

    try:
        settings = LinkSettings(
            server_url=str(raw["server_url"]),
            cache_path=Path(raw.get("cache_path") or path.with_name("snapshot.json")),
            token=raw.get("token") or None,
            key=raw.get("key") or None,
            daemon_id=int(raw["daemon_id"]) if raw.get("daemon_id") is not None else None,
        )
    except (TypeError, ValueError) as exc:
        # Everything above reads a value out of a JSON document, and JSON can
        # only hand back a dict, a list, a string, a number, a bool or null --
        # so `TypeError` and `ValueError` between them are exhaustive over what
        # the *content* of this file can do, whatever shape it is in. Measured
        # examples, each of which raised straight through before: a `daemon_id`
        # of "abc" (ValueError from `int`), a `daemon_id` of [1] (TypeError from
        # `int`), a `cache_path` of ["a"] (TypeError from `Path`).
        #
        # Returned as None rather than raised because the docstring promises it
        # and the promise is load-bearing: this is called at startup, and a
        # daemon that refuses to boot over a corrupt pairing file is four dark
        # panels for the sake of a file it does not need in order to draw.
        log.warning(
            "link settings that cannot be read; ignoring them",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if settings.credential is None:
        log.warning("link settings carry no credential; ignoring them", extra={"path": str(path)})
        return None
    return settings


def write_link_settings(path: Path, settings: LinkSettings) -> None:
    """Write the pairing atomically, and never wider than 0600.

    Both halves matter and for different reasons. The mode is because this file
    is what pairs the rack: a key with it is a key to that rack's configuration
    and its panels. The atomicity is because this file is *rewritten* -- the key
    replaces the token on the connect that pairs -- and a plain write truncates
    first: a power cut in that window leaves a rack holding neither credential,
    which nothing on the Pi can recover from and which needs a new token minted
    in the interface to fix.

    So the same shape as `ors_daemon.status.write_status` and
    `LinkClient._write_cache`: a temporary file beside the target (a rename is
    only atomic within one filesystem), `fsync` before the rename (or the name
    can point at contents that never reached the disk), then `os.replace`. The
    temporary is created 0600 as well, since it holds the credential for as long
    as the real file would, and `fchmod` forces the mode even if a previous run
    left one behind with a wider one.

    Raises. Whether a link that cannot save its pairing should stop is the
    caller's call, and the two callers answer differently: the `connect` command
    must fail loudly, and the running daemon must not.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "server_url": settings.server_url,
            "cache_path": str(settings.cache_path),
            "token": settings.token,
            "key": settings.key,
            "daemon_id": settings.daemon_id,
        },
        indent=2,
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        # The target is untouched -- that is the whole point of writing beside
        # it -- so all that is left is not to leave litter that the next write
        # would have to reason about.
        temporary.unlink(missing_ok=True)
        raise


def websocket_url(base: str) -> str:
    """The daemon socket's URL, derived from the server's.

    Parsed rather than string-replaced. Two `str.replace` calls rewrite every
    occurrence of `http://` anywhere in the URL, including inside a path or a
    query, and they answer nothing about a URL that names no host at all --
    which is what a `--server rack:8080` typo produces, and what M2's tunnel
    already had to learn to reject.

    Query and fragment are dropped: this is a route, and anything else in there
    was not meant for it. A path prefix is kept, because a server behind a
    reverse proxy at `/ors` is a real deployment and `wss://host/ws/daemon`
    would miss it.
    """
    parsed = urlsplit(base.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        raise LinkError(
            f"cannot dial {base!r}: expected an http(s) or ws(s) URL naming a host, "
            "as in http://rack-server:8080"
        )
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/") + DAEMON_PATH, "", ""))


class LinkClient(threading.Thread):
    """One socket to the server, reconnecting forever.

    The server going away is a normal state, not an error: the daemon keeps
    rendering from its cache throughout, and this thread's only job is to notice
    when the server is back and take whatever it has been given since.

    `heartbeat` is the *interval* between heartbeat messages, in seconds -- not
    the monotonic liveness stamp that `Poller.heartbeat` and
    `ScreenWorker.heartbeat` carry for the supervisor's watchdog. The supervisor
    does not watch this thread, and publishing a stamp nobody reads would be the
    mistake `Tunnel` records having made with its `ready` event: for every way a
    link can fail *except one*, reconnecting already does everything a restart
    would.

    The exception is a send that never returns, which is why `_SendWatchdog`
    exists. It is not a liveness watchdog on this thread -- it watches one
    socket for one specific thing, for as long as that socket is open, and its
    answer is to take the socket apart so that reconnecting can happen at all.

    Everything public here is read from other threads: `connected` and
    `retry_in` for a status report, and `send` by task 11's frame path.
    """

    def __init__(
        self,
        settings: LinkSettings,
        settings_path: Path,
        on_snapshot: SnapshotHandler,
        stop: threading.Event,
        clock: Clock,
        connect_factory: Callable[[str], Any] | None = None,
        config_version: int | None = None,
        on_command: CommandHandler | None = None,
        on_frames_request: FramesHandler | None = None,
        on_detect: DetectHandler | None = None,
        on_probe: ProbeHandler | None = None,
        on_link_down: Callable[[], None] | None = None,
        heartbeat: float = HEARTBEAT_INTERVAL_S,
        backoff_floor: float = BACKOFF_FLOOR_S,
        backoff_cap: float = BACKOFF_CAP_S,
        send_deadline: float = SEND_DEADLINE_S,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """`settings_path` has no default on purpose.

        It is where the key from `Paired` is written, and a client constructed
        without one pairs successfully and is locked out on its next boot. A
        `TypeError` at construction is the loudest way to say that.

        `config_version` is what the daemon is *running* as it starts -- the
        version of the cached snapshot it booted from, or None if it booted from
        its local file or from nothing. It is the caller's to supply because
        only the caller knows which of those happened: a cache whose version
        parses but whose snapshot does not is a cache the daemon did not boot
        from, and reading the number out of the file here would claim a
        configuration that is not on the panels.
        """
        # Never `_stop`: `threading.Thread._stop` is a real method that `join`
        # calls, so an event stored under that name makes every join raise.
        super().__init__(name="link", daemon=True)
        self.settings = settings
        self.config_version = config_version
        self.connected = False
        self.heartbeat = heartbeat
        self.retry_in = backoff_floor

        self._settings_path = Path(settings_path)
        self._on_snapshot = on_snapshot
        self._on_command = on_command
        self._on_frames_request = on_frames_request
        self._on_detect = on_detect
        self._on_probe = on_probe
        # The other half of `on_frames_request`: a subscription arrives on a
        # socket and has to end with it. See `_dispatch_link_down`.
        self._on_link_down = on_link_down
        self._stop_event = stop
        self._clock = clock
        self._connect = connect_factory or _default_connect
        self._backoff_floor = backoff_floor
        self._backoff_cap = backoff_cap
        # Waiting on the stop event rather than sleeping is what makes SIGTERM
        # feel immediate: a thirty-second backoff would otherwise have to be sat
        # out in full before the loop noticed it had been asked to leave.
        self._sleeper = sleeper or stop.wait
        self._backing_off = False
        self._started_at = clock()
        self._last_beat = self._started_at
        # The socket is handed to other threads through `send`, so it is only
        # ever read or replaced under this lock -- and `send` on it is
        # serialised with this end's own acks and heartbeats.
        self._send_lock = threading.Lock()
        self._connection: Any | None = None
        self._send_deadline = send_deadline
        # `time.monotonic()` by which the write now in flight must have finished,
        # or None when none is. Written by whichever thread is sending and read
        # by the watchdog, which is why it is one float and not a structure:
        # a single attribute read cannot see half of an update.
        self._send_expiry: float | None = None
        # Monotonic and not the injected clock, because this is a deadline on a
        # thread that is stuck rather than a question about the time of day. An
        # NTP step must not turn a healthy send into a force-close, or a wedged
        # one into a wait that never ends.
        self._now = time.monotonic

    def tick_once(self) -> None:
        """One connection attempt, run to its end. The unit the tests drive."""
        credential = self.settings.credential
        if credential is None:
            # Nothing to say and no way to say it. Backed off rather than
            # raised, so a daemon started before anyone paired it keeps drawing.
            log.error("this daemon has no pairing; not connecting")
            self._settle(0.0)
            return

        connection = None
        watchdog = None
        hopeless = False
        opened = self._clock()
        try:
            url = websocket_url(self.settings.server_url)
            connection = self._connect(url)
            with self._send_lock:
                self._connection = connection
            # Before the first byte goes out, because the hello is a send like
            # any other and a server that accepts a socket and reads nothing
            # parks on it exactly as one that stops reading later does.
            watchdog = _SendWatchdog(self, connection)
            watchdog.start()
            # Under the lock like every other write, even though this one is
            # the first: `_connection` is published above, so task 11's frame
            # path can already be in `send` by the time this runs, and two
            # writes in flight at once would be two deadlines in one attribute.
            with self._send_lock, self._deadline():
                connection.send(
                    Hello(
                        token=credential,
                        hostname=os.uname().nodename,
                        daemon_version=__version__,
                        config_version=self.config_version,
                    ).model_dump_json()
                )
            self.connected = True
            self._last_beat = self._clock()
            log.info("link up", extra={"server": url, "config_version": self.config_version})
            self._reack(connection)
            self._serve(connection)
        except LinkClosed as closed:
            # The server said why, and two of the reasons are not "try again".
            hopeless = self._refused(closed)
        except Exception as exc:
            # Every other way this can end arrives here, and none of them is
            # worth a traceback: a server that is down, a socket that dropped
            # mid-message and a wifi blip are all "try again".
            log.info("link down", extra={"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.connected = False
            with self._send_lock:
                self._connection = None
            # A subscription is not a fact about this daemon; it is a fact about
            # a browser the server was relaying to, and the socket that carried
            # it has gone. Without this the frame stream keeps a set of screen
            # ids that nothing can be sent for, and the pump keeps waking and
            # encoding WebP for them -- 7-15ms of a Pi per panel per frame,
            # spent for nobody, for as long as the server is away. A reconnect
            # is what turns it back on: `_resume_frames` asks again on every
            # connect, precisely so that a browser that is still watching does
            # not have to wait for a person to reload the page.
            self._dispatch_link_down()
            if watchdog is not None:
                watchdog.stop()
            self._settle(_elapsed(opened, self._clock()), hopeless=hopeless)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    log.debug("closing a dead socket failed")

    def _reack(self, connection: Any) -> None:
        """Say what this rack is actually running, on every connect. Second, after hello.

        The other half of spec section 8's "daemons reconnect and re-ack their
        version. Nothing is re-pushed if the versions already match." Only the
        second sentence was built: the server compares `Hello.config_version`,
        skips the push on a match, and is then holding no evidence at all --
        `Hub.register` cleared what this daemon had last confirmed, and a
        skipped push is a push there is nothing to ack. So after every
        reconnect, and a wifi blip is a reconnect, the server could report what
        it had minted and nothing about what was on the glass.

        The claim in the hello is not that evidence and is not being promoted to
        it: it may still only ever *skip* a push, which is what stops a daemon
        talking its way out of a configuration. This is the same fact said in
        the message the server treats as an answer, so that the interface can
        tell "pushed" from "applied".

        *After the hello and never before it.* The server authenticates and
        registers on the hello, and `register` is what clears the previous ack --
        one arriving first would be read on an unidentified socket and then
        cleared by the connect it belongs to.

        *Not on the connect that spends a pairing token.* `ws_daemon._push_for`
        refuses to believe the claim there and says why: at the moment a token is
        spent, the server knows outright that it has given this daemon nothing,
        so there is nothing for an ack to be about. Judged on holding a key
        rather than on anything the server says, because this end has to decide
        before it has heard back.

        *Nothing at all when the daemon is running nothing.* None is "I have no
        configuration", and acking 0 would be the mistake `Hello.config_version`
        documents in the other direction -- 0 is a real version and the one an
        empty server counts from.
        """
        version = self.config_version
        if version is None or self.settings.key is None:
            return
        self._send(connection, Ack(config_version=version))

    def run(self) -> None:
        """Connect, serve, wait, repeat, until stopped. Nothing gets out of here.

        `tick_once` is total -- it catches everything a socket can do -- so the
        only exceptions this could see come from the injected sleeper, and a
        link thread that ends is a rack that can never be reconfigured again
        with nothing anywhere saying why.
        """
        while not self._stop_event.is_set():
            self.tick_once()
            try:
                self._sleeper(self.retry_in)
            except Exception:
                log.exception("the link's sleeper failed")
                self._stop_event.wait(self.retry_in)

    def send(self, message: DaemonMessage) -> bool:
        """Send one message if the link is up. False if it is not.

        Dropped rather than queued, and that is what task 11's frames want: a
        frame held while the server is away is a stale panel image by the time
        anyone sees it, and a queue of them is memory this end does not control.
        """
        with self._send_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                with self._deadline():
                    connection.send(message.model_dump_json())
            except Exception as exc:
                # The receive loop is about to find the same thing out and end
                # the connection; there is nothing useful to do from here.
                log.debug("could not send", extra={"error": str(exc), "said": message.type})
                return False
            return True

    @contextlib.contextmanager
    def _deadline(self) -> Iterator[None]:
        """Bound one write to the socket. `SEND_DEADLINE_S` says why there is one.

        Only the deadline is published here; the enforcing is `_SendWatchdog`'s,
        because there is no way to interrupt a thread parked in `sendall` from
        inside that thread. The lock is already held by every caller, so at most
        one write is ever in flight and one expiry describes it.
        """
        self._send_expiry = self._now() + self._send_deadline
        try:
            yield
        finally:
            self._send_expiry = None

    def _overran(self) -> bool:
        """Whether the write in flight has passed its deadline. The watchdog asks.

        One read of one attribute, deliberately. A send that finishes between
        this read and the force-close that follows it is a send that had already
        overrun, so the worst the race can do is drop a link that had just
        recovered from being parked for a whole `SEND_DEADLINE_S` -- which
        reconnects, and which is not a state worth keeping anyway.
        """
        expiry = self._send_expiry
        return expiry is not None and self._now() >= expiry

    def _wedged(self, connection: Any) -> None:
        """Take the socket out from under a parked send. Called on the watchdog.

        An error, and not a quiet one. It is the only symptom of a server that
        accepted this link and stopped reading it, and without a line here that
        state is indistinguishable in a log from a link that is simply idle.
        """
        log.error(
            "the server accepted this link and stopped reading it; taking the socket apart",
            extra={"deadline_s": self._send_deadline},
        )
        try:
            connection.abort()
        except Exception as exc:
            # There is nothing left to try. Logged rather than raised because
            # this is a bare thread and an exception out of it goes nowhere.
            log.error("could not take apart a wedged socket", extra={"error": str(exc)})

    def _serve(self, connection: Any) -> None:
        """Read this socket until the server goes away or the daemon is stopped.

        `recv` is given a deadline so that the stop event is consulted whether or
        not the server has anything to say -- see `RECV_TIMEOUT_S`. Reaching it
        is not an error and not evidence about the link; it is where the
        heartbeat gets sent from on a quiet connection.
        """
        while not self._stop_event.is_set():
            try:
                raw = connection.recv(timeout=RECV_TIMEOUT_S)
            except TimeoutError:
                raw = None
            if raw is not None:
                self._receive(connection, raw)
            self._beat(connection)

    def _beat(self, connection: Any) -> None:
        """Say we are still here, if it is time to.

        Elapsed time and not wall-clock difference: the injected clock is
        timezone-aware and subtracting two of its readings directly answers a
        question about the clock on the wall, which loses or gains an hour
        across a DST shift (`ors_daemon.clock` has the long version). A negative
        answer -- an NTP step backwards -- is treated as due rather than as time
        to wait out, or a correction would buy the server hours of silence.
        """
        now = self._clock()
        since = _elapsed(self._last_beat, now)
        if 0.0 <= since < self.heartbeat:
            return
        self._last_beat = now
        uptime = max(0.0, _elapsed(self._started_at, now))
        self._send(connection, Heartbeat(uptime_s=int(uptime)))

    def _receive(self, connection: Any, raw: str) -> None:
        try:
            message = parse_server_message(raw)
        except ValidationError as exc:
            self._unreadable(connection, raw, exc)
            return

        if isinstance(message, ConfigPush):
            self._config(connection, message)
        elif isinstance(message, Paired):
            self._paired(message)
        elif isinstance(message, Command):
            self._dispatch("command", self._on_command, message)
        elif isinstance(message, FramesRequest):
            self._dispatch("frames", self._on_frames_request, message)
        elif isinstance(message, DetectRequest):
            self._answer(connection, "detect", self._on_detect, message)
        elif isinstance(message, ProbeRequest):
            self._answer(connection, "probe", self._on_probe, message)

    def _config(self, connection: Any, push: ConfigPush) -> None:
        """Apply a pushed configuration, or say why not. Always answers.

        **The skip below is defence, not a handshake.** It used to be described
        as the thing that stops a reconnect repainting a rack that is already
        showing the right thing, and that is not where the skipping happens: the
        server decides it, at hello, in `ws_daemon._push_for`, which answers None
        when the claimed version matches the row. No production path reaches
        here with a version this daemon already runs. Every push out of
        `changes.change` carries a number `bump_config_version_on` has just
        minted, which is by construction one this rack has never seen;
        `POST /api/daemons/{id}/push` bumps deliberately, precisely so that its
        push *cannot* be deduped away here; and the pairing connect -- the one
        connect on which the server pushes without believing the claim -- is
        preceded by a `Paired` that sets `config_version` to None, which matches
        no integer.

        So it is kept for what it costs nothing to keep: a version the server
        repeated by accident would otherwise be a full teardown and repaint of
        four panels. The ack still goes out either way, because the answer to a
        push is an ack whether or not there was work in it -- the server clears
        what it believes a daemon has confirmed on every connect, so silence
        here would read as "still hasn't got it" and buy another push next time.
        """
        if push.version == self.config_version:
            log.info("already running this configuration", extra={"version": push.version})
            self._send(connection, Ack(config_version=push.version))
            return

        if self._stop_event.is_set():
            # `_serve` only looks at the stop event between `recv` calls, so a
            # push already in flight when SIGTERM landed is read anyway -- and
            # `on_snapshot` is a teardown and repaint of four panels, run right
            # here on this thread, against a supervisor that is being torn down
            # underneath it. Both endings are dark panels: overrun
            # `SHUTDOWN_BUDGET` and systemd SIGKILLs before `Supervisor.stop`
            # can sleep them, or abandon the apply and leave the rack halfway
            # through a repaint.
            #
            # Answered with silence on purpose. The server clears what it
            # believes a daemon has confirmed on every connect, so saying
            # nothing reads as "still hasn't got it", which is exactly true, and
            # the next connect gets the push again.
            log.info("not applying a push while stopping", extra={"version": push.version})
            return

        try:
            self._on_snapshot(push.snapshot, push.version)
        except Exception as exc:
            # Anything the apply path can raise. It stops four panels being
            # taken down for a configuration one screen of which is unusable,
            # and it is the only way the person who saved the edit hears about
            # it: the server logs this reason where they are looking.
            self._nack(connection, push.version, f"{type(exc).__name__}: {exc}")
            return

        if self._stop_event.is_set():
            # `on_snapshot` did not apply this push in place -- it stopped the
            # supervisor instead, which is exactly what `_snapshot_handler`'s
            # mismatch branch does for a timezone change, the only thing under
            # this daemon that calls `supervisor.stop()` from inside
            # `on_snapshot`. (The early return above this method's docstring
            # names -- a push arriving while a stop was already in flight --
            # cannot be this: it returns before `_on_snapshot` is ever called,
            # so reaching here with the event set means this call is what set
            # it.)
            #
            # That makes the cache write below this push's *only* way to reach
            # the next boot: `run` is on its way out for a restart, and there
            # is no other copy of this configuration on disk. Acking anyway --
            # the ordinary rule for a push that keeps the rack running, three
            # lines down -- would have the next boot read the stale cache
            # under a version the server already believes is confirmed, so it
            # never re-pushes and the rack is stuck re-clocking against its
            # old zone on every restart, forever. Nacked instead: nothing here
            # believes this push landed, so the server's own retry on the next
            # connect is what actually recovers it.
            if not self._write_cache(push):
                self._nack(
                    connection,
                    push.version,
                    "could not cache this push before restarting to re-clock; will retry",
                )
                return
            self.config_version = push.version
            self._send(connection, Ack(config_version=push.version))
            return

        self.config_version = push.version
        self._write_cache(push)
        self._send(connection, Ack(config_version=push.version))

    def _paired(self, message: Paired) -> None:
        """Keep the key. Everything about this daemon's future depends on it.

        The token is spent -- the server deleted its hash in the same statement
        that minted this key -- so it is dropped rather than kept beside it.

        The running version is cleared as well, and that is not housekeeping.
        "I am running version N" is a claim about a particular server, and this
        is a new one: a re-imaged Pi with a stale cache claiming 0 against a
        server on the schema default of 0 would otherwise skip the pairing push
        and sit showing the previous rack's configuration, with the server
        holding an ack that says it is up to date. The server always pushes on
        the connect that spends a token for exactly this reason; this is the
        other half of that agreement.

        The clear is made durable, and that is the same decision rather than an
        extra one. Clearing the attribute alone leaves the previous server's
        snapshot in the cache, and anything at all between here and the first
        successful apply -- a power cut, a reboot, a link that drops -- means the
        next boot loads it, claims its version, and gets the push skipped if the
        new server's counter collides. There is no "push now" anywhere in M3a to
        recover with, so the file goes now.
        """
        if self.settings.key is not None:
            # Nothing on this socket authenticates the server: the URL is
            # whatever was typed on the command line, over a scheme that may not
            # even be TLS. A healthy server never sends `Paired` to a daemon it
            # has already keyed, so one arriving is a wrong address or somebody
            # answering on it -- and taking it would write their key over the
            # real one *on disk*, locking the rack out of its real server with
            # no recovery short of minting a new token. Refused, loudly, and the
            # session carries on on the credential that got it here.
            log.error(
                "the server sent a pairing to a daemon that already has a key; ignoring it",
                extra={"daemon": message.daemon_id, "server": self.settings.server_url},
            )
            return

        log.info("paired with the server", extra={"daemon": message.daemon_id})
        self.settings = replace(
            self.settings, key=message.key, token=None, daemon_id=message.daemon_id
        )
        self.config_version = None
        self._forget_cache()
        try:
            write_link_settings(self._settings_path, self.settings)
        except (OSError, ValueError) as exc:
            # `ValueError` for the same reason `_write_cache` catches it, and it
            # is the same line of code in both: `write_link_settings` derives its
            # temporary with `Path.with_name`, which raises `ValueError` and not
            # `OSError` for a path with no filename. Caught only as `OSError`,
            # that failure unwinds past this line into `tick_once`'s blanket
            # handler as one INFO line -- so the loudest thing that can happen
            # here says nothing, and the `ConfigPush` that follows `Paired` is
            # dropped with it.
            #
            # Nothing wider than these two. Anything else is a bug rather than a
            # disk or a configuration error, and it ends the connection instead
            # of being logged as something this handler survives.
            #
            # The key is good for this session either way, so the link stays up
            # and the rack keeps running -- but the next boot has nothing, and
            # recovering means minting a new token in the interface. It is the
            # worst thing that can quietly happen here, so it is an error.
            log.error(
                "could not save the key this daemon reconnects with; "
                "it will need pairing again after a restart",
                extra={"path": str(self._settings_path), "error": str(exc)},
            )

    def _forget_cache(self) -> None:
        """Delete the snapshot this daemon booted from. Raises nothing.

        Deleting rather than rewriting: there is nothing true to put in the file
        at this moment, and an absent cache is a boot from the local config
        file, which is the fallback the rest of the daemon is built around.
        """
        path = Path(self.settings.cache_path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Logged and stepped over. The claim is still cleared in memory, so
            # this session pushes and applies normally; what is lost is only the
            # protection against being interrupted before that happens.
            log.error(
                "could not clear the previous server's cached snapshot",
                extra={"path": str(path), "error": str(exc)},
            )

    def _dispatch(self, what: str, handler: Callable[[Any], None] | None, message: Any) -> None:
        """Hand a message to whoever is listening, if anyone is.

        The seam task 11 plugs the frame path into. Nothing here knows what a
        handler does, so a handler that raises is logged and stepped over: this
        socket is how the rack is reconfigured, and one bad command must not be
        what costs it.
        """
        if handler is None:
            log.debug("nothing is listening for this", extra={"said": what})
            return
        try:
            handler(message)
        except Exception:
            log.exception("a link handler raised", extra={"said": what})

    def _answer(
        self, connection: Any, what: str, handler: Callable[[Any], Any] | None, message: Any
    ) -> None:
        """Hand over a request that expects a reply, and send whatever comes back.

        `_dispatch`'s sibling, and the difference is that silence here has a
        cost: somebody is waiting. A correlated request nothing answers is a
        wait that expires at the far end, which the operator reads as "this rack
        did not respond" -- for a rack that is perfectly healthy and simply was
        not wired to its own handler. That is the M3a defect exactly (`_link`
        built the client without `on_command`, and a route reported
        `delivered: true` for a message no supervisor ever saw), so the missing
        handler is a warning naming the request rather than the debug line
        `_dispatch` gets away with.

        A handler that raises is logged and stepped over, for `_dispatch`'s
        reason: this socket is how the rack is reconfigured, and one bad request
        must not be what costs it. The handlers this end passes are written to
        turn every failure into a reply instead -- see `ors_daemon.hardware` --
        so reaching this branch is a bug in the daemon rather than a probe that
        did not work.
        """
        if handler is None:
            log.warning(
                "nothing here can answer this; the server's request will time out",
                extra={"said": what, "request": getattr(message, "request_id", None)},
            )
            return
        try:
            reply = handler(message)
        except Exception:
            log.exception("a link handler raised", extra={"said": what})
            return
        self._send(connection, reply)

    def _dispatch_link_down(self) -> None:
        """Tell whoever is listening that this connection has ended. Raises nothing.

        On the `finally` of every attempt, including the ones that never
        connected: it means "there is no socket", which is as true of a dial
        that was refused as of a link that dropped, and the answer -- stop
        producing for a peer that cannot be reached -- is the same either way.

        Guarded like `_dispatch` and for the same reason. This runs inside the
        cleanup of the thread that is the rack's only way of being
        reconfigured, so a listener that raises here must not be able to end it.
        """
        if self._on_link_down is None:
            return
        try:
            self._on_link_down()
        except Exception:
            log.exception("a link-down handler raised")

    def _unreadable(self, connection: Any, raw: str, error: ValidationError) -> None:
        """Answer a message this build could not parse.

        Only a config push is nacked, and the distinction is the point: a nack
        is an answer to a *push*, logged by the server at error level as a rack
        refusing its configuration. Sending one for a message type this build
        has never heard of would report version skew as a refused snapshot, and
        the server's own reader skips an unreadable message for the mirror-image
        reason -- one bad message is not worth a whole rack's link.
        """
        if _claimed_type(raw) != "config":
            log.warning("unreadable message from the server; skipped", extra=_why(error))
            return
        self._nack(connection, _version_of(raw), first_error(error))

    def _nack(self, connection: Any, version: int, reason: str) -> None:
        log.error("refused a snapshot", extra={"version": version, "reason": reason})
        self._send(connection, Nack(config_version=version, reason=reason[:500]))

    def _send(self, connection: Any, message: DaemonMessage) -> None:
        """Send on the socket this loop is serving, under the shared lock.

        Bounded like every other write here. This one is the link thread's own,
        so a parked ack or heartbeat is the receive loop stopping, the stop event
        going unread and the reconnect never happening.
        """
        with self._send_lock:
            with self._deadline():
                connection.send(message.model_dump_json())

    def _write_cache(self, push: ConfigPush) -> bool:
        """Save what to boot from when the server is unreachable. Raises nothing.

        Atomic for the reason `write_status` is: a reader landing inside a plain
        write gets an empty file, and the reader here is the next boot of the
        rack. Failures are logged rather than raised, because for most callers
        the configuration is already *running* by the time this returns --
        losing the ack over a read-only disk would have the server re-push on
        every connect, which is a full repaint of the rack each time, to fix a
        problem the next boot has and this moment does not.

        Returns whether the write actually landed, rather than leaving that to
        a log line: `_config`'s re-clock branch is the one caller for which the
        configuration is *not* already running and this write is the only copy
        of the push that will exist once the process exits -- it has to know,
        not just log, so it can nack instead of acking a push it never
        durably kept.
        """
        path = Path(self.settings.cache_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            with open(temporary, "w") as handle:
                json.dump(
                    {"version": push.version, "snapshot": push.snapshot.model_dump(mode="json")},
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError) as exc:
            # `ValueError` for the same reason `Supervisor.tick` catches it: a
            # cache path with no filename cannot have a temporary derived beside
            # it, and that is a configuration error rather than a disk one.
            log.error(
                "could not cache the snapshot; this rack boots from its config file",
                extra={"path": str(path), "error": str(exc)},
            )
            return False
        return True

    def _refused(self, closed: LinkClosed) -> bool:
        """Log a close the server put a code on, and say whether to stop trying hard.

        The server defines the whole 4000-series vocabulary and states the reason
        it bothered: a client that cannot tell "your credential is wrong" from
        "your configuration is unservable" retries the first forever and gives up
        on the second. Answering all of them with one INFO line makes a
        locked-out rack log exactly what a wifi blip logs.

        True means "go straight to the cap". Not "stop": neither of these can
        self-heal, but both are fixed by a person -- a token minted in the
        interface, a package upgraded on one end -- and nothing else will fetch
        the daemon back afterwards. So it keeps dialling, once every
        `BACKOFF_CAP_S`, which is the least noise that still recovers on its own.
        Everything else is a server that is restarting, a socket that was
        superseded or a snapshot it could not build, all of which the ordinary
        backoff is exactly right for.
        """
        if closed.code == CLOSE_UNAUTHORIZED:
            log.error(
                "the server refused this daemon's credential; it needs pairing again",
                extra={"code": closed.code, "reason": closed.reason},
            )
            return True
        if closed.code == CLOSE_PROTOCOL_SKEW:
            # Both versions, because neither on its own tells anyone what to
            # upgrade: the server puts what it speaks in the close reason, and
            # what this build speaks is only knowable from here.
            log.error(
                "the server speaks a version of this protocol that this build does not; "
                "one end needs upgrading",
                extra={"protocol": PROTOCOL_VERSION, "server_said": closed.reason},
            )
            return True
        log.info("link down", extra={"code": closed.code, "reason": closed.reason})
        return False

    def _settle(self, lasted: float, *, hopeless: bool = False) -> None:
        """Decide how long to wait before dialling again.

        Judged on how long the connection *lasted*, not on whether it opened.
        Resetting the delay the moment a socket connects reads a refusal as a
        success: a server that closes on an unknown credential, on a protocol it
        does not share, or because another connection superseded this one,
        accepts the socket first -- and the reset clears `_backing_off` with it,
        so the close that follows takes the plain-floor branch rather than the
        doubling one. The delay never leaves the floor: a reconnect every second,
        forever, against a server that has already said no.

        A session that outlived one heartbeat interval is the evidence this end
        can get without a reply: it sent one and the server did not close on it.
        Derived from `heartbeat` rather than written as a number beside a
        comment, because that is the relationship, not a coincidence.

        The doubling then follows M2's poller exactly: the first retry after a
        working session is one plain floor, and only a failure that follows a
        failure doubles. Doubling the already-capped delay rather than raising a
        base to a power keeps it finite over a server that is down all weekend.

        `hopeless` short-circuits all of it -- see `_refused`. Checked first,
        because the point of it is that no amount of evidence from this
        connection is worth anything against a "no" the server has spelled out.
        """
        if hopeless:
            self._backing_off = True
            self.retry_in = self._backoff_cap
            return
        if lasted >= self.heartbeat:
            self._backing_off = False
            self.retry_in = self._backoff_floor
            return
        delay = self.retry_in * 2 if self._backing_off else self._backoff_floor
        self._backing_off = True
        self.retry_in = min(self._backoff_cap, delay)


class _SendWatchdog(threading.Thread):
    """Watches one socket for a write that has stopped making progress.

    It exists because there is nothing else that can. A thread parked in
    `socket.sendall` cannot time itself out, `websockets`' keepalive thread
    cannot reach it -- a ping needs `send_context()`, which blocks on the mutex
    the parked sender holds -- and no other thread can get past that mutex
    either. What is left is taking the file descriptor away, which is what
    `Connection.abort` does, and doing it from a thread that is not involved.

    One per connection rather than one per client, so it exists exactly while
    there is a socket to watch and `tick_once` cannot leave one behind. It waits
    on an event rather than sleeping, so stopping it is immediate and a shutdown
    does not pay the poll interval.

    Nothing watches *this* thread, and nothing needs to: it holds no lock, calls
    nothing that blocks, and if it died the failure it guards against is the one
    the link had before this class existed.
    """

    def __init__(self, client: LinkClient, connection: Any) -> None:
        super().__init__(name="link-send-watchdog", daemon=True)
        self._client = client
        self._connection = connection
        self._done = threading.Event()
        # Half the deadline, so a parked send is noticed between one and one and
        # a half of them -- close enough that the bound means something, coarse
        # enough that an idle link wakes this thread twice a heartbeat rather
        # than continuously. Floored so that a test's short deadline cannot ask
        # for a spin.
        self._poll = max(0.01, min(1.0, client._send_deadline / 2))

    def run(self) -> None:
        while not self._done.wait(self._poll):
            if self._client._overran():
                self._client._wedged(self._connection)
                return

    def stop(self) -> None:
        self._done.set()
        self.join()


class _Socket:
    """The real transport, with `websockets`' vocabulary translated into this one.

    Two things live here and nowhere else. The first is the close code: the
    server distinguishes "your credential is wrong" from "we do not speak the
    same protocol", and a daemon that cannot read the difference retries the one
    that will never work and treats the one that needs a person as a wifi blip.
    Reading it means catching `websockets.exceptions.ConnectionClosed`, and that
    type may not appear anywhere above this line, because everything above stays
    importable and testable on a machine with no `websockets` installed. So the
    exception class is handed in and the code comes back out as `LinkClosed`.

    The second is `abort`. `close()` performs a closing handshake, which needs
    the protocol mutex, which is precisely what a parked send is holding -- so
    the one method that could rescue a wedged link is the one method that cannot
    run. Taking the socket down directly is what actually frees it, and
    `Connection.socket` is documented for exactly this kind of thing.
    """

    def __init__(self, connection: Any, closed: type[BaseException]) -> None:
        self._connection = connection
        self._closed = closed

    def send(self, payload: str) -> None:
        try:
            self._connection.send(payload)
        except self._closed as exc:
            raise _closure(exc) from exc

    def recv(self, timeout: float | None = None) -> str:
        try:
            return str(self._connection.recv(timeout=timeout))
        except self._closed as exc:
            raise _closure(exc) from exc

    def close(self) -> None:
        self._connection.close()

    def abort(self) -> None:
        """Take the file descriptor away, from a thread that is not parked on it.

        `shutdown` before `close` because on Linux it is the shutdown that
        interrupts a peer thread already blocked in the kernel -- closing a
        descriptor another thread is sitting in does not wake it. It frees the
        reader as well as the writer, which is what is wanted: `websockets`'
        own receive thread ends, the protocol goes to closed, and the `recv`
        this module is serving raises instead of waiting for a server that is
        never going to be read from again.

        Both calls are guarded because both race an ordinary close: by the time
        this runs the socket may already be gone, and that is a success.
        """
        raw = self._connection.socket
        try:
            raw.shutdown(socket_module.SHUT_RDWR)
        except OSError:
            pass
        try:
            raw.close()
        except OSError:
            pass


def _closure(error: BaseException) -> LinkClosed:
    """A `websockets` close as this module's own.

    `rcvd` is the close frame the *server* sent, which is the one that carries
    the reason this daemon was turned away. It is None whenever no close frame
    arrived -- a TCP connection that dropped, or one this end closed first --
    and a missing code is the common case rather than an error, so it is carried
    through as None rather than guessed at.
    """
    received = getattr(error, "rcvd", None)
    return LinkClosed(
        code=getattr(received, "code", None),
        reason=str(getattr(received, "reason", "") or ""),
    )


def _elapsed(start: datetime, end: datetime) -> float:
    """Seconds between two readings of the clock, as elapsed rather than wall time.

    Both are converted to UTC first. Subtracting two aware datetimes that carry
    the same `tzinfo` object -- which two readings of one `system_clock` do --
    is documented to ignore it and answer the wall-clock difference, which is an
    hour out on the two nights a year the offset moves.
    """
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()


def _claimed_type(raw: str) -> str | None:
    """What an unparseable message says it is, if it says anything readable."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload.get("type") if isinstance(payload, dict) else None


def _version_of(raw: str) -> int:
    """The version a push claims, for answering one this build cannot parse.

    Nought when there is nothing readable there. It names no real push, which is
    what makes it the right answer: the server logs the refusal with the number
    it sent, and a number invented here would be worse than an obviously absent
    one. Every way JSON can disappoint is caught, `isinstance` included -- a
    top-level array has no `get`, and an `AttributeError` escaping into the
    receive loop would take the link down over a malformed message.
    """
    try:
        payload = json.loads(raw)
        return int(payload["version"]) if isinstance(payload, dict) else 0
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


def _why(failure: ValidationError) -> dict[str, str]:
    """A parse failure as a log field, without the payload that caused it."""
    return {"error": first_error(failure)}


def _mode_of(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _default_connect(url: str) -> _Socket:
    """The real transport: one blocking WebSocket connection, wrapped.

    `websockets.sync.client`, not the asyncio one, because this is a thread and
    the whole daemon is threads -- an event loop here would need its own thread
    to run in and a queue to talk to the rest through, for a socket that carries
    a message a minute. Both imports are inside the function so that everything
    above stays importable and testable on a machine with no `websockets`, and
    `ConnectionClosed` is handed to `_Socket` rather than named in an `except`
    clause up there for the same reason.

    Every keyword is passed explicitly. Their values, and what each one costs a
    rack, are on the constants above.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    return _Socket(
        connect(
            url,
            open_timeout=OPEN_TIMEOUT_S,
            close_timeout=CLOSE_TIMEOUT_S,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            max_size=MAX_MESSAGE_BYTES,
        ),
        ConnectionClosed,
    )
