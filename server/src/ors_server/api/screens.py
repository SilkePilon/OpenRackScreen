"""Panels: what each one draws, where it sits in the rack, and what it looks like.

Every route is `async def` for the reason `daemons.py` gives, and every mutation
goes through `changes.change`, which is what bumps the version and pushes the
result. Nothing here calls `bump_config_version` or `hub.push_config` directly,
and that is the point: a route that could would be a route that could forget.

The bodies are validated with `ors_schema.daemon`'s own models -- `DisplayConfig`
for the wiring, `NightWindow` for a sleep override -- rather than with copies.
The API and the rack then refuse the same documents for the same reasons, in the
request that asked rather than as a nack seconds later.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
from contextlib import closing
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from ors_render import RenderContext, load_builtin_templates, render_screen
from ors_schema.daemon import DisplayConfig, NightWindow
from ors_schema.scene import Template
from pydantic import BaseModel, ConfigDict, Field

from ors_server.api.changes import change, one_row

log = logging.getLogger(__name__)
router = APIRouter(tags=["screens"])

MAX_NAME = 64
MAX_POSITION = 1024
"""How far along a rack a panel may claim to be.

Not a statement about how many panels exist -- `position` is an ordinal the
editor renumbers, and nothing requires it to be dense -- but a bound on a number
that reaches SQLite and a `sorted` key. A rack is four panels; a thousand is
past anything anybody will bolt to a wall.
"""

PREVIEW_SIZE = 240
"""The panel's real pixel size, so the preview is the picture, not an impression."""


class ScreenBody(BaseModel):
    """The fields a screen has, all optional. `NewScreen` makes some of them not.

    One model for POST and PATCH, with the PATCH semantics coming from
    `exclude_unset` at the call site rather than from a second model: two models
    is two places to add the next field, and the one that gets forgotten is
    always the patch.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    position: int | None = Field(default=None, ge=1, le=MAX_POSITION)
    display: DisplayConfig | None = None
    rotation: Literal[0, 90, 180, 270] | None = None
    hflip: bool | None = None
    enabled: bool | None = None
    template: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    params: dict[str, Any] | None = None
    sleep_override: NightWindow | None = None


class NewScreen(ScreenBody):
    daemon_id: int
    name: str = Field(min_length=1, max_length=MAX_NAME)
    position: int = Field(ge=1, le=MAX_POSITION)
    display: DisplayConfig
    template: str = Field(min_length=1, max_length=MAX_NAME)


class ScreenView(BaseModel):
    id: int
    daemon_id: int
    name: str
    position: int
    display: dict[str, Any]
    rotation: int
    hflip: bool
    enabled: bool
    template: str
    params: dict[str, Any]
    sleep_override: dict[str, Any] | None


class Reorder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=MAX_POSITION)
    """The new left-to-right order. Every screen named is renumbered from 1."""


class Deleted(BaseModel):
    deleted: int


@router.get("/screens")
async def list_screens(
    request: Request, daemon_id: Annotated[int | None, Query()] = None
) -> list[ScreenView]:
    with closing(request.app.state.database.connect()) as connection:
        # `position, id` because SQLite promises no order among equal keys and
        # two panels sharing a position is a state the schema allows -- the same
        # tiebreak `snapshot._screens` uses, so the list and the rack agree.
        if daemon_id is None:
            rows = connection.execute("SELECT * FROM screen ORDER BY position, id").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM screen WHERE daemon_id = ? ORDER BY position, id", (daemon_id,)
            ).fetchall()
    return [_view(row) for row in rows]


@router.get("/screens/{screen_id}")
async def read_screen(request: Request, screen_id: int) -> ScreenView:
    with closing(request.app.state.database.connect()) as connection:
        row = one_row(
            connection,
            "SELECT * FROM screen WHERE id = ?",
            (screen_id,),
            missing=f"no screen {screen_id}",
        )
    return _view(row)


@router.post("/screens", status_code=201)
async def create_screen(request: Request, response: Response, body: NewScreen) -> ScreenView:
    async with change(request, response) as edit:
        one_row(
            edit.connection,
            "SELECT id FROM daemon WHERE id = ?",
            (body.daemon_id,),
            missing=f"no daemon {body.daemon_id}",
        )
        cursor = edit.connection.execute(
            "INSERT INTO screen"
            " (daemon_id, position, name, display, rotation, hflip, enabled, template, params,"
            "  sleep_override)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.daemon_id,
                body.position,
                body.name,
                body.display.model_dump_json(),
                body.rotation or 0,
                int(bool(body.hflip)),
                int(body.enabled if body.enabled is not None else True),
                body.template,
                json.dumps(body.params or {}),
                body.sleep_override.model_dump_json() if body.sleep_override else None,
            ),
        )
        screen_id = int(cursor.lastrowid)
        edit.affects(body.daemon_id)
        edit.record(body.daemon_id, "info", "screen", f"added {body.name!r}")
        row = _read(edit.connection, screen_id)
    return _view(row)


@router.patch("/screens/{screen_id}")
async def patch_screen(
    request: Request, response: Response, screen_id: int, body: ScreenBody
) -> ScreenView:
    """Change some of a screen's fields and leave the rest alone.

    `exclude_unset` is what makes this a PATCH: a body naming only `rotation`
    must not blank `params`, and a model whose every field defaults to None
    cannot tell "not sent" from "set to nothing" any other way.
    """
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT * FROM screen WHERE id = ?",
            (screen_id,),
            missing=f"no screen {screen_id}",
        )
        daemon_id = int(row["daemon_id"])
        columns = _columns(body)
        if columns:
            assignments = ", ".join(f"{name} = ?" for name in columns)
            edit.connection.execute(
                f"UPDATE screen SET {assignments} WHERE id = ?",  # noqa: S608 - names from _columns
                (*columns.values(), screen_id),
            )
        edit.affects(daemon_id)
        edit.record(daemon_id, "info", "screen", f"edited {row['name']!r}")
        row = _read(edit.connection, screen_id)
    return _view(row)


@router.delete("/screens/{screen_id}")
async def delete_screen(request: Request, response: Response, screen_id: int) -> Deleted:
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT daemon_id, name FROM screen WHERE id = ?",
            (screen_id,),
            missing=f"no screen {screen_id}",
        )
        edit.connection.execute("DELETE FROM screen WHERE id = ?", (screen_id,))
        edit.affects(int(row["daemon_id"]))
        edit.record(int(row["daemon_id"]), "info", "screen", f"removed {row['name']!r}")
    return Deleted(deleted=screen_id)


@router.post("/screens/reorder")
async def reorder_screens(request: Request, response: Response, body: Reorder) -> list[ScreenView]:
    """Renumber the screens named, from 1, in the order given.

    Every id has to exist before anything is written, because a reorder that
    applied the ids it recognised and ignored the rest would leave the rack in
    an order nobody asked for -- and the interface would show the order it sent.
    The transaction makes the refusal total.

    More than one rack's screens may be named at once. The affected set is read
    from the rows rather than assumed, so each rack is pushed exactly its own.
    """
    if len(set(body.ids)) != len(body.ids):
        # Two panels cannot be given one position from one entry, and a third
        # would keep a number nothing in this request assigned.
        raise HTTPException(status_code=422, detail="a screen may appear once in a reorder")
    async with change(request, response) as edit:
        for position, screen_id in enumerate(body.ids, start=1):
            row = one_row(
                edit.connection,
                "SELECT daemon_id FROM screen WHERE id = ?",
                (screen_id,),
                missing=f"no screen {screen_id}",
            )
            edit.connection.execute(
                "UPDATE screen SET position = ? WHERE id = ?", (position, screen_id)
            )
            edit.affects(int(row["daemon_id"]))
        for daemon_id in edit.daemons():
            edit.record(daemon_id, "info", "screens", "the panels were reordered")
        rows = [_read(edit.connection, screen_id) for screen_id in body.ids]
    return [_view(row) for row in rows]


@router.get(
    "/screens/{screen_id}/preview",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def preview_screen(request: Request, screen_id: int) -> Response:
    """Draw this screen the way the rack would, without a rack.

    PNG rather than the WebP the link carries: this is one image for an editor
    pane, where losing nothing matters more than weighing little, and every
    browser and every test can read it.

    With no live data at all, so every binding resolves to nothing and the
    template's own defaults are what show. That is the honest thing for a
    preview to be -- the alternative is the server polling a rack's integrations
    to draw a thumbnail -- and it is also what makes the route free of the
    daemon entirely: a screen can be previewed before its Pi has ever connected.

    Off the event loop, because it is the one route here that computes: a
    240x240 render at supersample 2 is milliseconds of Pillow, and `async def`
    (which the hub's affinity requires of everything in this package) would put
    those milliseconds in front of every frame the server is relaying.
    """
    state = request.app.state
    with closing(state.database.connect()) as connection:
        row = one_row(
            connection,
            "SELECT * FROM screen WHERE id = ?",
            (screen_id,),
            missing=f"no screen {screen_id}",
        )
        template = _template_for(connection, row)
    png = await asyncio.to_thread(_render, template, row)
    return Response(content=png, media_type="image/png")


def _render(template: Template, row: sqlite3.Row) -> bytes:
    """The scenes, the bound parameters, and no data. Returns PNG bytes.

    `bind_params` rather than the row's `params` alone: a template's declared
    defaults are half of what a screen draws, and a preview that skipped them
    would show an empty gauge for every screen that has not overridden
    everything.

    The mount correction -- `rotation` and `hflip` -- is deliberately not
    applied. Those describe how a panel is bolted into the rack, and the browser
    shows the picture the way a person standing at the rack sees it; the daemon
    transposes for the glass, and the live frames it streams are not transposed
    either. A preview that rotated would disagree with the panel beside it.
    """
    image = render_screen(
        template.scenes,
        RenderContext(data={"params": template.bind_params(_json(row["params"]))}),
        size=PREVIEW_SIZE,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _template_for(connection: sqlite3.Connection, row: sqlite3.Row) -> Template:
    """The template this screen names, from the table or from the built-ins.

    The table first, because a built-in the editor has amended lives there and
    the preview must show what the rack would draw. A screen naming a template
    that is not in either is a 404 rather than a 500: it is a real state -- a
    template deleted from another tab -- and the page asking is showing a list
    that is one edit old.
    """
    name = row["template"]
    stored = connection.execute("SELECT * FROM template WHERE name = ?", (name,)).fetchone()
    if stored is not None:
        return Template(
            name=stored["name"],
            category=stored["category"],
            builtin=bool(stored["builtin"]),
            scenes=_json(stored["scenes"]),
            params_schema=_json(stored["params_schema"]),
        )
    builtin = load_builtin_templates().get(name)
    if builtin is None:
        raise HTTPException(status_code=404, detail=f"no template {name!r}")
    return builtin


def _columns(body: ScreenBody) -> dict[str, Any]:
    """The columns a PATCH body really named, as SQLite wants them.

    `exclude_unset=True` and not `exclude_none=True`: `sleep_override: null` is
    a screen that has stopped overriding the rack's night window, which is a
    change, and dropping the nulls would make it unsayable.
    """
    named = body.model_dump(exclude_unset=True, mode="json")
    named.pop("daemon_id", None)
    columns: dict[str, Any] = {}
    for field_name, value in named.items():
        if field_name in ("display", "sleep_override"):
            columns[field_name] = None if value is None else json.dumps(value)
        elif field_name == "params":
            columns[field_name] = json.dumps(value or {})
        elif field_name in ("hflip", "enabled"):
            columns[field_name] = int(bool(value))
        else:
            columns[field_name] = value
    return columns


def _read(connection: sqlite3.Connection, screen_id: int) -> sqlite3.Row:
    return one_row(
        connection,
        "SELECT * FROM screen WHERE id = ?",
        (screen_id,),
        missing=f"no screen {screen_id}",
    )


def _view(row: sqlite3.Row) -> ScreenView:
    return ScreenView(
        id=int(row["id"]),
        daemon_id=int(row["daemon_id"]),
        name=row["name"],
        position=int(row["position"]),
        display=_readable(row["display"], row["id"], "display"),
        rotation=int(row["rotation"]),
        hflip=bool(row["hflip"]),
        enabled=bool(row["enabled"]),
        template=row["template"],
        params=_readable(row["params"], row["id"], "params"),
        sleep_override=(
            _readable(row["sleep_override"], row["id"], "sleep_override")
            if row["sleep_override"]
            else None
        ),
    )


def _readable(raw: str | None, screen_id: object, column: str) -> Any:
    """A JSON column for a *listing*, or empty because it cannot be read.

    Every writer in this package writes `json.dumps`, so a column that will not
    parse is a database somebody hand-edited or restored -- and where that is
    *reported* is `config_error` on `GET /api/daemons`, which names the table,
    the column and the row, and which is what stops the rack being pushed at all.

    So this answers the way `daemons._capabilities` answers: empty, with a
    warning in the log. Raising made one bad row on one rack a 500 for `GET
    /api/screens` -- every screen on every rack -- so there was no list from
    which to find it or repair it and the editor was simply unloadable. An empty
    document, on a page that also shows the `config_error` naming the row, is
    strictly more than no page at all.

    `RecursionError` alongside `ValueError` for the reason `snapshot._json_column`
    gives: a well-formed document 20,000 deep is unreadable by a different road,
    and derives from `RuntimeError`, so it goes past a guard written for a
    malformed one.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        log.warning(
            "a screen column could not be read", extra={"screen": screen_id, "column": column}
        )
        return {}


def _json(raw: str | None) -> Any:
    """A JSON column on the preview path, or a 500 that says which.

    Deliberately *not* the tolerant read above. A preview is one screen asked
    for by name, so failing it costs that pane and no list; and an empty
    `scenes` is not a template that draws nothing, it is a `ValidationError` one
    line later with nothing in it about the column that was unreadable.
    """
    return json.loads(raw) if raw is not None else None
