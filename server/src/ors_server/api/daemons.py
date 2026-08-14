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
from contextlib import closing
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from ors_schema.link import Command
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import State

from ors_server.api.changes import change, config_error, one_row
from ors_server.pairing import mint_token, rotate_key

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


class Delivered(BaseModel):
    delivered: bool
    """Whether a rack was there to receive it."""


class Pushed(BaseModel):
    version: int
    delivered: bool


class Deleted(BaseModel):
    deleted: int


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
    and never again, from this or any other route."""
    state = request.app.state
    try:
        daemon_id, token = mint_token(state.database, body.name)
    except sqlite3.IntegrityError as error:
        # `daemon.name` is UNIQUE. A 409 rather than a 500, because two racks
        # called "pi-rack" is a thing a person does, not a thing that goes wrong.
        raise HTTPException(
            status_code=409, detail=f"a daemon named {body.name!r} exists"
        ) from error

    async with change(request, response) as edit:
        # Nothing to push, and no version to mint: a rack that has just been
        # created has no screens, no socket and no configuration that has
        # changed. The counter is the generation of a *configuration*, and
        # moving it here would start every rack at 1 for an edit that touched
        # nothing. The event is still the transaction's, so a creation that then
        # failed leaves no history of a rack that does not exist.
        edit.affects_nobody()
        edit.record(daemon_id, "info", "created", f"minted a pairing token for {body.name!r}")
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
        edit.affects(daemon_id)
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
    """
    async with change(request, response) as edit:
        one_row(
            edit.connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
        edit.connection.execute("DELETE FROM daemon WHERE id = ?", (daemon_id,))
        # The one edit with nothing to push: the rack it changed is gone, and
        # minting a version for a row that no longer exists is a `KeyError`.
        edit.affects_nobody()
    log.info("a daemon was deleted", extra={"daemon": daemon_id})
    return Deleted(deleted=daemon_id)


@router.post("/daemons/{daemon_id}/command")
async def send_command(request: Request, daemon_id: int, body: CommandBody) -> Delivered:
    """Say something to a rack that is listening. Changes nothing on the server.

    Deliberately not a `change`: a command mints no version and pushes no
    snapshot. Bumping here would mean pressing identify tore down and repainted
    every panel on the rack.

    Refused when the rack is not connected, which is the opposite of what an
    edit does. `Hub.push_config` drops a send to an offline daemon in silence
    because the edit is saved and pushed on reconnect -- there is nothing about
    `identify` that survives being deferred by an hour, so answering 200 would
    be a button that lies.
    """
    state = request.app.state
    with closing(state.database.connect()) as connection:
        one_row(
            connection,
            "SELECT id FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
    if not state.hub.is_online(daemon_id):
        raise HTTPException(status_code=409, detail=f"daemon {daemon_id} is not connected")
    await state.hub.send_command(daemon_id, Command(command=body.command, screen_id=body.screen_id))
    return Delivered(delivered=True)


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
        delivered=request.app.state.hub.is_online(daemon_id),
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
        config_error=config_error(state, int(row["id"])),
        version=row["version"],
        capabilities=_capabilities(row),
        last_seen=row["last_seen"],
        paired_at=row["paired_at"],
        created_at=row["created_at"],
    )
