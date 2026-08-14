"""Integrations, and the one invariant `db.py` documents and this file keeps.

**A credential must never reach `integration.config`.** That column is exported
verbatim on a schema bump -- deliberately, because the export exists so a rack's
integration configuration survives one, and a redacted `config` is one you have
to retype -- so anything inline in it lands in a plaintext file beside the
database. A URL is how it happens in practice: `https://user:hunter2@prom.local`
validates, stores and exports perfectly readable.

Two rules follow, and this is the only place that can enforce them.

1. **A URL carrying userinfo is refused**, anywhere in the config, at any depth.
   Rejected rather than redacted: redaction would leave the operator with a
   config that no longer describes where to poll, and it would be one rewrite of
   this module away from being forgotten.
2. **A credential is its own field and becomes a `secret` row**, encrypted by
   `SecretStore`. It is write-only over the API: it appears in no response, in
   no log line, and in no export -- `db._REDACTED` covers `secret.ciphertext`.

M3a has nowhere to *put* a decrypted credential: `PrometheusConfig` has no field
for one and forbids extras, so `build_snapshot` refuses an enabled integration
that holds one rather than silently giving a rack an unauthenticated poller.
Storing one is therefore only useful on a disabled row today, and this module
says so in the request rather than letting it arrive as a rolled-back edit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from ors_schema.daemon import PrometheusConfig
from ors_schema.errors import first_error
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ors_server.api.changes import change, one_row

log = logging.getLogger(__name__)
router = APIRouter(tags=["integrations"])

MAX_NAME = 64
MAX_CREDENTIAL = 4096
"""How long a stored credential may be. A bearer token, not a file."""

PROBE_TIMEOUT_S = 4.0
"""How long the Test button waits on one query. `PrometheusConfig.timeout`'s default.

The button is a person waiting at a form, so this is a bound on their patience
rather than on the poller's: the daemon's own timeout is a field of the config
and belongs to the rack.
"""

MAX_PROBE_BYTES = 1024 * 1024
"""How much of a reply the Test button will read.

The URL is operator-authored and points wherever they said, so the response is
not a thing this server chose the size of. A megabyte is far past any instant
query's result and far short of a memory event.
"""


class IntegrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    config: dict[str, Any] | None = None
    poll_interval: float | None = Field(default=None, gt=0, le=3600)
    enabled: bool | None = None
    credential: str | None = Field(default=None, max_length=MAX_CREDENTIAL, repr=False)
    """A password, token or bearer, which becomes an encrypted `secret` row.

    Write-only, and out of `repr` for the reason `Hello.token` is: a model's
    `repr` is what reaches a log the moment anybody drops one into an `extra`.
    An empty string clears the stored secret; absent leaves it alone.
    """


class NewIntegration(IntegrationBody):
    daemon_id: int
    type: Literal["prometheus"] = "prometheus"
    name: str = Field(min_length=1, max_length=MAX_NAME)
    config: dict[str, Any]


class IntegrationView(BaseModel):
    """One integration, minus its credential -- which has no field here at all.

    `has_credential` rather than the value, so the interface can show that one
    is stored and offer to replace it without the plaintext ever travelling.
    """

    id: int
    daemon_id: int
    type: str
    name: str
    config: dict[str, Any]
    poll_interval: float
    enabled: bool
    has_credential: bool


class FieldReport(BaseModel):
    name: str
    ok: bool
    value: str | None = None
    error: str | None = None


class TestReport(BaseModel):
    ok: bool
    fields: list[FieldReport]


class Deleted(BaseModel):
    deleted: int


@router.get("/integrations")
async def list_integrations(
    request: Request, daemon_id: Annotated[int | None, Query()] = None
) -> list[IntegrationView]:
    with closing(request.app.state.database.connect()) as connection:
        if daemon_id is None:
            rows = connection.execute("SELECT * FROM integration ORDER BY id").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM integration WHERE daemon_id = ? ORDER BY id", (daemon_id,)
            ).fetchall()
    return [_view(row) for row in rows]


@router.get("/integrations/{integration_id}")
async def read_integration(request: Request, integration_id: int) -> IntegrationView:
    with closing(request.app.state.database.connect()) as connection:
        row = one_row(
            connection,
            "SELECT * FROM integration WHERE id = ?",
            (integration_id,),
            missing=f"no integration {integration_id}",
        )
    return _view(row)


@router.post("/integrations", status_code=201)
async def create_integration(
    request: Request, response: Response, body: NewIntegration
) -> IntegrationView:
    state = request.app.state
    enabled = True if body.enabled is None else body.enabled
    _check(body.config, body.name, body.poll_interval or 5.0)
    _credential_has_somewhere_to_go(bool(body.credential), enabled)

    async with change(request, response) as edit:
        one_row(
            edit.connection,
            "SELECT id FROM daemon WHERE id = ?",
            (body.daemon_id,),
            missing=f"no daemon {body.daemon_id}",
        )
        # On the edit's connection, so a create that is then refused leaves no
        # ciphertext behind. See `SecretStore.put_on`.
        secret_id = (
            state.secrets.put_on(edit.connection, body.credential) if body.credential else None
        )
        cursor = edit.connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, poll_interval,"
            " enabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                body.daemon_id,
                body.type,
                body.name,
                json.dumps(body.config),
                secret_id,
                body.poll_interval or 5.0,
                int(enabled),
            ),
        )
        edit.affects(body.daemon_id)
        edit.record(body.daemon_id, "info", "integration", f"added {body.name!r}")
        row = _read(edit.connection, int(cursor.lastrowid))
    return _view(row)


@router.patch("/integrations/{integration_id}")
async def patch_integration(
    request: Request, response: Response, integration_id: int, body: IntegrationBody
) -> IntegrationView:
    state = request.app.state
    named = body.model_dump(exclude_unset=True)
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT * FROM integration WHERE id = ?",
            (integration_id,),
            missing=f"no integration {integration_id}",
        )
        name = body.name if body.name is not None else row["name"]
        interval = body.poll_interval if body.poll_interval is not None else row["poll_interval"]
        config = body.config if body.config is not None else json.loads(row["config"])
        enabled = row["enabled"] if body.enabled is None else body.enabled
        _check(config, name, interval)

        columns: dict[str, Any] = {}
        if "name" in named:
            columns["name"] = name
        if "config" in named:
            columns["config"] = json.dumps(config)
        if "poll_interval" in named:
            columns["poll_interval"] = interval
        if "enabled" in named:
            columns["enabled"] = int(bool(enabled))
        if "credential" in named:
            # Replaced, not accumulated: the old row is deleted, because a
            # credential nothing references is a ciphertext in every export from
            # now on with nothing left that could decide to remove it.
            columns["secret_id"] = (
                state.secrets.put_on(edit.connection, body.credential) if body.credential else None
            )
            _forget(state, edit.connection, row["secret_id"])
        # About the state this edit leaves behind, not about what it named: a
        # request that only sets `enabled: true` carries no credential and is
        # still the request that puts one in front of a snapshot.
        _credential_has_somewhere_to_go(
            columns.get("secret_id", row["secret_id"]) is not None, bool(enabled)
        )

        if columns:
            assignments = ", ".join(f"{column} = ?" for column in columns)
            edit.connection.execute(
                f"UPDATE integration SET {assignments} WHERE id = ?",  # noqa: S608 - from columns
                (*columns.values(), integration_id),
            )
        edit.affects(int(row["daemon_id"]))
        edit.record(int(row["daemon_id"]), "info", "integration", f"edited {name!r}")
        row = _read(edit.connection, integration_id)
    return _view(row)


@router.delete("/integrations/{integration_id}")
async def delete_integration(request: Request, response: Response, integration_id: int) -> Deleted:
    state = request.app.state
    async with change(request, response) as edit:
        row = one_row(
            edit.connection,
            "SELECT daemon_id, name, secret_id FROM integration WHERE id = ?",
            (integration_id,),
            missing=f"no integration {integration_id}",
        )
        edit.connection.execute("DELETE FROM integration WHERE id = ?", (integration_id,))
        # `integration.secret_id` is `ON DELETE SET NULL`, which protects the
        # integration from a deleted secret and does nothing in this direction:
        # without this the ciphertext outlives the only thing that referenced
        # it, in the database and in every export of it, unreachable and
        # undeletable through any route.
        _forget(state, edit.connection, row["secret_id"])
        edit.affects(int(row["daemon_id"]))
        edit.record(int(row["daemon_id"]), "info", "integration", f"removed {row['name']!r}")
    return Deleted(deleted=integration_id)


@router.post("/integrations/{integration_id}/test")
async def test_integration(request: Request, integration_id: int) -> TestReport:
    """Dry-run every field of this integration and show what came back.

    Not a `change`: it writes nothing and pushes nothing. It exists because a
    PromQL query that returns no series is indistinguishable, on the glass, from
    a panel that has not finished connecting -- so the only other way to find out
    is to save it and go and look at the rack.

    What it reports is the first sample of each query's result vector. The
    daemon's `reduce`, `label` and `strip` are deliberately not reimplemented
    here: they are one integration's semantics and they live with the poller
    that owns them, and a second copy of them in the server would be two answers
    to "what does this screen show" that could disagree.

    The fetch is `state.probe`, which is a plain function on the app so that a
    test can hand it a different one without a socket, a port or a sleep.
    """
    state = request.app.state
    with closing(state.database.connect()) as connection:
        row = one_row(
            connection,
            "SELECT * FROM integration WHERE id = ?",
            (integration_id,),
            missing=f"no integration {integration_id}",
        )
    config = _check(json.loads(row["config"]), row["name"], row["poll_interval"])

    reports: list[FieldReport] = []
    for name, spec in config.fields.items():
        try:
            samples = await asyncio.to_thread(state.probe, config.url, spec.query, config.timeout)
        except ProbeError as error:
            reports.append(FieldReport(name=name, ok=False, error=str(error)))
            continue
        if not samples:
            reports.append(FieldReport(name=name, ok=False, error="the query matched no series"))
            continue
        reports.append(FieldReport(name=name, ok=True, value=samples[0]))
    return TestReport(ok=all(report.ok for report in reports), fields=reports)


class ProbeError(RuntimeError):
    """The integration could not be reached, or did not answer usefully."""


def query_prometheus(url: str, query: str, timeout: float) -> list[str]:
    """One instant query against a Prometheus, as strings. Raises `ProbeError`.

    `urllib` rather than a client library, because this is the only outbound
    HTTP the server makes and a dependency for one request is a dependency to
    keep patched on an arm64 image forever. The daemon has its own poller and
    does not share this: it runs on the Pi, behind a tunnel this server has no
    view of.
    """
    endpoint = f"{url.rstrip('/')}/api/v1/query?{urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:  # noqa: S310 - operator URL
            payload = json.loads(response.read(MAX_PROBE_BYTES))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ProbeError(f"{type(error).__name__}: {error}") from error
    if payload.get("status") != "success":
        raise ProbeError(str(payload.get("error", "the server refused the query")))
    return [
        str(result["value"][1])
        for result in payload.get("data", {}).get("result", [])
        if isinstance(result, dict) and len(result.get("value", ())) == 2
    ]


def _check(config: dict[str, Any], name: str, poll_interval: float) -> PrometheusConfig:
    """Validate a config the way the daemon will, and refuse a credential in it.

    The same model the rack parses, with the columns overlaid exactly as
    `snapshot._integrations` overlays them, so what is accepted here is what a
    snapshot will assemble. The alternative is a row that saves and then blocks
    every one of that rack's later edits.
    """
    _no_userinfo(config)
    try:
        return PrometheusConfig.model_validate(
            {**config, "name": name, "type": "prometheus", "poll_interval": poll_interval}
        )
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=first_error(error)) from error


def _no_userinfo(value: Any, path: str = "config") -> None:
    """Refuse `https://user:pass@host` anywhere in the config, at any depth.

    Walked rather than checked at `url`, because `config` is a free JSON
    document: `tunnel` carries fields of its own today and M4's integrations
    will carry more, and a check that named one key would be a check that the
    next key evades.

    A string is treated as a URL only when it parses as one with a scheme and a
    host, so a PromQL query containing an `@` is left alone.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _no_userinfo(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _no_userinfo(item, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    try:
        parsed = urlsplit(value)
    except ValueError:
        return
    if parsed.scheme and parsed.netloc and (parsed.username or parsed.password):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{path} carries a credential in a URL. `config` is exported in plain text"
                " beside the database; send it as `credential` instead, which is encrypted."
            ),
        )


def _credential_has_somewhere_to_go(has_credential: bool, enabled: bool) -> None:
    """Refuse to store a credential on an *enabled* integration, and say why.

    No integration type can carry one on the wire in M3a: `PrometheusConfig` has
    no field for it and forbids extras, so `build_snapshot` refuses an enabled
    row that holds a `secret_id` rather than handing a rack a poller that
    authenticates with nothing. Left to that refusal this would be an edit that
    saves, blocks every later edit to the rack, and reports it as a snapshot
    error naming a table -- so the answer is given here, where the field is.
    """
    if has_credential and enabled:
        raise HTTPException(
            status_code=422,
            detail=(
                "no integration type can carry a credential to a daemon yet, so an enabled"
                " integration holding one would block this rack's configuration entirely."
                " Store it on a disabled integration, or leave it out."
            ),
        )


def _forget(state: Any, connection: sqlite3.Connection, secret_id: int | None) -> None:
    if secret_id is not None:
        state.secrets.delete_on(connection, int(secret_id))


def _read(connection: sqlite3.Connection, integration_id: int) -> sqlite3.Row:
    return one_row(
        connection,
        "SELECT * FROM integration WHERE id = ?",
        (integration_id,),
        missing=f"no integration {integration_id}",
    )


def _view(row: sqlite3.Row) -> IntegrationView:
    return IntegrationView(
        id=int(row["id"]),
        daemon_id=int(row["daemon_id"]),
        type=row["type"],
        name=row["name"],
        config=json.loads(row["config"]),
        poll_interval=float(row["poll_interval"]),
        enabled=bool(row["enabled"]),
        has_credential=row["secret_id"] is not None,
    )
