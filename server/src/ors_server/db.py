from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
"""Bumped whenever the schema below changes.

There are no migrations: a bump exports the database and rebuilds it empty.
That is a deliberate trade for a single-file database holding a config you can
re-enter, and the export is what makes it survivable -- see `initialise`.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    token_hash     TEXT,
    paired_at      TEXT,
    version        TEXT,
    capabilities   TEXT NOT NULL DEFAULT '{}',
    last_seen      TEXT,
    status         TEXT NOT NULL DEFAULT 'unpaired',
    config_version INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    builtin       INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL DEFAULT 'general',
    scenes        TEXT NOT NULL,
    params_schema TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS screen (
    id             INTEGER PRIMARY KEY,
    daemon_id      INTEGER NOT NULL REFERENCES daemon(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    display        TEXT NOT NULL,
    rotation       INTEGER NOT NULL DEFAULT 0,
    hflip          INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    template       TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}',
    sleep_override TEXT
);

CREATE TABLE IF NOT EXISTS secret (
    id         INTEGER PRIMARY KEY,
    ciphertext TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration (
    id            INTEGER PRIMARY KEY,
    daemon_id     INTEGER NOT NULL REFERENCES daemon(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    name          TEXT NOT NULL,
    config        TEXT NOT NULL,
    secret_id     INTEGER REFERENCES secret(id) ON DELETE SET NULL,
    poll_interval REAL NOT NULL DEFAULT 5.0,
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_event (
    id        INTEGER PRIMARY KEY,
    daemon_id INTEGER REFERENCES daemon(id) ON DELETE CASCADE,
    at        TEXT NOT NULL,
    level     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""

# An export is a plaintext file beside the database, so nothing credential-bearing
# may reach it: `secret.ciphertext` is the encrypted credential itself, and
# `daemon.token_hash` is derived from a pairing token and worthless in an export
# besides, because a rebuild means re-pairing every daemon anyway.
#
# Keyed by the table name in the database being exported, which on a rebuild is
# the *old* schema's: renaming a table here without keeping its old name would
# quietly stop redacting the export that matters.
_REDACTED = {"secret": {"ciphertext"}, "daemon": {"token_hash"}}

# Everything not named above is exported verbatim, `integration.config` included,
# and that is deliberate: the export exists so a rack's integration configuration
# survives a schema bump, and a redacted `config` is one you have to retype, which
# is the whole thing this avoids.
#
# The constraint that makes it safe is the integrations API's to enforce:
# `integration.config` MUST NOT carry a credential. Credentials go in `secret`,
# encrypted, referenced by `integration.secret_id`. That covers the non-obvious
# forms too -- a URL may not embed `user:password@`, since `config` is a free
# string and a `https://user:pw@prom.local:9090` would land in this file in
# plaintext. Reject them at the boundary; do not redact them here.


class Database:
    """The one SQLite file, opened only by this process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        # `isolation_level=None` means autocommit: sqlite3 opens no implicit
        # transaction, so a statement lands when it runs and `PRAGMA
        # journal_mode` -- which SQLite refuses to change inside one -- always
        # has a transaction-free connection to run on.
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL so a reader is never blocked by the writer; foreign keys because
        # SQLite leaves them off by default and a screen whose daemon is gone is
        # a config the daemon cannot be given.
        #
        # Only one of these two is per-connection. `journal_mode = WAL` is
        # recorded in the database header and survives into every later
        # connection, so this is a no-op after the first; `foreign_keys` is
        # per-connection and back to OFF on the next one, so it has to be set
        # here rather than once at creation.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialise(self) -> Path | None:
        """Create or rebuild the schema. Returns the export path if it rebuilt."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Read before connecting: connecting creates an empty file, which would
        # make every database look like one that already existed.
        fresh = not self.path.exists()
        with closing(self.connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]

        export: Path | None = None
        if not fresh and version != SCHEMA_VERSION:
            # Export first: if it raises, the old database is still there to try
            # again from. The sidecars go with the file they belong to -- a -wal
            # left beside a rebuilt database of the same name is a WAL for an
            # inode that no longer exists.
            export = self._write_export()
            self.path.unlink()
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)

        with closing(self.connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return export

    def export(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every table, with the credential-bearing columns redacted."""
        dumped: dict[str, list[dict[str, Any]]] = {}
        with closing(self.connect()) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                rows = []
                # The table name is interpolated because it cannot be bound, and
                # quoted because it comes from whatever schema is on disk.
                statement = f'SELECT * FROM "{table}"'  # noqa: S608 - names from sqlite_master
                for row in connection.execute(statement):
                    record = dict(row)
                    for column in _REDACTED.get(table, ()):
                        record[column] = "<redacted>"
                    rows.append(record)
                dumped[table] = rows
        return dumped

    def _write_export(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"export-{stamp}.json")
        target.write_text(json.dumps(self.export(), indent=2, default=str))
        return target
