"""The daemon's end of the link, and the rule that governs all of it.

*No failure of the server, of this socket or of the network may darken the
rack.* Everything below follows from that. The connection is a source of
configuration, not a dependency: a server that is down, unreachable, speaking a
protocol this build has never heard of, or refusing this daemon's credential
outright, is answered here with a log line and a retry, while four panels keep
drawing from the snapshot they already have. Nothing in this module raises into
the daemon, and the one thread it owns cannot end on its own.

Three things about the protocol are worth having in front of you before reading
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
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ors_schema.daemon import DaemonConfig
from ors_schema.errors import first_error
from ors_schema.link import (
    Ack,
    Command,
    ConfigPush,
    DaemonMessage,
    FramesRequest,
    Heartbeat,
    Hello,
    Nack,
    Paired,
    parse_server_message,
)
from pydantic import ValidationError

from ors_daemon import __version__
from ors_daemon.clock import Clock

log = logging.getLogger(__name__)

SnapshotHandler = Callable[[DaemonConfig, int], None]
CommandHandler = Callable[[Command], None]
FramesHandler = Callable[[FramesRequest], None]

DAEMON_PATH = "/ws/daemon"
"""The route `ors_server.link.ws_daemon` mounts. Appended to the server URL."""

HEARTBEAT_INTERVAL_S = 15.0
"""How often a daemon says it is still there, in seconds.

Three times the server's `SEND_TIMEOUT = 5.0`, and the floor is the interesting
half of that. The server bounds how long one send to a daemon may take before it
gives up on the socket; a daemon beating faster than that bound makes it
decoration, because the server would then be dropping peers that are merely
between heartbeats. At or above it, a late heartbeat is evidence of something.

The ceiling is what the message costs. Every non-frame message makes the server
write `daemon.last_seen`, which is one SQLite write per rack per beat -- four a
minute here, against a link that is otherwise silent for hours at a time. It is
not a liveness *detector*: the hub knows a rack is online because it is holding
its socket, and uvicorn's ping/pong notices a peer that has gone away. This is
the coarse "when did we last hear anything" the interface shows, so a beat every
fifteen seconds is as fine-grained as anything can read.

Deliberately unrelated to `RECV_TIMEOUT_S`, which is about shutdown latency.
"""

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


class LinkError(Exception):
    """The link cannot be attempted at all -- a server URL nothing can dial."""


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
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
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

    settings = LinkSettings(
        server_url=str(raw["server_url"]),
        cache_path=Path(raw.get("cache_path") or path.with_name("snapshot.json")),
        token=raw.get("token") or None,
        key=raw.get("key") or None,
        daemon_id=int(raw["daemon_id"]) if raw.get("daemon_id") is not None else None,
    )
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
    `ScreenWorker.heartbeat` carry for the supervisor's watchdog. Nothing
    watches this thread (there is nothing useful to do about a wedged link that
    reconnecting does not already do), so publishing a stamp nobody reads would
    be the mistake `Tunnel` records having made with its `ready` event.

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
        heartbeat: float = HEARTBEAT_INTERVAL_S,
        backoff_floor: float = BACKOFF_FLOOR_S,
        backoff_cap: float = BACKOFF_CAP_S,
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
        opened = self._clock()
        try:
            url = websocket_url(self.settings.server_url)
            connection = self._connect(url)
            with self._send_lock:
                self._connection = connection
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
            self._serve(connection)
        except Exception as exc:
            # Every way this can end arrives here, and none of them is worth a
            # traceback: a server that is down, a credential it refused, a
            # protocol it does not share and a wifi blip are all "try again".
            log.info("link down", extra={"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.connected = False
            with self._send_lock:
                self._connection = None
            self._settle(_elapsed(opened, self._clock()))
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    log.debug("closing a dead socket failed")

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
                connection.send(message.model_dump_json())
            except Exception as exc:
                # The receive loop is about to find the same thing out and end
                # the connection; there is nothing useful to do from here.
                log.debug("could not send", extra={"error": str(exc), "said": message.type})
                return False
            return True

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

    def _config(self, connection: Any, push: ConfigPush) -> None:
        """Apply a pushed configuration, or say why not. Always answers.

        The skip is what stops a reconnect from repainting a rack that is
        already showing the right thing, and it is safe because the server never
        mints one version for two configurations: `bump_config_version` moves the
        counter on every edit. The ack is sent either way -- the server clears
        what it believes a daemon has confirmed on every connect, so silence
        here reads as "still hasn't got it" and buys another push next time.
        """
        if push.version == self.config_version:
            log.info("already running this configuration", extra={"version": push.version})
            self._send(connection, Ack(config_version=push.version))
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
        """
        log.info("paired with the server", extra={"daemon": message.daemon_id})
        self.settings = replace(
            self.settings, key=message.key, token=None, daemon_id=message.daemon_id
        )
        self.config_version = None
        try:
            write_link_settings(self._settings_path, self.settings)
        except OSError as exc:
            # The key is good for this session either way, so the link stays up
            # and the rack keeps running -- but the next boot has nothing, and
            # recovering means minting a new token in the interface. It is the
            # worst thing that can quietly happen here, so it is an error.
            log.error(
                "could not save the key this daemon reconnects with; "
                "it will need pairing again after a restart",
                extra={"path": str(self._settings_path), "error": str(exc)},
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
        """Send on the socket this loop is serving, under the shared lock."""
        with self._send_lock:
            connection.send(message.model_dump_json())

    def _write_cache(self, push: ConfigPush) -> None:
        """Save what to boot from when the server is unreachable. Raises nothing.

        Atomic for the reason `write_status` is: a reader landing inside a plain
        write gets an empty file, and the reader here is the next boot of the
        rack. Failures are logged rather than raised because the configuration
        is already *running* by the time this is called -- losing the ack over a
        read-only disk would have the server re-push on every connect, which is
        a full repaint of the rack each time, to fix a problem the next boot has
        and this moment does not.
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

    def _settle(self, lasted: float) -> None:
        """Decide how long to wait before dialling again.

        Judged on how long the connection *lasted*, not on whether it opened.
        Resetting the delay the moment a socket connects reads a refusal as a
        success: a server that closes on an unknown credential, on a protocol it
        does not share, or because another connection superseded this one,
        accepts the socket first -- so the delay would be reset and then doubled
        once per attempt, settling at a constant two seconds and hammering a
        server that has already said no.

        A session that outlived one heartbeat interval is the evidence this end
        can get without a reply: it sent one and the server did not close on it.
        Derived from `heartbeat` rather than written as a number beside a
        comment, because that is the relationship, not a coincidence.

        The doubling then follows M2's poller exactly: the first retry after a
        working session is one plain floor, and only a failure that follows a
        failure doubles. Doubling the already-capped delay rather than raising a
        base to a power keeps it finite over a server that is down all weekend.
        """
        if lasted >= self.heartbeat:
            self._backing_off = False
            self.retry_in = self._backoff_floor
            return
        delay = self.retry_in * 2 if self._backing_off else self._backoff_floor
        self._backing_off = True
        self.retry_in = min(self._backoff_cap, delay)


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


def _default_connect(url: str) -> Any:  # pragma: no cover - exercised on the rack
    """The real transport: one blocking WebSocket connection.

    `websockets.sync.client`, not the asyncio one, because this is a thread and
    the whole daemon is threads -- an event loop here would need its own thread
    to run in and a queue to talk to the rest through, for a socket that carries
    a message a minute. It is imported inside the function so that everything
    above stays importable and testable on a machine with no `websockets`.
    """
    from websockets.sync.client import connect

    return connect(url)
