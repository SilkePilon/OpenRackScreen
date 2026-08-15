"""Racks: pairing one, revoking its key, telling it something, and letting it go.

**Pairing happens in the browser and nowhere else.** There is no CLI for it by
design -- `POST /api/daemons` is what mints a token and shows it once, beside the
command that carries it to the Pi. `pairing.mint_token` says the same thing from
the other end.

Every route here is `async def`, the read-only-looking ones included, because
`Hub` is event-loop-affine and FastAPI runs a `def` route in a threadpool. The
natural shape of `GET /api/daemons` -- blocking `sqlite3` plus `hub.online_ids()`
-- is exactly the shape that breaks it: `online_ids` builds a set from a dict a
reconnect may be resizing, and the `RuntimeError: Set changed size during
iteration` that comes out is neither a `WebSocketDisconnect` nor a
`ValidationError`, so it escapes the daemon socket's handler and takes the whole
rack offline rather than failing one request. `test_api_routes.py` sweeps for it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from ors_schema.link import (
    MAX_SPI_HZ,
    MAX_WIRING_NUMBER,
    Command,
    DetectRequest,
    DetectResult,
    PanelCandidate,
    ProbeRequest,
    ProbeResult,
    not_a_flag,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import State

from ors_server.api.changes import change, config_error, one_row
from ors_server.link.hub import SEND_TIMEOUT
from ors_server.pairing import mint_token_on, rotate_key

log = logging.getLogger(__name__)
router = APIRouter(tags=["daemons"])

MAX_NAME = 64
"""How long a rack's name may be. It is a label in a sidebar, not a document."""

MAX_EVENTS_READ = 500
"""How many events one request may ask for. See `MAX_EVENTS_PER_DAEMON`."""


class NewDaemon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)


class DaemonView(BaseModel):
    """One rack, as the interface sees it.

    An explicit model rather than the row, and that is the security property
    rather than tidiness: `daemon` carries `token_hash` and `key_hash`, and the
    obvious `dict(row)` in a list comprehension puts both of them on the wire.
    FastAPI serialises through this model, so a column added to the table later
    cannot arrive in a response by accident either.
    """

    id: int
    name: str
    status: str
    online: bool
    config_version: int
    """The generation counter this server has minted for this rack's configuration.

    What was *sent*, or would be. Read beside `applied_version`, never instead of
    it: on its own it is a number the server invented, and reporting it as though
    the glass agreed with it is exactly the stale image this pair exists to make
    visible.
    """
    applied_version: int | None
    """The `config_version` this rack has confirmed applying, or None if unknown.

    The other half of the only question a person asks about an edit they just
    saved: did the Pi apply what I sent? `config_version` alone cannot answer it
    -- a rack that nacked a snapshot, or that never received one, reports the
    version the server minted and goes on drawing the previous configuration,
    which is a stale image the interface reports as current.

    **None is "no rack has told me", not "old".** It is what an offline rack, a
    rack that has not yet answered its first push, and a rack that has just
    reconnected all report, and the honest rendering of it is "unknown".

    **Read from the hub and deliberately not stored on the row.** An ack is
    evidence about a socket the server is holding *now*, and `Hub.register`
    clears it on every connect precisely because a Pi that reboots comes back
    with nothing while the server still remembers it confirming version 7.
    Writing it to the row would put that stale claim back, and would do it in
    the one case somebody is reading this page to diagnose. It would also need
    invalidating from `rotate-key`, from `delete`, and from anything else that
    changes what a rack is -- a second source of truth for a question only the
    socket can answer.

    What that costs is a server restart: the hub starts empty, so every rack
    reads unknown until it says otherwise. It says otherwise within one backoff,
    because a daemon re-acks the version it is running on every connect (see
    `ors_daemon.link.LinkClient._reack`, and spec section 8's "daemons reconnect
    and re-ack their version"). Refilled from the racks is a better answer than
    remembered from before the restart, because it is true.
    """
    config_error: str | None
    """Why this rack cannot be given a configuration, or None because it can.

    The blank-rack signal. Four dark panels otherwise look exactly like four
    panels waiting for data, and the only other evidence is a line in the
    server's log. It is computed per request rather than stored -- see
    `changes.config_error`.
    """
    version: str | None
    capabilities: dict[str, Any]
    last_seen: str | None
    paired_at: str | None
    created_at: str


class DaemonCreated(BaseModel):
    """A rack and the token that pairs it, which is shown exactly once.

    Separate from `DaemonView` so that `token` cannot be answered by any route
    that lists: the field does not exist on the model the list is built from.
    """

    id: int
    name: str
    token: str


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: Literal["identify", "sleep", "wake", "reload"]
    screen_id: int | None = None
    """Which panel, or None for the whole rack. Not a flag -- see `not_a_flag`.

    The schema's own rule, called rather than copied, and it has to be here as
    well as on `Command`: this model is *upstream* of that one. `bool` is an
    `int` and pydantic's lax mode takes it as one, so `{"screen_id": true}`
    arrived here as `1`, and by the time `Command`'s validator saw the value it
    was an ordinary integer -- 200 on the wire, `screen_id: 1`, and `sleep`
    darkening whichever panel holds row id 1 on somebody else's rack.
    """

    @field_validator("screen_id", mode="before")
    @classmethod
    def _screen_id_is_a_row_id(cls, screen_id: object) -> object:
        return not_a_flag(screen_id)


class Delivered(BaseModel):
    delivered: bool
    """Whether the message really left the server, which is not the same question
    as whether a socket was open.

    `Pushed.delivered` learned this the hard way: `hub.is_online` is true of a
    rack whose send then times out and is dropped, so a route answering that
    question reported `true` with nothing on the wire. This is about the send.
    """


NO_MECHANISM = {
    "sleep": "a screen sleeps because it is inside this rack's night window or its own "
    "sleep_override, and nothing else puts one to sleep. Set one with "
    "PATCH /api/screens/{id} or PATCH /api/settings",
    "wake": "a screen wakes when it leaves this rack's night window or its own "
    "sleep_override, and nothing else wakes one. Set one with "
    "PATCH /api/screens/{id} or PATCH /api/settings",
    "reload": "a paired rack's configuration comes from this server rather than from a file "
    "on the Pi, so there is nothing local for it to re-read. "
    "POST /api/daemons/{id}/push sends it again",
}
"""The commands the protocol carries that this build cannot make a rack do.

`ors_schema.link.Command` names four, and one of them is implemented: the daemon
answers `identify` by painting each panel's ordinal on it, which is what the
setup wizard needs and what the milestone's definition of done names. The other
three are refused here rather than sent, because there is no mechanism behind
them on the Pi -- `ors_daemon.__main__._command_handler` carries the long form of
why each one is a design question rather than a wiring one.

Refused with 501 rather than 422. It is not the caller's mistake: the request is
well formed, the command is one the protocol defines, and what is missing is on
this side. 422 would say "you typed it wrong" about a message a newer build will
accept unchanged.

Each reason names the thing that *does* work, because a refusal that only says
no leaves the reader waiting for a button that is not coming.

Kept as a denial list rather than narrowing `CommandBody.command` to `identify`,
so that this file and `Command` hold one vocabulary between them rather than two
that can drift -- and so that the reason travels with the answer, which a
validation error on a `Literal` cannot carry.
"""


class Pushed(BaseModel):
    version: int
    delivered: bool


class Deleted(BaseModel):
    deleted: int


DETECT_TIMEOUT = 2 * SEND_TIMEOUT
"""How long a rack has to answer a detect before the caller is told it did not.

Ten seconds, and deliberately not `REQUEST_TIMEOUT`'s forty. That default has to
cover the worst *legal* case on the wire, which is a probe holding a panel lit
for thirty seconds; a detect lists `/dev/spidev*` and reads the running
configuration, so what is being waited on here is a round trip and a directory
listing. `SEND_TIMEOUT` twice over is the same shape `REQUEST_TIMEOUT` is built
from -- one for the question to reach a socket, one for the answer to come back
-- and it is what an operator pressing Detect on an interface is prepared to sit
through.

Passed explicitly rather than defaulted, which is what `Hub.request` takes a
`timeout` parameter *for*: the two callers below are not asking the same kind of
question, and a rack that has stopped answering should not hold a request
handler open for forty seconds over a question it could have answered in one.
"""

MAX_HOLD_S = 5.0
"""The longest hold this route will ask a rack for, in seconds.

**Not `MAX_PROBE_HOLD_S`, which is thirty.** The wire allows thirty and the
daemon silently cuts to five: `ors_daemon.supervisor.PROBE_HOLD_BUDGET` is 5.0,
a request past it is truncated rather than refused, and `ProbeResult` carries no
field saying it was. So a request for ten seconds is answered `ok: true` by a
rack that lit the panel for five -- and an interface counting down ten seconds
has an operator looking up at second six, seeing dark glass, and answering "no,
nothing lit" about wiring that is fine. That is a wrong answer about hardware,
arrived at from two correct components, and this bound is where it is stopped:
refused at the edge, where there is still somebody to tell.

Written down here rather than imported, because `ors-server` may not import
`ors-daemon` -- the same arrangement `MAX_SCREEN_NAME` and
`ors_server.api.screens.MAX_NAME` already have, and it holds for the same reason:
the two ends have to agree, and a number that could not be honoured cannot
honestly be offered. Whoever changes `PROBE_HOLD_BUDGET` changes this, and
`test_api_detect.py` states the pair together so the disagreement has somewhere
to fail.
"""

PROBE_TIMEOUT = MAX_HOLD_S + 2 * SEND_TIMEOUT
"""How long a rack has to answer a probe. Fifteen seconds.

`REQUEST_TIMEOUT`'s own formula, with the hold this route will actually ask for
in place of the largest the wire permits: the daemon replies once the hold is
over, so the wait has to outlast the hold and the two sends around it. Bounding
it here rather than taking the forty-second default is what keeps a rack that
went quiet from parking a request handler for four times as long as the longest
probe it could have been running.
"""


class ProbeBody(BaseModel):
    """The wiring an operator typed in, for one candidate panel.

    Every field is required and none has a default, which is the point of the
    endpoint: `DisplayConfig` defaults `spi_bus` and `spi_cs` to 0 and `hz` to
    40 MHz, and a probe that filled a field in from a default would be proving
    wiring nobody chose and reporting it as the wiring that was asked for. What
    is being tested is the guess that is about to be saved, whole.

    `extra="forbid"` for `PanelCandidate`'s reason inverted: a wizard sending
    `spi_bus` or `pattern` has to be told, rather than have the field ignored and
    the probe run against something else.
    """

    model_config = ConfigDict(extra="forbid")

    bus: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    cs: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    dc: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    rst: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    hz: int = Field(ge=1, le=MAX_SPI_HZ)
    hold_s: float = Field(ge=0.0, le=MAX_HOLD_S, allow_inf_nan=False)
    """Seconds to leave the pattern up, at most `MAX_HOLD_S`. Zero is allowed:
    a script does not look."""

    @field_validator("bus", "cs", "dc", "rst", "hz", mode="before")
    @classmethod
    def _wiring_is_a_number_not_a_flag(cls, value: object) -> object:
        # `ProbeRequest` has this rule and it has to be here as well, for
        # `CommandBody.screen_id`'s reason: this model is *upstream* of that one.
        # `bool` is an `int` and pydantic's lax mode takes it as one, so
        # `{"bus": true}` would arrive at `ProbeRequest` as an honest 1 -- and 1
        # is a plausible bus, chip select, GPIO line and clock, so the probe
        # would light some other device and report truthfully on whatever it lit.
        return not_a_flag(value, what="a bus, chip select, pin or clock is a number")

    @field_validator("hold_s", mode="before")
    @classmethod
    def _a_hold_is_a_duration_not_a_flag(cls, value: object) -> object:
        # Its own validator rather than a sixth name above, because the reason is
        # what an operator reads: a `hold_s` refused with "a bus, chip select,
        # pin or clock is a number" would be a correct refusal carrying a false
        # reason.
        return not_a_flag(value, what="a hold is a duration in seconds")


class Detected(BaseModel):
    """What one rack answered a detect with.

    An object rather than a bare list, so that a later field -- when the daemon
    can say something about a device beyond its number -- is an addition rather
    than a change of shape for every caller.

    `PanelCandidate` is reused rather than restated, unlike `DaemonView`, and the
    difference is what each model is protecting against. `DaemonView` exists
    because the row behind it carries `token_hash` and `key_hash`; this one
    carries three numbers off the wire that were built for a person to read, and
    two declarations of the same three fields is how the interface and the daemon
    come to disagree about `claimed_by`.
    """

    panels: list[PanelCandidate]


class Probed(BaseModel):
    """Whether the rack could drive that wiring, and what stopped it if not.

    `ok` is the verdict and it means "the device opened and the pattern was
    written", never "the operator saw it" -- only the person in front of the rack
    can answer that, which is why the wizard asks them afterwards rather than
    trusting this.

    A failed probe is a 200 carrying `ok: false`, not an HTTP error. The probe
    ran, the rack answered, and the answer is no: "SPI2.5 is already driving the
    screen 'CPU'" is as useful a result as a panel lighting up, and it is a fact
    about the rack's wiring rather than about this request. The statuses below
    are kept for the things that are: no rack, no answer, or a second probe on a
    bus that is already held.
    """

    ok: bool
    error: str | None
    """The rack's own words for why it failed, or None. Commentary, never the
    verdict: `ProbeResult` deliberately permits `ok=False` with no reason and
    `ok=True` with one, because a validator tying the pair would turn a daemon
    that forgot the sentence into a probe that answers with a *timeout*."""


class EventView(BaseModel):
    id: int
    daemon_id: int | None
    at: str
    level: str
    kind: str
    message: str


@router.get("/daemons")
async def list_daemons(request: Request) -> list[DaemonView]:
    state = request.app.state
    with closing(state.database.connect()) as connection:
        rows = connection.execute("SELECT * FROM daemon ORDER BY id").fetchall()
    return [_view(state, row) for row in rows]


@router.post("/daemons", status_code=201)
async def create_daemon(request: Request, response: Response, body: NewDaemon) -> DaemonCreated:
    """Mint a rack and the one-time token that pairs it. The token is shown here
    and never again, from this or any other route.

    The row is written **inside** the transaction, on the edit's own connection.
    Minted outside it, a `change` that then raised would leave a `daemon` row
    holding a token nobody was ever shown -- and `daemon.name` is UNIQUE, so that
    name would be occupied forever and every retry would answer 409, with no
    event, because the event is the thing that was rolled back.
    """
    try:
        async with change(request, response) as edit:
            daemon_id, token = mint_token_on(edit.connection, body.name)
            # Nothing to push, and no version to mint: a rack that has just been
            # created has no screens, no socket and no configuration that has
            # changed. The counter is the generation of a *configuration*, and
            # moving it here would start every rack at 1 for an edit that touched
            # nothing.
            edit.affects_nobody()
            edit.record(daemon_id, "info", "created", f"minted a pairing token for {body.name!r}")
    except sqlite3.IntegrityError as error:
        # `daemon.name` is UNIQUE. A 409 rather than a 500, because two racks
        # called "pi-rack" is a thing a person does, not a thing that goes wrong.
        # Caught around the whole block rather than around the INSERT: `change`
        # rolls back and re-raises, so this is where the error arrives.
        raise HTTPException(
            status_code=409, detail=f"a daemon named {body.name!r} exists"
        ) from error
    return DaemonCreated(id=daemon_id, name=body.name, token=token)


@router.post("/daemons/{daemon_id}/rotate-key")
async def rotate_daemon_key(request: Request, response: Response, daemon_id: int) -> DaemonCreated:
    """Revoke this rack's key and mint a new pairing token for the same row.

    The action a leaked key needs, and until it existed there was none: the key
    a daemon presents on every connect lives as long as the pairing, and the
    only way to invalidate it was to delete the row -- which cascades the rack's
    screens away, so recovering from a leak meant re-entering the whole
    configuration.

    The row is left `unpaired` holding a token and no key, which is the state a
    never-paired rack is in. Clearing the key is not optional: the schema's
    `CHECK (token_hash IS NULL OR key_hash IS NULL)` refuses a row holding both,
    and that constraint exists precisely because this is the code most likely to
    write a token over a live key.

    A connected daemon is *not* dropped here. It holds a socket the hub is
    routing to and a key the database has forgotten, so it stays up until it
    next reconnects and cannot authenticate -- which is what a rotate button
    should mean: the rack keeps working until somebody re-pairs it, rather than
    going dark the moment the button is pressed.
    """
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT name FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
        token = rotate_key(edit.connection, daemon_id)
        # No rack to tell, its own included. A rotation changes credentials and
        # no configuration at all, so minting a version would be a number for a
        # change that did not happen -- and the push it caused would go down a
        # socket whose credential the database has just forgotten.
        edit.affects_nobody()
        edit.record(daemon_id, "warning", "rotate-key", "the key was revoked and a token minted")
    log.warning("a daemon key was rotated", extra={"daemon": daemon_id})
    return DaemonCreated(id=daemon_id, name=row["name"], token=token)


@router.delete("/daemons/{daemon_id}")
async def delete_daemon(request: Request, response: Response, daemon_id: int) -> Deleted:
    """Delete a rack and, by the schema's cascade, everything that was on it.

    `screen`, `integration` and `daemon_event` all reference `daemon(id) ON
    DELETE CASCADE`, which SQLite honours only because `Database.connect` turns
    `PRAGMA foreign_keys` on -- it is per-connection and off by default, and
    without it this leaves orphaned screens rather than an error.

    **`secret` is the one table the cascade does not reach**, and it holds the
    ciphertext. It has no `daemon_id` -- it is referenced the other way, by
    `integration.secret_id`, which is `ON DELETE SET NULL` and so protects the
    integration from a deleted secret and does nothing in this direction. So the
    integration rows go and their credentials stay: unreachable through any
    route, undeletable, and in every export of the database from then on.
    `delete_integration` says the same thing about the same column; this is that
    leak reached one level up, and it has to be forgotten *before* the delete,
    while there is still a row naming which secrets were this rack's.
    """
    state = request.app.state
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT name FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
        for secret_id in _credentials_of(edit.connection, daemon_id):
            state.secrets.delete_on(edit.connection, secret_id)
        edit.connection.execute("DELETE FROM daemon WHERE id = ?", (daemon_id,))
        # The one edit with nothing to push: the rack it changed is gone, and
        # minting a version for a row that no longer exists is a `KeyError`.
        edit.affects_nobody()
        # Against no rack, which is not the same as against none: the row this
        # names has just been deleted, and `daemon_event.daemon_id` cascades, so
        # an event recorded against it would be removed by the statement above.
        # `daemon_id` is nullable for exactly this. The most destructive action
        # in the API was the one action the status panel could not show.
        edit.record(None, "warning", "deleted", f"the rack {row['name']!r} was deleted")
    log.info("a daemon was deleted", extra={"daemon": daemon_id})
    return Deleted(deleted=daemon_id)


def _credentials_of(connection: sqlite3.Connection, daemon_id: int) -> list[int]:
    """The `secret` rows this rack's integrations are the only reference to."""
    return [
        int(row["secret_id"])
        for row in connection.execute(
            "SELECT secret_id FROM integration WHERE daemon_id = ? AND secret_id IS NOT NULL",
            (daemon_id,),
        )
    ]


@router.post(
    "/daemons/{daemon_id}/command",
    responses={
        409: {"description": "the rack is not connected, and a command cannot be deferred"},
        501: {"description": "this build has no mechanism behind that command; see NO_MECHANISM"},
    },
)
async def send_command(request: Request, daemon_id: int, body: CommandBody) -> Delivered:
    """Say something to a rack that is listening. Changes nothing on the server.

    Deliberately not a `change`: a command mints no version and pushes no
    snapshot. Bumping here would mean pressing identify tore down and repainted
    every panel on the rack.

    **Every way this route can fail to do what it was asked is an answer that
    says so**, because the one thing it may not be is a button that lies, and it
    was one three times over.

    *A command with nothing behind it* is a 501 and is not sent. Three of the
    four the protocol carries have no mechanism on the Pi; `NO_MECHANISM` names
    them and what to use instead. Answered first, before anything is read: what
    this build implements is not a fact about which rack was named, and a
    request that answers 404 for one id and 501 for another has told the caller
    nothing it can act on.

    *A screen this rack does not own* is a 404 and is not sent. `screen_id` is a
    row id in a table that holds every rack's screens, and `not_a_flag` only
    stops `true` from becoming row 1 -- an honest integer naming somebody else's
    panel went out unchallenged, was ignored by a daemon that does not have it,
    and was reported delivered.

    *A rack that is not connected* is a 409, which is the opposite of what an
    edit does. `Hub.push_config` drops a send to an offline daemon in silence
    because the edit is saved and pushed on reconnect -- there is nothing about
    `identify` that survives being deferred by an hour.

    *A send that did not get out* is `delivered: false`. `is_online` is true of a
    rack that is TCP-alive and has stopped reading, whose send the hub then times
    out and drops; asking the socket rather than the send is how
    `POST /api/daemons/{id}/push` came to report a success with zero bytes on the
    wire.
    """
    reason = NO_MECHANISM.get(body.command)
    if reason is not None:
        raise HTTPException(status_code=501, detail=f"{body.command}: {reason}")

    state = request.app.state
    with closing(state.database.connect()) as connection:
        one_row(
            connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
        if body.screen_id is not None:
            one_row(
                connection,
                "SELECT id FROM screen WHERE id = ? AND daemon_id = ?",
                (body.screen_id, daemon_id),
                missing=f"daemon {daemon_id} has no screen {body.screen_id}",
            )
    if not state.hub.is_online(daemon_id):
        raise HTTPException(status_code=409, detail=f"daemon {daemon_id} is not connected")
    delivered = await state.hub.send_command(
        daemon_id, Command(command=body.command, screen_id=body.screen_id)
    )
    return Delivered(delivered=delivered)


@router.post("/daemons/{daemon_id}/push")
async def push_now(request: Request, response: Response, daemon_id: int) -> Pushed:
    """Send this rack its configuration again, whatever either end believes.

    The way out of a blank rack. `Hello.config_version` is a claim the server
    cannot verify and is only ever allowed to *skip* a push, so a daemon
    claiming a version it does not really have is sent nothing and stays blank
    until an unrelated edit bumps the counter -- and editing a screen you did not
    want to change is not a recovery procedure.

    It **mints a new version** rather than re-sending the current one, and that
    is the whole reason it works. `ors_daemon.link._config` answers a push whose
    version equals the one it believes it is running with an ack and no apply,
    so re-sending the number the daemon is lying about would be answered exactly
    as the skip was. A number it has never seen cannot be deduped.

    `delivered` is about the send and not about the socket. A rack whose
    committed configuration is unservable has no snapshot to send, so nothing is
    attempted -- and `hub.is_online` is true of it anyway, which is how this
    answered `{"version": 2, "delivered": true}` with nothing on the wire. This
    is the one button that exists to dig a blank rack out of a stuck state, so it
    is the last place that may claim a success that did not happen; the 202 and
    `UNSERVABLE_HEADER` beside it say why.
    """
    async with change(request, response) as edit:
        one_row(
            edit.connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
        edit.affects(daemon_id)
        edit.record(daemon_id, "info", "push", "the configuration was pushed by hand")
    return Pushed(
        version=edit.versions[daemon_id],
        delivered=edit.delivered.get(daemon_id, False),
    )


@router.post(
    "/daemons/{daemon_id}/detect",
    responses={
        502: {"description": "the rack answered something that was not a detect result"},
        503: {"description": "the rack is not connected, and only the rack knows its hardware"},
        504: {"description": "the rack is connected and did not answer in time"},
    },
)
async def detect_panels(request: Request, daemon_id: int) -> Detected:
    """Ask a rack what SPI devices it has, and who is already driving each one.

    The first question the add-screen wizard asks, and nothing in this server can
    answer it: which `/dev/spidev*` a Pi exposes is a fact about that Pi, and
    which of them a running worker is mid-frame on is a fact about that Pi's
    supervisor. So this asks, and waits.

    Changes nothing, and is not a `change`: it lists a directory on somebody
    else's machine. Detection is also **not rate-limited** -- two operators
    detecting at once is two directory listings -- which is exactly where it
    parts company with the probe below.

    **Three ways there is no answer, and they are three different answers.** A
    rack that is not connected is a 503, because the thing to do is start the
    daemon. A rack that is connected and silent is a 504, because the thing to do
    is look at what it is busy with. A rack that answers the *other* question is a
    502: `Hub.deliver_reply` matches the id and the rack, and nothing on the wire
    says which question a reply belongs to, so a `ProbeResult` carrying this
    request's id resolves this wait -- and it has no `panels`. Answering that with
    a 500 would report a rack's confusion as this server's.
    """
    state = request.app.state
    with closing(state.database.connect()) as connection:
        one_row(
            connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
    reply = await state.hub.request(
        daemon_id,
        DetectRequest(request_id=_question_id("detect")),
        timeout=DETECT_TIMEOUT,
    )
    if reply is None:
        raise _no_answer(state, daemon_id, "detect", DETECT_TIMEOUT)
    if not isinstance(reply, DetectResult):
        raise _wrong_answer(daemon_id, "detect", reply)
    return Detected(panels=reply.panels)


@router.post(
    "/daemons/{daemon_id}/probe",
    responses={
        409: {"description": "that rack is already probing a panel; see one_probe_at_a_time"},
        502: {"description": "the rack answered something that was not a probe result"},
        503: {"description": "the rack is not connected, and a probe cannot be deferred"},
        504: {"description": "the rack is connected and did not answer in time"},
    },
)
async def probe_panel(request: Request, daemon_id: int, body: ProbeBody) -> Probed:
    """Light one candidate panel with the wiring an operator supplied, and hold it.

    The other half of detection, and the half that touches hardware: enumeration
    cannot discover `dc` and `rst` -- a GC9A01 has no readable id over 4-wire SPI
    and the GPIO lines were chosen with a screwdriver -- so this is the guess
    being tested, and the proof is a human seeing the glass come on.

    **`hold_s` is bounded at `MAX_HOLD_S`, which is 5.0 and not the wire's 30.**
    The daemon cuts a longer hold to five and says nothing about having done it,
    so a countdown longer than five seconds asks an operator about a panel that
    is already dark. Whatever builds an interface on this route may not count
    past that number; the constant's docstring is the whole argument.

    **One probe at a time per rack** (spec 6.4), which is `one_probe_at_a_time`
    and lives here rather than in the hub: it is a rule about a *bus*, and the
    hub deliberately lets one rack hold several waits at once. A second probe is
    refused with a 409 and a sentence, because the alternative -- two panel inits
    interleaved on one bus -- is the failure the daemon's bus guard exists to
    prevent, and because an operator standing at the rack needs to be told which
    of the two lit panels they are looking at.

    `ok: false` is a **200**. The rack answered; the answer is no. The statuses
    are for the questions this server could not get answered at all -- see
    `detect_panels` for the 503/504/502 split, which is the same one.
    """
    state = request.app.state
    with closing(state.database.connect()) as connection:
        one_row(
            connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
    with one_probe_at_a_time(state, daemon_id):
        reply = await state.hub.request(
            daemon_id,
            ProbeRequest(
                request_id=_question_id("probe"),
                bus=body.bus,
                cs=body.cs,
                dc=body.dc,
                rst=body.rst,
                hz=body.hz,
                hold_s=body.hold_s,
            ),
            timeout=PROBE_TIMEOUT,
        )
    if reply is None:
        raise _no_answer(state, daemon_id, "probe", PROBE_TIMEOUT)
    if not isinstance(reply, ProbeResult):
        raise _wrong_answer(daemon_id, "probe", reply)
    return Probed(ok=reply.ok, error=reply.error)


@contextmanager
def one_probe_at_a_time(state: State, daemon_id: int) -> Iterator[None]:
    """Spec 6.4's rule, and the only place it is implemented.

    A probe is a real panel init -- roughly 160 ms of resets and register writes,
    then a frame -- and it takes the daemon's bus guard for its whole hold, so
    every worker sharing that bus is held off it. Two at once on one rack is two
    inits interleaved on one bus, which is M2's defect returning by another road.

    Kept as a set of rack ids on `app.state` rather than a lock, because the
    answer to the second caller is a **refusal with a reason** and not a queue. A
    lock would leave the second operator waiting out the first one's hold and
    then lighting a panel nobody was watching for; `Hub.request`'s `None` would
    make it a timeout, which is the wrong report for somebody standing in front
    of the rack.

    Safe without any lock of its own for the reason the hub is: the test and the
    add below have no await between them, and every caller is an `async def`
    route on the one event loop. A `def` route would break that, and the sweep in
    `test_api_routes.py` is what stops one being written.

    Released in a `finally`, so every way out gives the rack back: the answer, a
    timeout, a rack that vanished, a cancelled caller -- and a 502 raised on the
    reply is deliberately outside the block, so the rack is free before this
    server starts composing an error about it. Held instead of released, one
    silent probe would refuse every later probe on that rack until a restart.
    """
    try:
        probing: set[int] = state.probing_racks
    except AttributeError:
        # First probe of this process, on this app. Created here rather than in
        # `create_app` so that the rule and its storage are one thing to read;
        # `state` is per app, which is what keeps a dozen apps in one test run
        # from sharing a rack id. Reached by name and not through `getattr`,
        # which the router sweep in `test_api_routes.py` forbids outright --
        # a dynamic lookup makes its "no router pushes on its own" check blind.
        #
        # `probing_racks` rather than `probing`, because `state.probe` next door
        # is the Prometheus query function, and two attributes a letter apart on
        # one object is a mix-up waiting for whoever reads this next.
        probing = state.probing_racks = set()
    if daemon_id in probing:
        raise HTTPException(
            status_code=409,
            detail=f"daemon {daemon_id} is already probing a panel, and a probe holds that "
            f"rack's SPI bus for up to {MAX_HOLD_S:g}s. Wait for it to finish and try again",
        )
    probing.add(daemon_id)
    try:
        yield
    finally:
        probing.discard(daemon_id)


def _question_id(question: str) -> str:
    """A correlation id for one question, unique across every rack and every
    question this server has ever asked.

    `uuid4` and not a counter, per rack or otherwise. `Hub.deliver_reply` matches
    an id *and* the rack it came back from, so a collision across racks is already
    refused -- but `Hub.request`'s cleanup removes its entry by identity precisely
    because it cannot trust these to be unique, and a reused id there takes a live
    wait out from under a caller whose answer is still on its way. The prefix is
    for whoever is reading the log beside a Pi's; 39 characters, against
    `MAX_REQUEST_ID`'s 64.
    """
    return f"{question}-{uuid4().hex}"


def _no_answer(state: State, daemon_id: int, question: str, timeout: float) -> HTTPException:
    """503 or 504 for a question that came back with nothing.

    `Hub.request` answers `None` to four different things -- no socket, a send
    that never left, nobody answering in time, and a rack that went away
    mid-question -- and that is the right signature for the hub and the wrong
    answer for a person: "the rack is not plugged in" and "the rack is connected
    and ignoring me" send somebody to different rooms.

    **Asked after the call and not before it, and that is the race.** A check in
    front of the question is a fact about the moment before it was asked: a rack
    that disconnects while the question is in flight would be reported as a
    timeout, and the operator would go looking for a busy daemon on a Pi that is
    powered off. The hub abandons every wait belonging to a connection it drops,
    so `None` arrives immediately in that case and this reads the state that is
    true *now* -- which is the state the answer is about. The mirror case is
    honest too: a rack that dropped and reconnected inside one question is online
    here and is told it did not answer, which is exactly what happened.
    """
    if not state.hub.is_online(daemon_id):
        return HTTPException(
            status_code=503,
            detail=f"daemon {daemon_id} is not connected, so nothing could ask it to {question}. "
            "Only the rack knows its own hardware: start the daemon on the Pi and watch "
            "GET /api/daemons for it to come online",
        )
    return HTTPException(
        status_code=504,
        detail=f"daemon {daemon_id} is connected but did not answer the {question} within "
        f"{timeout:g}s. It may be applying a configuration and holding its bus -- see "
        f"GET /api/events?daemon_id={daemon_id}, then try again",
    )


def _wrong_answer(
    daemon_id: int, question: str, reply: DetectResult | ProbeResult
) -> HTTPException:
    """A reply that answers the other question, which nothing on the wire forbids.

    `Hub.deliver_reply` matches the `request_id` and the rack, deliberately and
    for good reasons of its own, so the *type* is this route's to check. 502
    rather than 500: the rack is the upstream that answered something else, and
    the difference is where somebody goes to look.
    """
    return HTTPException(
        status_code=502,
        detail=f"daemon {daemon_id} answered the {question} with {reply.type!r}, "
        "which is not a reply to it. Check that the rack is running a build of "
        "ors-daemon this server knows",
    )


@router.get("/events")
async def list_events(
    request: Request,
    daemon_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_READ)] = 100,
) -> list[EventView]:
    """A rack's recent history, newest first. See `MAX_EVENTS_PER_DAEMON`."""
    with closing(request.app.state.database.connect()) as connection:
        if daemon_id is None:
            rows = connection.execute(
                "SELECT * FROM daemon_event ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM daemon_event WHERE daemon_id = ? ORDER BY id DESC LIMIT ?",
                (daemon_id, limit),
            ).fetchall()
    return [EventView(**dict(row)) for row in rows]


def _capabilities(row: sqlite3.Row) -> dict[str, Any]:
    """What the daemon said about its hardware, or nothing because the column is not
    readable.

    Written by `ws_daemon._record_hello` from a bounded dict, so the only way it
    is not JSON is a database somebody edited -- and a listing that answers 500
    for the whole rack because one column is malformed is a rack nobody can even
    look at. `config_error` is where an unreadable column is *supposed* to
    surface, and this is a list view.
    """
    try:
        parsed = json.loads(row["capabilities"] or "{}")
    except ValueError:
        log.warning("a daemon's capabilities column is not JSON", extra={"daemon": row["id"]})
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _view(state: State, row: sqlite3.Row) -> DaemonView:
    return DaemonView(
        id=int(row["id"]),
        name=row["name"],
        status=row["status"],
        online=state.hub.is_online(int(row["id"])),
        config_version=int(row["config_version"]),
        applied_version=state.hub.acked_version(int(row["id"])),
        config_error=config_error(state, int(row["id"])),
        version=row["version"],
        capabilities=_capabilities(row),
        last_seen=row["last_seen"],
        paired_at=row["paired_at"],
        created_at=row["created_at"],
    )
