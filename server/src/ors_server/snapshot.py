from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig
from ors_schema.scene import ParamSpec, Scene
from pydantic import ValidationError

from ors_server.db import Database
from ors_server.secrets import SecretStore


class SnapshotError(ValueError):
    """The database holds something no daemon could be given.

    Raised where the row is, naming it, rather than letting a pydantic
    `ValidationError` about `screens.0.rotation` reach a caller that only knows
    it asked for a snapshot. Every mutation in the configuration API assembles a
    snapshot before pushing it, so this surfaces in the request that made the
    change -- which is the only moment anyone knows what they just did.
    """


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
    """
    with closing(database.connect()) as connection:
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

    One statement, so two edits landing together cannot both read back the same
    number and push two different snapshots under one version -- the daemon
    dedupes on it, so the second would be dropped as already applied. (Needs
    SQLite 3.35 for `RETURNING`; the oldest distro Python 3.11 ships with is
    newer than that.)
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
            "name": row["name"],
            "position": row["position"],
            "display": json.loads(row["display"]),
            "rotation": row["rotation"],
            "hflip": bool(row["hflip"]),
            "enabled": bool(row["enabled"]),
            "template": row["template"],
            "params": json.loads(row["params"]),
            "sleep_override": json.loads(row["sleep_override"]) if row["sleep_override"] else None,
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
            raise SnapshotError(
                f"integration {row['name']!r} of type {row['type']!r} has a stored credential,"
                " but nothing in the wire format can carry one"
            )
        config = json.loads(row["config"])
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
            "scenes": json.loads(row["scenes"]),
            "params_schema": json.loads(row["params_schema"]),
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
        payload["night"] = json.loads(settings["night"])

    try:
        config = DaemonConfig.model_validate(payload)
    except ValidationError as error:
        raise SnapshotError(f"daemon {daemon_id} has a configuration no daemon can run") from error

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
