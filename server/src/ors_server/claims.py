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
"""Thirty minutes, counted from two different clocks depending on the row.

For a still-pending claim: how long it occupies a slot before it is treated
as though it were never filed. A daemon that is still trying files a new one
(see `file_claim`'s update-in-place path) and is unaffected; this number only
bounds how long a claim nobody is ever going to approve sits in the list an
admin is scanning, and how long it holds the one slot its fingerprint
reserves via `claim_pending_fingerprint`.

For a granted claim awaiting its daemon's poll: the same window, counted from
`granted_at` instead of `first_seen` (see `_expire`). Reusing the constant
rather than adding a second one is deliberate -- a legitimately polling
daemon collects its key within seconds of an admin's click, and a daemon that
does not is in exactly the situation an unapproved claim is: nobody is
usefully waiting for it any more, and its slot should free up rather than
being held forever pending a poll that may never come.
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
    daemon_id: int | None = None
    granted_at: float | None = None


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
        daemon_id=row["daemon_id"],
        granted_at=row["granted_at"],
    )


def _expire(connection: sqlite3.Connection, now: float) -> None:
    """Drop what this moment has aged out, on the caller's connection.

    A physical `DELETE`, not a filter added to every read: a `list_pending`
    that merely excluded stale rows would leave them sitting on
    `claim_pending_fingerprint` and counting against `MAX_PENDING` forever, so
    a rack that tried once and moved on would go on refusing the slot its old
    attempt reserved -- and the `claim` table would grow without bound from
    anyone on the LAN who ever files and is never approved, the same shape of
    leak `Limiter` avoids by sweeping in `too_many`.

    Two separate `DELETE`s, not one, because a pending and a granted row are
    aged out by different clocks: a pending row is stale `CLAIM_LIFETIME_S`
    after `first_seen`, and must **not** touch a granted one, or an approval
    made late in a claim's pending life would be swept before the daemon's
    poll ever sees it -- the exact bug this qualifier exists to prevent. A
    granted row is stale `CLAIM_LIFETIME_S` after `granted_at` instead (see
    `CLAIM_LIFETIME_S`'s docstring for why that clock and that window).
    """
    connection.execute(
        "DELETE FROM claim WHERE granted_at IS NULL AND first_seen <= ?",
        (now - CLAIM_LIFETIME_S,),
    )
    connection.execute(
        "DELETE FROM claim WHERE granted_at IS NOT NULL AND granted_at <= ?",
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

    A second filing under a fingerprint that is still *pending* (`granted_at
    IS NULL`) is **not** a second row: it updates `address`, `version` and
    `public_key` in place and keeps the original `id` and `first_seen`.
    `address` changes with DHCP and `version` with an upgrade, so the newer
    values are the honest ones; `public_key` is described by the design spec
    as ephemeral and generated per claim, so a retry that regenerated its
    keypair must overwrite the old one here too -- an approval encrypted to a
    stale `public_key` would be undecryptable by a daemon that has since
    discarded the matching private half, a permanent and silent pairing
    failure. `first_seen` does not move, because it is the answer to "how
    long has this rack been trying", and a chatty retry loop resetting its
    own clock every few seconds would make a real, ignored rack look brand
    new forever. This is also what keeps a restarting daemon from filling the
    queue by itself: `claim_pending_fingerprint` means its own retries can
    only ever occupy the one pending slot they already hold.

    A fingerprint whose only row has already been *granted* is a different
    rack-story, not a retry of this one -- the protocol already finished for
    it once -- so this filing lands a brand new row with a fresh `id` and
    `first_seen`, exactly as if the fingerprint had never been seen before.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        if connection.execute(
            "SELECT 1 FROM denied_fingerprint WHERE fingerprint = ?", (fingerprint,)
        ).fetchone():
            return None

        existing = connection.execute(
            "SELECT id FROM claim WHERE fingerprint = ? AND granted_at IS NULL", (fingerprint,)
        ).fetchone()
        if existing is not None:
            claim_id = existing["id"]
            connection.execute(
                "UPDATE claim SET address = ?, version = ?, public_key = ? WHERE id = ?",
                (address, version, public_key, claim_id),
            )
        else:
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM claim WHERE granted_at IS NULL"
            ).fetchone()["n"]
            if pending >= MAX_PENDING:
                return None
            # The claim id is the bearer credential a daemon's later poll
            # authenticates with (design spec S6.3 step 4), not a row label --
            # `secrets.token_urlsafe(32)` is the spec's own pinned generator
            # and width. A uuid4, by contrast, is only 122 bits with 6 fixed,
            # and every other id in this codebase is exactly the
            # non-secret row label it looks like; reusing that shape here
            # would read as harmless to a future maintainer and would not be.
            claim_id = token_urlsafe(32)
            connection.execute(
                "INSERT INTO claim"
                " (id, hostname, address, fingerprint, short_code, version, public_key, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (claim_id, hostname, address, fingerprint, short_code, version, public_key, now),
            )

        row = connection.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
        return _row_to_claim(row)


def list_pending(database: Database, now: float) -> list[Claim]:
    """Every claim still waiting on an admin, oldest first -- granted claims excluded."""
    with closing(database.connect()) as connection:
        _expire(connection, now)
        rows = connection.execute(
            "SELECT * FROM claim WHERE granted_at IS NULL ORDER BY first_seen ASC"
        ).fetchall()
    return [_row_to_claim(row) for row in rows]


def get_claim(database: Database, claim_id: str, now: float) -> Claim | None:
    """One claim by id, pending or granted, or `None` for one gone or never filed.

    Unlike `list_pending`, this is **not** filtered to pending rows: it is
    also what a daemon's `GET /api/racks/claims/{id}` poll reads (design spec
    S6.3 step 4), and a poll has to be able to find a claim that was just
    granted in order to collect anything from it.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    return _row_to_claim(row) if row is not None else None


def count_pending(database: Database, now: float) -> int:
    """How many claims are in the queue right now -- what `MAX_PENDING` bounds."""
    with closing(database.connect()) as connection:
        _expire(connection, now)
        return int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM claim WHERE granted_at IS NULL"
            ).fetchone()["n"]
        )


def approve(database: Database, claim_id: str, now: float) -> tuple[int, str] | None:
    """Approve a pending claim: create its daemon, mint a key, hand it over once.

    The guarded `UPDATE ... WHERE id = ? AND granted_at IS NULL RETURNING *`
    is the one statement that decides an approval race, exactly as
    `pairing._spend_token`'s guarded `UPDATE` decides a token race: two
    callers approving the same id at once can both reach this function, but
    SQLite serialises the two `UPDATE`s, and only the one whose `WHERE`
    still matches gets to mint a key at all -- the other's `RETURNING` is
    empty and it returns `None` before ever reaching `INSERT INTO daemon`.
    (A guard built the other way round -- insert the `daemon` row first and
    let its own `UNIQUE(name)` decide the race -- was the earlier, rejected
    shape: the loser would still have raised `sqlite3.IntegrityError` out of
    a write it should never have attempted, a crash standing in for a clean
    `None`.)

    The guard is `granted_at`, not `daemon_id`, on purpose: the new `daemon`
    row does not exist yet at the moment the race has to be decided, so
    `daemon_id` cannot be part of the same atomic statement that decides it
    -- it is filled in a moment later, once minting the daemon is safe to do
    because this call has already won. `granted_at IS NULL` is therefore what
    "still pending" means everywhere in this module (`file_claim`,
    `list_pending`, `count_pending`, `claim_pending_fingerprint`), so a reader
    who catches the row between the guard firing and `daemon_id` landing
    still sees it correctly as no-longer-pending.

    The row is **not** deleted. Row deletion was the earlier, rejected shape,
    and it broke the protocol outright: the design spec (S6.3 steps 4-5)
    describes the daemon collecting its key on a *separate* poll,
    `GET /api/racks/claims/{id}`, after this call has already returned to the
    admin's click -- a poll against a row that no longer exists always finds
    nothing, and pairing can never complete. Keeping the row is also what
    lets a repeat poll re-send the same thing rather than finding it gone,
    which is the design spec's own forward note on the poll route: it must be
    either idempotent or hold its discard until the daemon acknowledges
    receipt, and idempotent needs exactly this -- a survivor to serve from.
    A granted row does not live forever either: see `_expire` and
    `CLAIM_LIFETIME_S`'s docstring for its own retention.

    `RETURNING *`, not a named subset: whatever calls this next (the poll
    route) needs `public_key` and `fingerprint` off the *same* row to build
    the encrypted handover the spec describes, and a second `get_claim` call
    to fetch them would reopen the read-then-write race this guard exists to
    close.

    The plaintext key itself still never touches the database: `granted_key`
    is left NULL here, exactly as before, and `_fingerprint(key)` is what
    `daemon.key_hash` stores instead -- the same helper and the same column
    `pairing.claim_token` writes to, so a daemon paired by a claim
    authenticates through `authenticate_key` exactly as one paired by a token
    does. A future poll route that wants to *deliver* the key still has to
    encrypt it to `public_key` and store that ciphertext in `granted_key`
    itself; minting one here without anywhere safe to put it would only be
    handing a live credential to a caller with nothing to do with it yet.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute(
            "UPDATE claim SET granted_at = ? WHERE id = ? AND granted_at IS NULL RETURNING *",
            (now, claim_id),
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
        daemon_id = int(cursor.lastrowid)
        connection.execute("UPDATE claim SET daemon_id = ? WHERE id = ?", (daemon_id, claim_id))
        return daemon_id, key


def deny(database: Database, claim_id: str, now: float) -> bool:
    """Remove a pending claim and suppress its fingerprint for `DENY_SUPPRESSION_S`.

    `False` for a claim that is not there to deny -- already granted, already
    expired, or an id nobody ever filed -- so a caller cannot use this to
    learn which of those it was. Qualified by `granted_at IS NULL` for the
    same reason `file_claim`'s duplicate check is: a claim id that has
    already been granted is not this function's to remove, and letting a
    stray deny of one through would delete a row a daemon's poll might still
    be about to collect from and, worse, suppress a fingerprint that was
    just legitimately approved.

    An *expired* pending claim also answers `False` here, deliberately left
    that way rather than reaching past `_expire`'s sweep to record the
    fingerprint anyway: by the time an admin's click reaches this function
    the row is already gone by the same rule every other read in this module
    obeys, and resurrecting just enough of it to write a suppression entry
    would special-case `deny` against that rule for a case the 30-minute
    window already mostly handles by itself -- a claim nobody approved in
    half an hour is not showing up in the list to be clicked on much longer
    anyway. The one real gap this leaves -- a chatty rack whose claim ages
    out between an admin opening the list and clicking deny gets no
    suppression from that click -- is accepted, not unnoticed.
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        row = connection.execute(
            "DELETE FROM claim WHERE id = ? AND granted_at IS NULL RETURNING fingerprint",
            (claim_id,),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "INSERT INTO denied_fingerprint (fingerprint, denied_at) VALUES (?, ?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET denied_at = excluded.denied_at",
            (row["fingerprint"], now),
        )
        return True
