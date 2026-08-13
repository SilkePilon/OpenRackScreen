from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig
from ors_schema.errors import first_error
from ors_schema.scene import ParamSpec, Scene
from pydantic import ValidationError

from ors_server.db import Database
from ors_server.secrets import SecretStore


class SnapshotError(ValueError):
    """The database holds something no daemon could be given.

    One exception type for every way that can happen -- a column that is not
    JSON, a credential the wire format cannot carry, a field pydantic refuses --
    so a caller that knows only that it asked for a snapshot has one thing to
    catch. Every mutation in the configuration API assembles a snapshot before
    pushing it, so this surfaces in the request that made the change, which is
    the only moment anyone knows what they just did.

    The message is the whole value: it names the daemon, and then whatever the
    layer that raised it knows -- the table, column and row id, or the field path
    pydantic reported. A `ValidationError` carries the second of those already,
    so it is reformatted rather than replaced; wrapping it in something vaguer
    would leave the reader with strictly less than the error being caught.
    """


def _json_column(raw: str, table: str, column: str, row_id: object) -> Any:
    """One JSON-bearing column, parsed, or a `SnapshotError` that says which.

    Every JSON column in this module goes through here. `json.loads` on a corrupt
    one raises `Expecting value: line 1 column 1 (char 0)` -- no table, no column,
    no row, and nothing to grep for in a database with forty screens in it. Worse,
    `JSONDecodeError` subclasses `ValueError`, so it slips past a caller catching
    `SnapshotError` as an unhandled traceback and into one catching `ValueError`
    as a configuration complaint about nothing the reader can see.

    A column that will not parse *is* "the database holds something no daemon
    could be given", so it arrives as the exception that means that. The row is
    identified by whatever names it -- `id` for the tables that have one, the key
    for `setting` -- because the point is to be able to go and look at it.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError(
            f"{table}.{column} of row {row_id!r} is not valid JSON: {error}"
        ) from error


def scenes_json(scenes: Sequence[Scene]) -> str:
    """A template's scenes as the database stores them.

    Not `exclude_none=True`, which is the tempting way to keep the column
    small and is wrong: several fields are nullable with a *non-null* default,
    so dropping the nulls does not drop a default -- it changes the document.
    `RectElement.fill` defaults to white and `RingElement.track` to a dark grey,
    so a stroke-only rectangle saved as `"fill": null` comes back as a solid
    white box, and a ring drawn without its track comes back with one. The
    editor writes those nulls the moment someone clears a colour.
    """
    return json.dumps([scene.model_dump(mode="json", by_alias=True) for scene in scenes])


def params_schema_json(params_schema: Mapping[str, ParamSpec]) -> str:
    return json.dumps({key: spec.model_dump(mode="json") for key, spec in params_schema.items()})


def seed_builtin_templates(database: Database) -> None:
    """Copy the render engine's built-ins into the database, once.

    `DO NOTHING` rather than an upsert: once a row exists it belongs to whoever
    edited it. A built-in the editor has amended must survive the next restart,
    and re-seeding over it would revert that edit with no trace of why.

    All of them or none of them. `Database.connect` is autocommit, so without the
    explicit `BEGIN` this is seven transactions, and a server killed during its
    first start leaves three built-ins in the table -- enough for the editor to
    list and to draw previews from, and not enough for the rack to be shown the
    row its panel is drawing. A rolled-back seed is one the next start redoes.
    """
    with closing(database.connect()) as connection, connection:
        connection.execute("BEGIN")
        for name, template in load_builtin_templates().items():
            connection.execute(
                "INSERT INTO template (name, builtin, category, scenes, params_schema)"
                " VALUES (?, 1, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
                (
                    name,
                    template.category,
                    scenes_json(template.scenes),
                    params_schema_json(template.params_schema),
                ),
            )


def bump_config_version(database: Database, daemon_id: int) -> int:
    """Advance this daemon's generation counter and return the new value.

    One statement, so no two callers are handed the same number. An UPDATE
    followed by a SELECT cannot promise that: `Database.connect` is autocommit,
    so a second edit commits between the two and both callers read the same
    value back, push two different snapshots under it, and the daemon drops the
    second as one it has already applied.

    What this does *not* promise is that the number identifies the rows that were
    pushed under it. `build_snapshot` opens its own connection, so an edit can
    land between minting a version and reading the tables, and no single
    statement can bind the two together. What makes the sequence safe is the
    order the link route uses: mint first, then read. The snapshot pushed is then
    never *older* than its number, and the edit that raced it mints a higher one
    and pushes its own, so the rack converges on the last write. Reading first
    would push stale rows under a number the daemon has already applied, and the
    correction would be deduped away.

    (`RETURNING` needs SQLite 3.35.0. The guarantee it leans on -- that the
    UPDATE runs to completion before any RETURNING row is emitted, so reading
    only the first row still applies the change -- is documented from that same
    release: the "Processing Order" section describes the interleaved behaviour
    as a pre-release prototype and says why it was abandoned. The oldest
    SQLite behind a distro Python 3.11 is Debian bookworm's 3.40.1.)
    """
    with closing(database.connect()) as connection:
        row = connection.execute(
            "UPDATE daemon SET config_version = config_version + 1"
            " WHERE id = ? RETURNING config_version",
            (daemon_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"no daemon {daemon_id}")
    return int(row["config_version"])


def _screens(connection: sqlite3.Connection, daemon_id: int) -> list[dict[str, Any]]:
    # `id` breaks the tie because SQLite promises no order among equal keys, and
    # two panels sharing a position is a state the schema allows and the editor
    # can produce mid-reorder. Left to chance, the same database would push a
    # different left-to-right order on different days.
    rows = connection.execute(
        "SELECT * FROM screen WHERE daemon_id = ? ORDER BY position, id", (daemon_id,)
    ).fetchall()
    return [
        {
            # The row id travels, and it is the only field here that is not
            # about the panel. It is what a frame from this rack is addressed
            # by -- `_owns` refuses one naming a screen the daemon does not own,
            # and the hub fans it out to whoever is watching that id -- and the
            # daemon cannot derive it from anything else it is sent.
            "id": row["id"],
            "name": row["name"],
            "position": row["position"],
            "display": _json_column(row["display"], "screen", "display", row["id"]),
            "rotation": row["rotation"],
            "hflip": bool(row["hflip"]),
            "enabled": bool(row["enabled"]),
            "template": row["template"],
            "params": _json_column(row["params"], "screen", "params", row["id"]),
            "sleep_override": (
                _json_column(row["sleep_override"], "screen", "sleep_override", row["id"])
                if row["sleep_override"]
                else None
            ),
        }
        for row in rows
    ]


def _integrations(connection: sqlite3.Connection, daemon_id: int) -> list[dict[str, Any]]:
    integrations = []
    for row in connection.execute(
        "SELECT * FROM integration WHERE daemon_id = ? AND enabled = 1 ORDER BY id", (daemon_id,)
    ).fetchall():
        if row["secret_id"] is not None:
            # No integration type carries a credential in M3a. `PrometheusConfig`
            # has no field for one and forbids extras, so a decrypted secret has
            # nowhere to go; M4's qBittorrent is the first that will, and this
            # becomes a lookup from `type` to the field that carries it.
            #
            # Until then a `secret_id` is a bug in whatever wrote the row, and
            # dropping it silently would give the rack an integration polling
            # unauthenticated -- a 401 every interval, a screen stuck on
            # `connecting`, and nothing anywhere saying a credential was
            # discarded. The plaintext is deliberately not fetched: there is no
            # use for it here, and an error message is a log line.
            #
            # This blocks *every* push for the daemon, an unrelated screen rename
            # included, so there has to be a way out that does not mean deleting
            # the credential -- and the query above is it. A disabled integration
            # is never read here, deliberately: disabling the offending row is
            # how a rack gets its other edits moving again, which is why the
            # message says so. The route's write lands before the snapshot it
            # then assembles, `Database.connect` being autocommit.
            raise SnapshotError(
                f"integration {row['name']!r} of type {row['type']!r} has a stored credential,"
                " but nothing in the wire format can carry one;"
                " disable the integration to unblock this daemon's other edits"
            )
        config = _json_column(row["config"], "integration", "config", row["id"])
        # The columns win: they are what the API edits and what the list view
        # shows, so a stale copy left inside `config` must not contradict them.
        config |= {
            "name": row["name"],
            "type": row["type"],
            "poll_interval": row["poll_interval"],
        }
        integrations.append(config)
    return integrations


def _templates(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    # Every template, built-ins included, rather than only the user's. The
    # daemon has its own copy of the built-ins, but the server is what renders
    # the editor's preview: shipping the row the preview was drawn from is what
    # keeps the panel and the browser showing the same thing when the two ends
    # are on different wheels.
    return {
        row["name"]: {
            "name": row["name"],
            "category": row["category"],
            "builtin": bool(row["builtin"]),
            "scenes": _json_column(row["scenes"], "template", "scenes", row["id"]),
            "params_schema": _json_column(
                row["params_schema"], "template", "params_schema", row["id"]
            ),
        }
        for row in connection.execute("SELECT * FROM template")
    }


def build_snapshot(database: Database, secrets: SecretStore, daemon_id: int) -> DaemonConfig:
    """Assemble what this daemon should be running, as the model it already loads.

    The wire format is `DaemonConfig` itself, so a snapshot goes through exactly
    the validation a hand-written YAML file does -- and a server that assembles
    something the daemon would refuse finds out here rather than on the rack.

    `secrets` is taken and not used: no integration type has a field to carry a
    credential yet (see `_integrations`), so there is nothing to decrypt. It is
    a parameter rather than something M4 adds later because every caller already
    holds the store, and threading it through afterwards would mean editing the
    configuration API and the daemon socket to add an argument neither of them
    has an opinion about.
    """
    with closing(database.connect()) as connection:
        if connection.execute("SELECT 1 FROM daemon WHERE id = ?", (daemon_id,)).fetchone() is None:
            raise KeyError(f"no daemon {daemon_id}")

        settings = {
            row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM setting")
        }
        payload: dict[str, Any] = {
            "version": 1,
            "screens": _screens(connection, daemon_id),
            "integrations": _integrations(connection, daemon_id),
            "templates": _templates(connection),
        }
    # Absent keys are left out entirely rather than defaulted here, so the one
    # place a default is written stays `DaemonConfig`. A second copy in this
    # module would be a rack running a timezone the schema never chose.
    if "timezone" in settings:
        payload["timezone"] = settings["timezone"]
    if "night" in settings:
        payload["night"] = _json_column(settings["night"], "setting", "value", "night")

    try:
        config = DaemonConfig.model_validate(payload)
    except ValidationError as error:
        # The same formatting the daemon puts on a hand-written file, because it
        # is the same model and the same question -- which field, and what would
        # have been acceptable. `__cause__` is not an answer: the caller is an
        # API route turning this into a response, and nothing about the row
        # survives into what the person who just saved a screen reads.
        raise SnapshotError(
            f"daemon {daemon_id} has a configuration no daemon can run: {first_error(error)}"
        ) from error

    _every_screen_resolves(config)
    return config


def _every_screen_resolves(config: DaemonConfig) -> None:
    """Refuse a screen naming a template that is not in the snapshot.

    `DaemonConfig` does not check this -- a screen's `template` is a free string
    to it -- but `resolve_screens` on the daemon raises `ConfigError` over the
    *whole* config when it cannot find one, so a single screen left pointing at
    a deleted template takes the entire rack down with it. Checked against the
    same two sources the daemon merges, and only for enabled screens, because
    that is the order the daemon checks in: a disabled screen is skipped before
    its template is looked up.
    """
    available = set(load_builtin_templates()) | set(config.templates)
    for screen in config.screens:
        if screen.enabled and screen.template not in available:
            raise SnapshotError(
                f"screen {screen.name!r} names template {screen.template!r}, which is not defined"
            )
