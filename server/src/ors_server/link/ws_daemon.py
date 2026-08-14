from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ors_schema.link import (
    PROTOCOL_VERSION,
    Ack,
    ConfigPush,
    DaemonMessage,
    Frame,
    Hello,
    Nack,
    Paired,
    parse_daemon_message,
)
from pydantic import ValidationError
from starlette.datastructures import State

from ors_server.db import Database
from ors_server.link.hub import Connection
from ors_server.pairing import authenticate_key, claim_token
from ors_server.snapshot import SnapshotError, build_snapshot

log = logging.getLogger(__name__)
router = APIRouter()

# The 4000-4999 range is reserved for the application, so these are ours to
# define and the daemon's to act on -- a client that cannot tell "your
# credential is wrong" from "your configuration is unservable" retries the first
# forever and gives up on the second. One code per reason, and every refusal
# below closes with exactly one of them.
CLOSE_MALFORMED = 4400
"""The first thing said on this socket was not a message this build knows."""
CLOSE_UNAUTHORIZED = 4401
"""No credential, or one that matches nothing. Deliberately one code for both.

Two codes would be a probe: whoever is scanning the network learns whether the
string they sent is a credential the server has heard of before, which is most
of the work of finding one that is.
"""
CLOSE_HELLO_FIRST = 4402
"""Something arrived before the socket said who it was."""
CLOSE_SLOW_HELLO = 4408
"""The socket was accepted and then said nothing. Mirrors HTTP 408."""
CLOSE_SUPERSEDED = 4409
"""The same daemon connected again and this socket is the old one."""
CLOSE_PROTOCOL_SKEW = 4426
"""The two ends do not speak the same version of this protocol. Mirrors HTTP 426.

Distinct from every other refusal here because a daemon can do something about
it and the something is not "retry": reconnecting against a build that cannot
understand you never gets anywhere, and the operator has to be told to upgrade
one end or the other.
"""
CLOSE_UNSERVABLE = 4500
"""The database holds a configuration this daemon could not be given."""

HELLO_TIMEOUT_S = 10.0
"""How long an accepted socket may say nothing before it is closed.

Nothing else ever wakes that handler. The daemon is under no obligation to speak
again once connected, uvicorn's ping timeout is about a peer that has gone away
rather than one that is deliberately quiet, and there is no credential yet to
hold anyone to account with -- so without this, anyone who can reach the port
parks a handler, a socket and a task by connecting, and can do it as many times
as the operating system will let them. Forty were held open at once.

Ten seconds because a daemon's `hello` is one small write it makes immediately,
with no work in front of it: what has to fit here is a congested LAN, a
reverse proxy, and a Pi that is still finishing its boot, not anything the
daemon computes. It is a liveness bound and not a rate limit -- a peer that
sends a well-formed `hello` with a wrong credential every ten seconds is still
free to, and bounding *that* means a connection cap, which is a piece of
infrastructure this route should not be growing on its own. Residual risk,
recorded rather than solved.
"""

OWNERSHIP_REFRESH_S = 5.0
"""How stale one connection's idea of which screens it owns may get.

The bound on `_owns`, and it has to be a bound rather than a memo of the ids
already refused. `screen_id` is an unbounded int chosen by the daemon: 500
frames naming 500 distinct ids produced 500 ownership queries -- synchronous
SQLite on the event loop -- 500 entries in a set nothing ever cleared, and 500
WARNING lines. A window costs one query and one line however many ids arrive.

Five seconds is the whole cost of the window as well: a screen moved to this
rack in the interface has its first frames dropped for up to that long, which
is ten frames at the 2 fps the daemon is asked for, against an edit whose
config push is in flight anyway. Shorter buys a promptness nobody can perceive;
much longer starts to look like the panel is broken.
"""


@dataclass
class _Session:
    """What the handler knows about the daemon on the other end of this socket.

    Per connection and not per daemon: a reconnect that overtakes this handler's
    exit is a different session with its own everything, and the `Connection` is
    what lets the hub tell the two apart.
    """

    daemon_id: int
    connection: Connection
    owned: set[int]
    """The screens this daemon may send frames for. See `_owns`."""
    refreshed_at: float | None = None
    """`time.monotonic()` when a frame last made `owned` be re-read, or None
    because no frame has yet named a screen that was not already in it."""
    refused: int = 0
    """Frames dropped since the last refusal was logged, including that one.

    A counter and not the set of ids, which is the difference between memory
    this connection's peer chooses the size of and memory it does not."""


@router.websocket("/ws/daemon")
async def daemon_socket(socket: WebSocket) -> None:
    """One daemon's link to the server, from hello to hang-up."""
    await socket.accept()
    state = socket.app.state
    session: _Session | None = None
    try:
        session = await _hello(state, socket)
        if session is None:
            return
        await _serve(state, socket, session)
    except WebSocketDisconnect:
        # The ordinary ending, not an error: a Pi rebooted, or the wifi went.
        log.info("a daemon socket closed", extra={"daemon": getattr(session, "daemon_id", None)})
    finally:
        if session is not None:
            state.hub.drop(session.connection)


async def _hello(state: State, socket: WebSocket) -> _Session | None:
    """Identify the daemon and bring it up to date, or close and return None."""
    try:
        raw = await asyncio.wait_for(socket.receive_text(), HELLO_TIMEOUT_S)
    except TimeoutError:
        # Nothing else would ever wake this handler: a peer that connects and
        # says nothing is not a peer that has gone away, so no ping timeout
        # applies, and there is no credential yet to hold it to account with.
        log.info("a socket was accepted and never said hello")
        await socket.close(code=CLOSE_SLOW_HELLO, reason="no hello")
        return None

    try:
        first = parse_daemon_message(raw)
    except ValidationError as error:
        # Closed rather than skipped, unlike a malformed message on an
        # established link: an unidentified socket has nothing worth keeping
        # open, and a peer that cannot manage a `hello` is not about to say
        # anything this server can act on.
        log.info("a socket opened and said something that is not a message", extra=_why(error))
        await socket.close(code=CLOSE_MALFORMED, reason="malformed")
        return None

    if not isinstance(first, Hello):
        log.info("a socket spoke before saying hello", extra={"said": first.type})
        await socket.close(code=CLOSE_HELLO_FIRST, reason="hello first")
        return None

    if first.protocol_version != PROTOCOL_VERSION:
        # Before the credential is looked at, because a message this build
        # cannot interpret is not one to act on whoever sent it -- and because
        # the daemon deserves the real answer rather than being turned away as
        # unauthorised for reasons that have nothing to do with its key. The
        # version is not a secret; it is a build number both ends already know.
        log.warning(
            "a daemon speaks a protocol version this server does not",
            extra={"claimed_protocol": first.protocol_version, "protocol": PROTOCOL_VERSION},
        )
        await socket.close(code=CLOSE_PROTOCOL_SKEW, reason=f"protocol {PROTOCOL_VERSION}")
        return None

    database: Database = state.database
    daemon_id, issued = _authenticate(database, first.token)
    if daemon_id is None:
        # The hostname is logged because it is the only thing here worth having
        # in a log, and it is worth remembering that it is also the only thing
        # in this message that was not checked. It identifies nobody.
        log.warning("refused a daemon socket", extra={"claimed_hostname": first.hostname})
        await socket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return None

    if issued is not None:
        # Down this socket by name rather than through the hub, and before the
        # connection is registered. The key belongs to the socket that spent the
        # token and to no other: routing it through the hub's by-id lookup would
        # send it to whichever connection the hub happens to hold for this
        # daemon, which during a reconnect race is somebody else's socket.
        #
        # It is sent exactly once, and if it is lost in flight the daemon is
        # left holding a spent token and cannot get back in. Recovering means
        # minting a new token from the interface -- the same act as rotating a
        # key, which the design already calls for.
        await socket.send_text(Paired(daemon_id=daemon_id, key=issued).model_dump_json())

    _record_hello(database, daemon_id, first)

    try:
        push = _push_for(state, daemon_id, first.config_version, pairing=issued is not None)
        owned = _owned_screens(database, daemon_id)
    except (SnapshotError, KeyError) as error:
        # Unhandled, this leaves the route by raising, which on a socket is a
        # close with no code the daemon can act on and a traceback in the log of
        # a server that was asked a perfectly ordinary question. It is a real
        # state: a column that is not JSON, an integration holding a credential
        # the wire format cannot carry, or a daemon deleted from the interface
        # between authenticating and reading its rows.
        log.error(
            "a connected daemon cannot be given its configuration",
            extra={"daemon": daemon_id, "error": str(error)},
        )
        await socket.close(code=CLOSE_UNSERVABLE, reason="no snapshot")
        return None

    # Registered only now, with everything already read: until this line the
    # hub still routes to whatever connection came before, so a daemon whose
    # configuration cannot be assembled never displaces a working one. Nothing
    # awaits between the read and the push either, so the snapshot cannot be
    # overtaken on the wire by an edit that was minted after it.
    session = _Session(
        daemon_id=daemon_id,
        connection=state.hub.register(daemon_id, socket.send_text),
        owned=owned,
    )
    try:
        if push is not None:
            await state.hub.push_config(daemon_id, push)
        await _resume_frames(state, session)
    except BaseException:
        # From here to `return`, this function owns the registration, and the
        # caller's `finally` cannot: `session = await _hello(...)` never
        # completes when this raises, so `daemon_socket` is left holding None
        # and drops nothing. A rack registered and then abandoned is one every
        # open tab shows as online -- `register` and `drop` announce set
        # *changes*, so its reconnect changes nothing anybody can see -- and
        # nothing will ever take it offline again.
        #
        # `BaseException` because a cancel is one of the ways out: a shutdown
        # landing between `register` and the return would otherwise leave the
        # same wreckage, and re-raising keeps it a cancel.
        state.hub.drop(session.connection)
        raise
    return session


def _authenticate(database: Database, credential: str) -> tuple[int | None, str | None]:
    """Who this is, and the key to hand back if this connect is what paired them.

    One field on the wire carries both credentials, so both are tried here. They
    cannot be confused within a row -- a token's hash is deleted the moment it is
    spent, and the schema's `CHECK` says a row holds one or the other -- so at
    most one column of any one row can match a string.

    The key is tried first, and the order matters for a reason beyond it being
    what all but the first connect of a daemon's life presents: claiming a token
    *spends* it. Trying the token first would make an ordinary reconnect capable
    of burning a pairing that belongs to another row, and the daemon holding
    that token could then never use it. Authenticate with the credential that
    has no side effects before reaching for the one that does.
    """
    daemon_id = authenticate_key(database, credential)
    if daemon_id is not None:
        return daemon_id, None
    claimed = claim_token(database, credential)
    if claimed is None:
        return None, None
    return claimed


def _push_for(
    state: State, daemon_id: int, running: int | None, *, pairing: bool
) -> ConfigPush | None:
    """What this daemon should be given now, or None because it already has it.

    The version compared against is the one on the row, read and not bumped. A
    connect changes no configuration, so it may not advance the counter -- and
    bumping here would defeat the comparison twice over: a number minted after
    the daemon spoke can never match what it claimed, so every reconnect would
    be a full teardown and repaint of the rack, and a flapping link would walk
    the counter up forever.

    `running` is a claim, not evidence, and it is only ever allowed to *skip* a
    push. Nothing here records it as acked: `Hub.register` has just cleared what
    this daemon had confirmed, and it stays cleared until a real `Ack` answers a
    real push. So a skipped push leaves the server saying "unknown", which is
    the conservative answer and the one that costs at most one extra push later.

    `pairing` is the one connect on which the claim is not admissible at all.
    The comparison asks "does this daemon already have what I would send?", and
    at the moment a token is spent the server knows outright that it has given
    this daemon nothing -- so there is nothing for the claim to be a claim
    about. It is also the connect where believing it is worst: a re-imaged Pi
    with a stale cache says 0, an untouched server is on the schema default of
    0, and nothing recovers, because the ack was just cleared and no push is
    retried until an unrelated edit bumps the counter. The rack sits blank
    against a server that believes it is up to date.

    Read the version before assembling the snapshot, never after. The number is
    then never newer than the rows it names, so an edit that lands in between
    mints a higher one and pushes its own; the other order would push stale rows
    under a number the daemon has already applied and the correction would be
    deduped away.
    """
    version = _config_version(state.database, daemon_id)
    if running == version and not pairing:
        return None
    return ConfigPush(
        version=version, snapshot=build_snapshot(state.database, state.secrets, daemon_id)
    )


async def _resume_frames(state: State, session: _Session) -> None:
    """Ask a reconnecting daemon for the frames a browser is still waiting on.

    Nothing else will: the interface subscribes when someone opens a screen, and
    if the Pi reboots after that, the watcher is left holding a queue nobody
    fills.

    `Hub.frames_for` is what assembles it, which is the same call the browser
    socket makes on every subscribe -- the intersection with what this daemon
    owns, and the bound on how many screens one request may name, are one rule
    and had been implemented here and there separately. This end had no bound at
    all, so a rack with more watched screens than the limit died in its own hello
    on a `ValidationError`, having already registered.

    Nothing is sent when the set is empty, unlike the browser socket's `_arm`: a
    daemon that has just connected is streaming nothing, so `enabled=False` here
    would be a message telling it to stop doing what it is not doing.
    """
    request = state.hub.frames_for(session.daemon_id, session.owned)
    if not request.enabled:
        return
    await state.hub.request_frames(session.daemon_id, request)


async def _serve(state: State, socket: WebSocket, session: _Session) -> None:
    """Read this socket until the daemon goes away or the hub replaces it."""
    # The hub holds a `send` callable, not a socket, so it cannot close this one
    # when a reconnect supersedes it -- it sets `Connection.closed` and leaves
    # the closing to whoever owns the socket, which is here. Without racing that
    # event, a superseded handler stays blocked in `receive` until uvicorn's
    # ping timeout tens of seconds later, and for all of that time it is still a
    # reader: still parsing messages, still relaying frames, still handing the
    # hub acks read from a daemon that has already gone.
    superseded = asyncio.create_task(session.connection.closed.wait())
    reader: asyncio.Task[str] | None = None
    try:
        while True:
            reader = asyncio.create_task(socket.receive_text())
            done, _ = await asyncio.wait({reader, superseded}, return_when=asyncio.FIRST_COMPLETED)
            if superseded in done:
                # Checked first, because both can finish in the same pass and a
                # message read from a connection the hub has already forgotten
                # must not be acted on.
                log.info("closing a superseded daemon socket", extra={"daemon": session.daemon_id})
                await socket.close(code=CLOSE_SUPERSEDED, reason="superseded")
                return
            raw = reader.result()
            try:
                message = parse_daemon_message(raw)
            except ValidationError as error:
                # One unreadable message is not worth a whole rack's link. The
                # alternative -- closing -- turns a single bad frame into a
                # reconnect loop that re-pushes the configuration every time
                # round, and every panel with it. It is safe to skip because of
                # what `extra="forbid"` and the tagged union buy: a malformed
                # message cannot be quietly mistaken for a well-formed one of
                # another type, so what is dropped here is exactly what was
                # unusable, and it is named in the log.
                log.warning(
                    "unreadable message from a daemon; skipped",
                    extra={"daemon": session.daemon_id, **_why(error), **_about(raw)},
                )
                continue
            await _handle(state, session, message)
    finally:
        # Both, on every way out of this function, and here rather than at each
        # exit because there are more of them than there look: `asyncio.wait`
        # cancels neither of the awaitables it was handed, so a handler
        # cancelled mid-race -- at shutdown, or under any outer deadline that is
        # ever put around this -- leaves its `receive_text()` pending, one per
        # connected rack. The superseded branch left one behind too.
        #
        # `_cancel` on a task that is already finished is a no-op that collects
        # its result, which is what the ordinary path through here wants: the
        # reader either returned the message just handled or raised the
        # disconnect on its way out, and either way something has to retrieve it.
        await _cancel(superseded)
        if reader is not None:
            await _cancel(reader)


async def _handle(state: State, session: _Session, message: DaemonMessage) -> None:
    if isinstance(message, Ack):
        # The `Connection`, not the id: this handler may be reading a socket the
        # hub has already replaced, and an ack recorded under the id would
        # describe the daemon's previous boot. The hub refuses that -- but only
        # if it is handed something whose identity it can check.
        state.hub.record_ack(session.connection, message.config_version)
    elif isinstance(message, Nack):
        # An error level, because a rack that refused its configuration is a
        # rack showing something other than what the interface says it shows,
        # and the server is where the person who just saved the edit is looking.
        log.error(
            "a daemon refused a snapshot",
            extra={
                "daemon": session.daemon_id,
                "version": message.config_version,
                "reason": message.reason,
            },
        )
    elif isinstance(message, Frame):
        # The refusal is logged inside `_owns`, which is the only thing that
        # knows how many frames a single line is standing for.
        if not _owns(state.database, session, message.screen_id):
            return
        await state.hub.relay_frame(message)
    else:
        log.debug(
            "a daemon said something",
            extra={"daemon": session.daemon_id, "said": message.type},
        )

    if not isinstance(message, Frame):
        # Every message is evidence the daemon is alive, but frames arrive
        # several times a second per screen and `last_seen` is a coarse field
        # nothing reads at that resolution -- the hub, not this column, is what
        # says whether a rack is online. A write per frame would put a SQLite
        # write on the event loop at 2 fps per panel and buy nothing.
        _touch(state.database, session.daemon_id)


def _owns(database: Database, session: _Session, screen_id: int) -> bool:
    """Whether this daemon may send frames for this screen.

    The only place in the system that can ask. `Hub.relay_frame` trusts
    `frame.screen_id` and reads no rows by design, so without this a daemon --
    or anyone holding a daemon's key -- can paint over a panel belonging to a
    rack it has nothing to do with, and every browser watching that screen shows
    it.

    Re-read on a miss rather than trusted from hello, because the set does go
    stale under a live connection: screens are added and moved between racks in
    the interface, which pushes a new configuration through the hub, and the hub
    knows nothing about this handler.

    The re-read is bounded by time and not by which ids have already been
    refused, and the difference is the whole of what this function gets wrong
    otherwise. `screen_id` is an unbounded int the daemon chooses, so a memo of
    refused ids is a query, a set entry and a log line per *distinct* id, all
    three sized by the peer -- 500 frames naming 500 ids produced exactly that.
    Worse, a memo consulted before the re-read never expires, so a screen
    legitimately handed to this daemon stayed blank for the rest of the
    connection while the log blamed the daemon for sending it. Re-reading on a
    schedule bounds the first problem and cannot have the second.

    A window of silence is the price, and it is paid in the right direction: a
    frame refused during it is a frame that would have been refused a moment
    earlier anyway, and one that should now be relayed is only late.
    """
    if screen_id in session.owned:
        return True

    now = time.monotonic()
    if session.refreshed_at is not None and now - session.refreshed_at < OWNERSHIP_REFRESH_S:
        # Inside the window: the table was read recently enough that reading it
        # again would say the same thing. Drop it, count it, and say nothing.
        session.refused += 1
        return False

    session.owned = _owned_screens(database, session.daemon_id)
    session.refreshed_at = now
    if screen_id in session.owned:
        return True

    session.refused += 1
    log.warning(
        "dropped frames for screens this daemon does not own",
        # The count is what makes one line worth as much as the flood it
        # replaces: without it, rate-limiting the log hides the volume, which is
        # the most interesting thing about a daemon sending ids that are not its.
        extra={"daemon": session.daemon_id, "screen": screen_id, "dropped": session.refused},
    )
    session.refused = 0
    return False


def _owned_screens(database: Database, daemon_id: int) -> set[int]:
    # `closing`, here and below, rather than `with database.connect()`: sqlite3's
    # own context manager commits a transaction and leaves the connection open,
    # so the obvious spelling would leak one per message read on this socket.
    with closing(database.connect()) as connection:
        return {
            int(row["id"])
            for row in connection.execute("SELECT id FROM screen WHERE daemon_id = ?", (daemon_id,))
        }


def _config_version(database: Database, daemon_id: int) -> int:
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT config_version FROM daemon WHERE id = ?", (daemon_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"no daemon {daemon_id}")
    return int(row["config_version"])


def _record_hello(database: Database, daemon_id: int, hello: Hello) -> None:
    """What the daemon says about itself, which is everything except who it is.

    `status` is deliberately not written here. Pairing is what sets it, and a
    hello that could set it too would be a daemon deciding its own standing with
    the server on the strength of having connected.
    """
    with closing(database.connect()) as connection:
        connection.execute(
            "UPDATE daemon SET version = ?, capabilities = ?, last_seen = ? WHERE id = ?",
            (
                hello.daemon_version,
                json.dumps(hello.capabilities),
                datetime.now(UTC).isoformat(),
                daemon_id,
            ),
        )


def _touch(database: Database, daemon_id: int) -> None:
    with closing(database.connect()) as connection:
        connection.execute(
            "UPDATE daemon SET last_seen = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), daemon_id),
        )


async def _cancel(task: asyncio.Task) -> None:
    """Cancel a task and collect it, so nothing is left for the loop to complain
    about at exit -- a cancelled receive whose result is never retrieved is an
    "exception was never retrieved" line in a log nobody can trace back."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _about(raw: str) -> dict[str, int]:
    """Which screen a message that would not parse was about, if it said.

    `_why` gives the field and the reason -- `frame.webp: bytes_too_long` -- and
    on a four-panel rack that is most of an answer and not all of one. An
    oversized frame is dropped here and the browser watching that screen is
    given no indication at all: the queue simply stops and the page keeps
    showing the last image it drew. So this log line is the only place in the
    system that can say *which* panel went quiet, and without the id nobody can
    tell a wedged encoder on one screen from four of them.

    Deliberately narrow. The payload is a peer's, and it has already failed
    validation, so nothing here trusts its shape: anything that is not an object
    with an integer `screen_id` produces no field rather than a guess. The
    second parse is bounded by `ws_max_size` like the first, and is only ever
    paid on a message that was already unusable.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    screen_id = payload.get("screen_id")
    # `bool` is an `int` in Python and `{"screen_id": true}` is not a screen.
    if not isinstance(screen_id, int) or isinstance(screen_id, bool):
        return {}
    return {"screen": screen_id}


def _why(failure: ValidationError) -> dict[str, str]:
    """A parse failure as a log field, without the payload that caused it.

    `str(error)` on a `ValidationError` includes the input it rejected, and the
    input on this socket is a `hello` carrying a credential.
    """
    return {
        "error": "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in failure.errors()
        )
    }
