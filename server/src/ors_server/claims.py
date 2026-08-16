"""The queue between a rack that has never been paired and an admin's click.

`pairing.py` is the credential half of joining a server: a token pasted once,
spent once, replaced by a key. This module is what M3c puts in front of it --
a rack that has never seen a token at all files a claim, an authenticated
admin approves or denies it from the interface, and only then does a key
exist. `file_claim` is reachable by **anyone on the LAN**, by necessity: a
daemon that has not been approved holds no credential to prove it should be
allowed to ask. Every limit in this file exists because of that one fact.

`approve` mints the same shape of credential `pairing.claim_token` does --
`secrets.token_urlsafe(KEY_BYTES)`, stored as only its `_fingerprint` -- and is
deliberately built on those two names rather than a second hash-and-compare.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_urlsafe
from uuid import uuid4

from ors_server.db import Database
from ors_server.pairing import KEY_BYTES, _fingerprint

MAX_PENDING = 32
"""The most claims the queue holds at once. A decision, not a default: 32 is
comfortably above any number of racks a single admin is ever approving in one
sitting, and the cap exists so that the unauthenticated endpoint in front of
`file_claim` cannot be used to exhaust the server. Enforced by **refusing**
the claim that would exceed it, in `file_claim` -- never by evicting an older
one. Eviction is the attack this cap exists to stop: a flood that pushed a
real rack's claim out of the list would hide it from the admin looking at
that list, which is worse than a flood that is merely refused.
"""

CLAIM_LIFETIME_S = 1800.0
"""Thirty minutes. How long an unapproved claim occupies a slot before it is
treated as though it were never filed. A daemon that is still trying files a
new one (see `file_claim`'s update-in-place path) and is unaffected; this
number only bounds how long a claim nobody is ever going to approve sits in
the list an admin is scanning, and how long it holds the one slot its
fingerprint reserves via the `UNIQUE` constraint.
"""

DENY_SUPPRESSION_S = 86400.0
"""Twenty-four hours. How long a denied fingerprint is refused before it may
file again. Long enough that one click stops a chatty rack from reappearing
in the queue it was just removed from every few seconds; not permanent,
because a fingerprint is not a life sentence -- a rack that was denied by
mistake, or genuinely reformed, should not need an admin to remember to clear
a deny list by hand.
"""


@dataclass(frozen=True, slots=True)
class Claim:
    """One rack's request to be approved, as read back from the `claim` table."""

    id: str
    hostname: str
    address: str
    fingerprint: str
    short_code: str
    version: str
    public_key: str
    first_seen: float


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"],
        hostname=row["hostname"],
        address=row["address"],
        fingerprint=row["fingerprint"],
        short_code=row["short_code"],
        version=row["version"],
        public_key=row["public_key"],
        first_seen=row["first_seen"],
    )


def _expire(connection: sqlite3.Connection, now: float) -> None:
    """Drop what this moment has aged out, on the caller's connection.

    A physical `DELETE`, not a filter added to every read: a `list_pending`
    that merely excluded stale rows would leave them sitting on the `UNIQUE
    fingerprint` and counting against `MAX_PENDING` forever, so a rack that
    tried once and moved on would go on refusing the slot its old attempt
    reserved -- and the `claim` table would grow without bound from anyone on
    the LAN who ever files and is never approved, the same shape of leak
    `Limiter` avoids by sweeping in `too_many`.

    Approved claims are excluded from both deletes' business by construction:
    `approve` removes its row in the same statement that consumes it (see
    `approve`'s docstring for why), so nothing bearing `daemon_id` is ever
    read by these two `DELETE`s to begin with.
    """
    connection.execute(
        "DELETE FROM claim WHERE first_seen <= ?",
        (now - CLAIM_LIFETIME_S,),
    )
    connection.execute(
        "DELETE FROM denied_fingerprint WHERE denied_at <= ?",
        (now - DENY_SUPPRESSION_S,),
    )


def file_claim(
    database: Database,
    *,
    hostname: str,
    address: str,
    fingerprint: str,
    short_code: str,
    version: str,
    public_key: str,
    now: float,
) -> Claim | None:
    """File a claim, or refresh the pending one this fingerprint already holds.

    `None` covers three refusals a caller must not be able to tell apart --
    the queue is full, or this fingerprint was denied and is still
    suppressed -- exactly as `pairing.claim_token` answers `None` for a token
    nobody minted and one already spent without distinguishing them.

    A second filing under a fingerprint already pending is **not** a second
    row: it updates `address` and `version` in place and keeps the original
    `id` and `first_seen`. `address` changes with DHCP and `version` with an
    upgrade, so the newer values are the honest ones; `first_seen` does not
    move, because it is the answer to "how long has this rack been trying",
    and a chatty retry loop resetting its own clock every few seconds would
    make a real, ignored rack look brand new forever. This is also what keeps
    a restarting daemon from filling the queue by itself: the `UNIQUE
    fingerprint` constraint means its own retries can only ever occupy the one
    slot they already hold.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        if connection.execute(
            "SELECT 1 FROM denied_fingerprint WHERE fingerprint = ?", (fingerprint,)
        ).fetchone():
            return None

        existing = connection.execute(
            "SELECT id FROM claim WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing is not None:
            claim_id = existing["id"]
            connection.execute(
                "UPDATE claim SET address = ?, version = ? WHERE id = ?",
                (address, version, claim_id),
            )
        else:
            pending = connection.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]
            if pending >= MAX_PENDING:
                return None
            claim_id = uuid4().hex
            connection.execute(
                "INSERT INTO claim"
                " (id, hostname, address, fingerprint, short_code, version, public_key, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (claim_id, hostname, address, fingerprint, short_code, version, public_key, now),
            )

        row = connection.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
        return _row_to_claim(row)


def list_pending(database: Database, now: float) -> list[Claim]:
    """Every claim currently waiting on an admin, oldest first."""
    with closing(database.connect()) as connection:
        _expire(connection, now)
        rows = connection.execute("SELECT * FROM claim ORDER BY first_seen ASC").fetchall()
    return [_row_to_claim(row) for row in rows]


def get_claim(database: Database, claim_id: str, now: float) -> Claim | None:
    """One pending claim by id, or `None` for one that does not exist or has expired."""
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    return _row_to_claim(row) if row is not None else None


def count_pending(database: Database, now: float) -> int:
    """How many claims are in the queue right now -- what `MAX_PENDING` bounds."""
    with closing(database.connect()) as connection:
        _expire(connection, now)
        return int(connection.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"])


def approve(database: Database, claim_id: str, now: float) -> tuple[int, str] | None:
    """Approve a pending claim: create its daemon, mint a key, hand it over once.

    The `DELETE ... RETURNING` is the one statement that decides this, exactly
    as `pairing._spend_token`'s guarded `UPDATE` decides a token race: two
    callers approving the same id at once can both reach this function, but
    SQLite serialises the two `DELETE`s, and only the one that finds a row
    still there gets to mint a key at all -- the other's `RETURNING` is empty
    and it returns `None` before touching `daemon`.

    The row is removed rather than kept and marked approved. `fingerprint` is
    `UNIQUE` across the whole table, and a completed claim kept as history
    would go on occupying that fingerprint's slot -- so the one rack it
    belongs to could never file again through this path if it ever needed to
    (a database rebuilt out from under a paired daemon, for one). Deleting it
    is also the plainest reading of "handed over exactly once": once this
    call returns, there is no row left to ask a second time, so there is
    nothing for a second `approve` of the same id to hand over -- it returns
    `None`, and the earlier caller's key is the only one that was ever minted
    for this claim.

    The key itself never touches the database: `_fingerprint(key)` is what
    `daemon.key_hash` stores, the same helper and the same column
    `pairing.claim_token` writes to, so a daemon paired by a claim
    authenticates through `authenticate_key` exactly as one paired by a token
    does -- there is only one key-checking path in this codebase, not two.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute(
            "DELETE FROM claim WHERE id = ? RETURNING hostname, version", (claim_id,)
        ).fetchone()
        if row is None:
            return None

        key = token_urlsafe(KEY_BYTES)
        stamp = datetime.now(UTC).isoformat()
        cursor = connection.execute(
            "INSERT INTO daemon (name, key_hash, version, status, paired_at, created_at)"
            " VALUES (?, ?, ?, 'paired', ?, ?)",
            (row["hostname"], _fingerprint(key), row["version"], stamp, stamp),
        )
        return int(cursor.lastrowid), key


def deny(database: Database, claim_id: str, now: float) -> bool:
    """Remove a pending claim and suppress its fingerprint for `DENY_SUPPRESSION_S`.

    `False` for a claim that is not there to deny -- already approved, already
    expired, or an id nobody ever filed -- so a caller cannot use this to
    learn which of those it was.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute(
            "DELETE FROM claim WHERE id = ? RETURNING fingerprint", (claim_id,)
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "INSERT INTO denied_fingerprint (fingerprint, denied_at) VALUES (?, ?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET denied_at = excluded.denied_at",
            (row["fingerprint"], now),
        )
        return True
