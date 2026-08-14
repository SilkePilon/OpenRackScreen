"""Templates: the scenes a screen draws, and the parameters it fills them with.

**A template edit affects every rack**, and that is not a shortcut. A snapshot
carries *all* templates -- `snapshot._templates` ships the built-ins too, so the
panel and the editor's preview are drawn from the same rows even when the two
ends are on different wheels -- so a change here really does change what every
daemon should be running. The routes below therefore never call `affects`, which
is what `changes.Change` reads as "every one of them".

Not the visual scene designer, which is phase 2. This is list, create, amend,
delete, and it is what the interface's assign and detach are built on.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from ors_schema.scene import ParamSpec, Scene
from pydantic import BaseModel, ConfigDict, Field

from ors_server.api.changes import change, one_row
from ors_server.snapshot import params_schema_json, scenes_json

log = logging.getLogger(__name__)
router = APIRouter(tags=["templates"])

MAX_NAME = 64
MAX_SCENES = 32
"""How many scenes one template may hold.

`select_scene` walks them in order and takes the first whose `when` passes, so
a template is a handful of alternatives, not a program. The bound is on a
document a browser posts and a daemon then parses on a Pi.
"""


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    category: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    scenes: list[Scene] | None = Field(default=None, max_length=MAX_SCENES)
    params_schema: dict[str, ParamSpec] | None = None
    # `builtin` is deliberately absent. It says where a row came from, and a
    # request that could set it would let a user-authored template claim to be
    # one `ors-render` ships -- which is what the daemon reads to tell an
    # override of its own built-in from the server's copy of one.


class NewTemplate(TemplateBody):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    scenes: list[Scene] = Field(max_length=MAX_SCENES)


class TemplateView(BaseModel):
    id: int
    name: str
    category: str
    builtin: bool
    scenes: list[dict[str, Any]]
    params_schema: dict[str, Any]


class Deleted(BaseModel):
    deleted: int


@router.get("/templates")
async def list_templates(request: Request) -> list[TemplateView]:
    with closing(request.app.state.database.connect()) as connection:
        rows = connection.execute("SELECT * FROM template ORDER BY name").fetchall()
    return [_view(row) for row in rows]


@router.get("/templates/{template_id}")
async def read_template(request: Request, template_id: int) -> TemplateView:
    with closing(request.app.state.database.connect()) as connection:
        row = one_row(
            connection,
            "SELECT * FROM template WHERE id = ?",
            (template_id,),
            missing=f"no template {template_id}",
        )
    return _view(row)


@router.post("/templates", status_code=201)
async def create_template(request: Request, response: Response, body: NewTemplate) -> TemplateView:
    async with change(request, response) as edit:
        try:
            cursor = edit.connection.execute(
                "INSERT INTO template (name, builtin, category, scenes, params_schema)"
                " VALUES (?, 0, ?, ?, ?)",
                (
                    body.name,
                    body.category or "general",
                    scenes_json(body.scenes),
                    params_schema_json(body.params_schema or {}),
                ),
            )
        except sqlite3.IntegrityError as error:
            # `template.name` is UNIQUE, and it is the name a screen resolves
            # against -- two rows sharing it would make which one a rack drew a
            # question about row order.
            raise HTTPException(
                status_code=409, detail=f"a template named {body.name!r} exists"
            ) from error
        row = _read(edit.connection, int(cursor.lastrowid))
    return _view(row)


@router.patch("/templates/{template_id}")
async def patch_template(
    request: Request, response: Response, template_id: int, body: TemplateBody
) -> TemplateView:
    """Amend a template, built-in rows included.

    A built-in the editor has amended is meant to survive the next restart --
    `seed_builtin_templates` inserts `ON CONFLICT DO NOTHING` precisely so that
    re-seeding cannot revert it -- so there is nothing to refuse here.
    """
    async with change(request, response) as edit:
        one_row(
            edit.connection,
            "SELECT id FROM template WHERE id = ?",
            (template_id,),
            missing=f"no template {template_id}",
        )
        columns = _columns(body)
        if columns:
            assignments = ", ".join(f"{name} = ?" for name in columns)
            try:
                edit.connection.execute(
                    f"UPDATE template SET {assignments} WHERE id = ?",  # noqa: S608 - from _columns
                    (*columns.values(), template_id),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(
                    status_code=409, detail=f"a template named {body.name!r} exists"
                ) from error
        row = _read(edit.connection, template_id)
    return _view(row)


@router.delete("/templates/{template_id}")
async def delete_template(request: Request, response: Response, template_id: int) -> Deleted:
    """Delete a template the editor made. A built-in is refused, and says why.

    `seed_builtin_templates` runs on every start and inserts anything missing,
    so deleting a built-in is a row that comes back at the next restart -- a
    delete button that undoes itself overnight is worse than one that refuses.
    Detaching it from the screens that name it is the thing that was meant.

    A template still named by a screen is refused by the snapshot rather than
    here: `build_snapshot` will not assemble a configuration whose screen names
    a template that is not defined, so `changes.change` rolls the delete back
    with the message that names the screen.
    """
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT name, builtin FROM template WHERE id = ?",
            (template_id,),
            missing=f"no template {template_id}",
        )
        if row["builtin"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{row['name']!r} is a built-in and is re-seeded at every start;"
                    " detach it from the screens that use it instead"
                ),
            )
        edit.connection.execute("DELETE FROM template WHERE id = ?", (template_id,))
    return Deleted(deleted=template_id)


def _columns(body: TemplateBody) -> dict[str, Any]:
    named = body.model_dump(exclude_unset=True)
    columns: dict[str, Any] = {}
    if "name" in named:
        columns["name"] = body.name
    if "category" in named:
        columns["category"] = body.category
    if "scenes" in named and body.scenes is not None:
        # Through `scenes_json` rather than `json.dumps` of the dumped model:
        # it is `mode="json", by_alias=True` and not `exclude_none`, which is
        # what keeps `Repeat.as` from being stored as `as_` and a cleared colour
        # from reloading as the field's default.
        columns["scenes"] = scenes_json(body.scenes)
    if "params_schema" in named and body.params_schema is not None:
        columns["params_schema"] = params_schema_json(body.params_schema)
    return columns


def _read(connection: sqlite3.Connection, template_id: int) -> sqlite3.Row:
    return one_row(
        connection,
        "SELECT * FROM template WHERE id = ?",
        (template_id,),
        missing=f"no template {template_id}",
    )


def _view(row: sqlite3.Row) -> TemplateView:
    return TemplateView(
        id=int(row["id"]),
        name=row["name"],
        category=row["category"],
        builtin=bool(row["builtin"]),
        scenes=json.loads(row["scenes"]),
        params_schema=json.loads(row["params_schema"]),
    )
