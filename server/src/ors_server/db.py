from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5
"""Bumped whenever the schema below changes.

There are no migrations: a bump exports the database and rebuilds it empty.
That is a deliberate trade for a single-file database holding a config you can
re-enter, and the export is what makes it survivable -- see `initialise`.

5, not 4, even though 4 was never released (no tag, no build on `master` ever
carried it): it was a committed, reviewable state on this branch (`36439ba`),
and a database created from *that* commit is version 4 without `granted_at`,
`granted_key` or `daemon_id` on `claim`. Upgrading such a file today raises
`sqlite3.OperationalError: no such column: granted_at` out of `claims.py`, not
out of anything in this module -- the version already matches `SCHEMA_VERSION`,
so `initialise` takes the "nothing changed" branch and never exports or
rebuilds. That is an undiagnosable start failure for the exact people most
likely to hit it: every dev or CI checkout that made a database on this branch
before this round. Bumping the number costs nothing and turns that into the
ordinary export-and-rebuild path.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    token_hash     TEXT,
    -- Two credentials with two lifetimes, so two columns. `token_hash` is the
    -- one-time pairing token and is nulled the moment it is spent; `key_hash`
    -- is the key issued in its place, which the daemon presents on every
    -- connect thereafter. Rotating a pairing from the interface is then exactly
    -- "clear `key_hash`, write a new `token_hash`" -- the daemon is
    -- disconnected until it is paired again, which is what a rotate button
    -- should mean. Sharing one column would make that state unwritable.
    key_hash       TEXT,
    paired_at      TEXT,
    version        TEXT,
    capabilities   TEXT NOT NULL DEFAULT '{}',
    last_seen      TEXT,
    status         TEXT NOT NULL DEFAULT 'unpaired',
    config_version INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    -- Which of the two a credential is, decided by the schema rather than by
    -- convention. `pairing.py` reasons that one string can match at most one of
    -- those columns, because a token's hash is deleted the moment it is spent,
    -- and everything downstream leans on that -- including the order the daemon
    -- socket tries them in. Nothing enforced it: a row holding both
    -- authenticates by its key, and the next claim of its token silently
    -- revokes that live key out from under a connected daemon. Harmless while
    -- `claim_token` is the only thing that writes here, and load-bearing the
    -- moment there is a re-pair endpoint -- which is precisely the code most
    -- likely to write a new token without clearing the old key.
    --
    -- A table constraint rather than a column one only because SQLite will not
    -- take a CHECK naming two columns in the middle of a column list.
    CHECK (token_hash IS NULL OR key_hash IS NULL)
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

CREATE TABLE IF NOT EXISTS claim (
    id           TEXT PRIMARY KEY,
    hostname     TEXT NOT NULL,
    address      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    short_code   TEXT NOT NULL,
    version      TEXT NOT NULL,
    public_key   TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    -- Ciphertext, written by `claims.approve` -- sealed to the claim's
    -- ephemeral `public_key` (design spec S6.3 step 5), never the plaintext
    -- key itself. That is not in tension with "a live credential never
    -- touches the database in the clear": `secret.ciphertext` is redacted
    -- from the export below for the same reason and is exactly this kind of
    -- column -- encrypted still counts. The server holds no private half of
    -- the ephemeral keypair (only the daemon does), so a sealed blob sitting
    -- here is not a working credential to anyone who does not already hold
    -- that private key; storing it is what makes the poll route's repeat
    -- reads (design spec's own forward note on S6.3 step 5) idempotent by
    -- construction -- `GET /api/racks/claims/{id}` becomes a plain read of
    -- this column, the same ciphertext every time, rather than something
    -- that has to be minted once and remembered elsewhere. `public_key` and
    -- `fingerprint` stay on the row alongside it because that is what
    -- `approve` needs on hand to build the ciphertext in the first place.
    granted_key  TEXT,
    -- NULL means still pending. Set, atomically, by the one guarded UPDATE
    -- that decides an approval race (`claims.approve`'s docstring) -- this
    -- column, not `daemon_id`, is what "no longer pending" means everywhere
    -- in `claims.py`, because it is set in that same guarded statement and
    -- `daemon_id` necessarily follows a moment later (the new `daemon` row
    -- does not exist yet when the guard fires). Also the clock a granted
    -- row's own retention counts from -- see `claims._expire`.
    granted_at   REAL,
    daemon_id    INTEGER REFERENCES daemon(id) ON DELETE CASCADE
);

-- `fingerprint` is unique only among still-pending claims, not across the
-- whole table's history. A completed claim's row now survives its own
-- approval (see the `granted_at` comment above), so a plain UNIQUE column
-- would let it go on occupying its fingerprint's only slot forever -- and the
-- one rack it belongs to could never file again through this path if it ever
-- needed to (a database rebuilt out from under a paired daemon, for one).
-- Qualifying the index by `granted_at IS NULL` instead means a granted row
-- steps out of the way the moment it is granted, and a re-file lands a fresh
-- pending row rather than colliding with the row it replaces.
CREATE UNIQUE INDEX IF NOT EXISTS claim_pending_fingerprint
    ON claim(fingerprint) WHERE granted_at IS NULL;

CREATE TABLE IF NOT EXISTS denied_fingerprint (
    fingerprint  TEXT PRIMARY KEY,
    denied_at    REAL NOT NULL
);
"""

# An export is a plaintext file beside the database, so nothing credential-bearing
# may reach it: `secret.ciphertext` is the encrypted credential itself, and
# `daemon.token_hash` is derived from a pairing token and worthless in an export
# besides, because a rebuild means re-pairing every daemon anyway. `key_hash` is
# the same rule and matters more: the key it is derived from is presented on
# every connect for the life of the pairing, not once.
#
# `claim.id` is not derived from a credential -- it *is* one, verbatim: design
# spec S6.3 step 4 makes the claim id itself the bearer credential a daemon's
# poll authenticates with (see `claims.file_claim`'s own comment on the
# `INSERT`), so exporting it in the clear would hand a plaintext file on disk
# the one thing that lets its reader collect a pending grant. `claim.granted_key`
# is `secret.ciphertext`'s case again, not `token_hash`'s: it is sealed to the
# claim's ephemeral public key, not plaintext, but encrypted still counts, and a
# rebuild discards it exactly as it discards `key_hash` -- there is nothing here
# worth keeping in the clear either.
#
# Keyed by the table name in the database being exported, which on a rebuild is
# the *old* schema's: renaming a table here without keeping its old name would
# quietly stop redacting the export that matters.
_REDACTED = {
    "secret": {"ciphertext"},
    "daemon": {"token_hash", "key_hash"},
    "claim": {"id", "granted_key"},
}

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
        dumped: dict[str, Any] = {}
        with closing(self.connect()) as connection:
            # Read off the file, not from the constant compiled into this build.
            # An export is written before the stale database is dropped, so its
            # rows are the *old* schema's -- and this number is how a restorer
            # decides which columns they have. `SCHEMA_VERSION` here would date
            # a version-2 dump as whatever the code doing the rebuilding is.
            dumped["_schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
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
                        # Only if the column is really there: an export is
                        # written from the *old* schema, which may predate it,
                        # and inventing a redacted field describes a database
                        # that never existed.
                        if column in record:
                            record[column] = "<redacted>"
                    rows.append(record)
                dumped[table] = rows
        return dumped

    def _write_export(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"export-{stamp}.json")
        target.write_text(json.dumps(self.export(), indent=2, default=str))
        return target
