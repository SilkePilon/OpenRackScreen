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
from collections.abc import Callable
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

    Neither `DELETE` here touches the `daemon` table, on purpose, and that is
    not the shape this function had a round ago. A granted row that ages out
    can leave behind a `daemon` row `approve` minted before delivery to the
    daemon was guaranteed, and this module once had a background sweep here
    that deleted it too, restricted to `last_seen IS NULL`. That sweep is
    gone: `last_seen` is not "has this daemon ever connected", it is
    "connected within the last `CLAIM_LIFETIME_S`" -- `link/ws_daemon.py`'s
    `_record_hello` and `_touch` are the only writers, and neither fires
    until a websocket session opens, which the design spec (S4.3) describes
    happening well after approval: the `config.txt` SPI edit means a rack
    commonly reboots, or waits on a human to reboot it, between collecting
    its key and ever dialling in. A `daemon` row minted a minute ago by a
    still-rebooting, still-`'paired'`-and-correctly-configured rack has
    `last_seen IS NULL` too, indistinguishable here from a true orphan -- and
    this function runs from every read in this module, including the
    unauthenticated `POST /api/racks/claims` path, so a sweep here fires
    constantly and on a clock nobody watching the admin's screen controls.
    Deleting that row deletes the rack: `screen` and `integration` both
    reference `daemon.id` `ON DELETE CASCADE` (`db.py`), so an admin's
    configuration of it is destroyed in the same instant, silently, and the
    rack -- which still believes it is paired and holds a key that now
    authenticates to nothing -- never files a new claim to recover. That is
    strictly worse than the collision this sweep existed to avoid: a
    `daemon.name` collision on re-approval is a loud `sqlite3.IntegrityError`
    an admin sees and can act on (`approve`'s own docstring), where a sweep
    here is a silent, permanent unpairing with no error for anyone.

    Colliding names are instead resolved in `approve`, against a `daemon` row
    provably tied to the *same* claim's fingerprint, not against any row that
    merely happens to share the pending claim's `first_seen`/`granted_at`
    clock. See `approve`'s own docstring for that mechanism, and its limits.
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

    `None` covers two refusals a caller must not be able to tell apart --
    the queue is full, or this fingerprint was denied and is still
    suppressed -- exactly as `pairing.claim_token` answers `None` for a token
    nobody minted and one already spent without distinguishing them.

    **What that buys, stated exactly, because it is less than it reads
    like.** One answer cannot be told from the other, and that is all: a
    caller picks its own fingerprint, so it can file a *fresh random* one
    first -- 202 means the queue is not full -- and then file the target,
    where a 429 now means suppression. The distinction survives as a
    two-request probe and no answer this function can give closes it, because
    the prober learns the queue's state from a filing that is entirely its
    own business.

    That is recorded rather than fixed, deliberately. Closing it means either
    refusing the probe's own honest filing (a real rack's first claim, on a
    server whose queue has room) or making a full queue answer 202 and drop
    the row -- a rack that believes it is waiting and never will be. Both cost
    a real rack its pairing to deny an attacker a fact that is worth little on
    its own: knowing a fingerprint was denied tells you nothing you can act
    on, since the suppression expires on its own clock either way and the row
    it protects has already been deleted. What must not happen, and does not,
    is a *single* response distinguishing them -- that would confirm a denial
    to anyone who could guess a fingerprint, without ever filing anything.

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


def _seal_not_implemented(key: str, public_key: str) -> str:
    """`approve`'s default `seal` -- always raises, naming the task that owes it.

    Sealing the minted key to the claim's ephemeral X25519 public key (design
    spec S6.3 step 5) is real cryptography this module does not own; Task 13
    does. This default exists so `approve` has a seam to take it through
    (`approve(..., seal=<the real one>)`) rather than a hardcoded call to
    something that does not exist in this codebase yet, while making it
    impossible to reach production behaviour by omission: nothing here calls
    `approve` without passing `seal`, so the only way to hit this by accident
    is a caller that forgets the argument, which is exactly what naming
    Task 13 in the message is for. That caller now exists --
    `api.claims.approve_claim` passes `seal=_seal`, and it is the only
    production one -- so this default is no longer a placeholder for a route
    that has not been written; it is the guard on the one that has.
    """
    raise NotImplementedError(
        "claims.approve() has no key-sealing primitive: pass seal= (Task 13's"
        " X25519 seal, design spec S6.3 step 5) -- there is no default that"
        " does the sealing, because approving a claim without one would leave"
        " a live credential with nowhere safe to be encrypted to."
    )


def _rollback(connection: sqlite3.Connection) -> None:
    """Undo an open transaction, without hiding why it is being undone.

    `ROLLBACK` on a connection SQLite has *already* rolled back itself --
    which it does for `SQLITE_FULL`, `SQLITE_IOERR`, `SQLITE_BUSY` and
    `SQLITE_NOMEM` -- raises `cannot rollback - no transaction is active`.
    Raised from inside an `except` block, that replaces the disk-full or
    I/O error the caller needed to see with a message about transactions.
    """
    try:
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def approve(
    database: Database,
    claim_id: str,
    now: float,
    *,
    seal: Callable[[str, str], str] = _seal_not_implemented,
) -> tuple[int, str] | None:
    """Approve a pending claim: create its daemon, mint a key, hand it over once.

    `seal(key, public_key)` is the caller's X25519 seal (design spec S6.3
    step 5, Task 13's to supply) -- this module owns none of that
    cryptography, so it takes it as a seam instead. The default,
    `_seal_not_implemented`, always raises; there is nothing safe it could do
    instead, since minting a key with no way to encrypt it would mean
    handing a live credential to a caller with nothing to do with it.

    **One transaction, not three autocommitted statements.** A `BEGIN
    IMMEDIATE` takes the write lock before anything is read, so the guarded
    read (`SELECT ... WHERE granted_at IS NULL`), `INSERT INTO daemon` and
    the guarded `UPDATE claim SET granted_at = ?, granted_key = ?, daemon_id
    = ?` all land together or not at all. The three-statement shape this
    replaced (`UPDATE claim SET granted_at` / `INSERT INTO daemon` / `UPDATE
    claim SET daemon_id`) ran under `Database.connect`'s autocommit
    (`isolation_level=None`): each statement committed the instant it ran, so
    a `daemon.name` collision on the second statement raised
    `sqlite3.IntegrityError` out of a claim that the *first* statement had
    already, irreversibly, marked granted. The claim was then permanently
    unapprovable (the guard says granted), undeniable (`deny`'s own guard
    says the same), and invisible (`list_pending` excludes granted rows) --
    the exact crash-standing-in-for-a-clean-`None` failure the guard was
    built to eliminate in the first place, relocated from the loser of a
    race onto the claim itself. Folding all three into one transaction closes
    that: on `IntegrityError` the whole transaction rolls back, the claim's
    `granted_at` is never set, and it is exactly as pending as it was before
    this call -- `list_pending` still shows it, a corrected retry can still
    approve it, and the caller sees the `IntegrityError` propagate, which is
    the API's to turn into a 409 (the same division of labour
    `mint_token_on`'s docstring describes for the same collision).

    The guard is `granted_at`, not `daemon_id`, on purpose, independent of
    the transaction change above: the new `daemon` row does not exist yet at
    the moment the guarded read happens, so `daemon_id` cannot be part of
    what decides "still pending" -- it is filled in a moment later, in the
    same statement that sets `granted_at`, once minting the daemon is known
    to be safe. `granted_at IS NULL` is therefore what "still pending" means
    everywhere in this module (`file_claim`, `list_pending`, `count_pending`,
    `claim_pending_fingerprint`), and the partial index
    `claim_pending_fingerprint` is keyed on it for the same reason (see
    `db.py`'s schema comment, and the whitebox test pinning that the index
    and this guard name the same column).

    The row is **not** deleted. Row deletion was an earlier, rejected shape,
    and it broke the protocol outright: the design spec (S6.3 steps 4-5)
    describes the daemon collecting its key on a *separate* poll,
    `GET /api/racks/claims/{id}`, after this call has already returned to the
    admin's click -- a poll against a row that no longer exists always finds
    nothing, and pairing can never complete. Keeping the row is also what
    lets a repeat poll re-send the same thing rather than finding it gone,
    which is the design spec's own forward note on the poll route: it must be
    either idempotent or hold its discard until the daemon acknowledges
    receipt. Storing `seal`'s ciphertext in `granted_key` rather than handing
    only the plaintext back is what makes idempotent the free choice here --
    a repeat poll is then a plain `SELECT granted_key FROM claim WHERE id =
    ?`, the same ciphertext every time, needing nothing remembered anywhere
    else. A granted row does not live forever either: see `_expire` and
    `CLAIM_LIFETIME_S`'s docstring for its own retention, and for what
    happens to the `daemon` row this call mints if the poll never comes.

    **`seal` is called with the SQLite write lock held.** `BEGIN IMMEDIATE`
    takes it before the guarded `SELECT`, and `seal` runs between that and
    the `INSERT`, so it must be pure computation: no I/O, no network, no
    lock of its own. An X25519 seal is sub-millisecond and fine; an HSM or a
    remote KMS dropped in later would hold every reader of this module out
    for the length of the call, because `_expire` makes even `get_claim` a
    writer.

    The plaintext key itself still never touches the database: `key` is
    returned to this call's own caller and nowhere else, `seal(key,
    public_key)` -- not `key` -- is what lands in `granted_key`, and
    `_fingerprint(key)` is what `daemon.key_hash` stores, the same helper and
    the same column `pairing.claim_token` writes to, so a daemon paired by a
    claim authenticates through `authenticate_key` exactly as one paired by a
    token does. A sealed blob at rest is not the same thing `granted_key`'s
    old NULL was avoiding: the server holds no private half of the claim's
    ephemeral keypair, so `seal`'s output is no more a working credential to
    a reader of the database file than `secret.ciphertext` is (see `db.py`'s
    schema comment on `granted_key` for the fuller version of this point).
    """
    with closing(database.connect()) as connection:
        _expire(connection, now)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT public_key, hostname, version, fingerprint FROM claim"
                " WHERE id = ? AND granted_at IS NULL",
                (claim_id,),
            ).fetchone()
            if row is None:
                _rollback(connection)
                return None

            key = token_urlsafe(KEY_BYTES)
            blob = seal(key, row["public_key"])
            stamp = datetime.now(UTC).isoformat()
            # Reclaim the name from an earlier grant to THIS SAME RACK that
            # was never collected, and from nothing else. Without it, a rack
            # whose first grant aged out unread re-files under the same
            # hostname and every re-approval afterwards dies on `daemon.name`
            # -- the row it needs the name back from is one this module minted
            # and nobody ever used, but `INSERT` cannot know that.
            #
            # All three conditions are load-bearing and the fingerprint is
            # the one that matters. It is read off `daemon.claim_fingerprint`
            # and not off the claim table, because `_expire` has already
            # deleted the claim that minted this row -- that expiry IS the
            # scenario -- so the link has to live where it survives.
            #
            # `_expire` used to do this as a background
            # sweep keyed on `last_seen IS NULL` alone, and that deleted live
            # racks: `last_seen` is written only by `link/ws_daemon.py`'s
            # `_record_hello` and `_touch`, so a rack that collected its key
            # and is still rebooting -- routine, since the `config.txt` SPI
            # edit requires one -- looked exactly like an orphan, and `screen`
            # and `integration` cascade off `daemon.id`, so the admin's
            # configuration went with it. Scoped to this claim's own
            # fingerprint, the only row reachable is one that stands for an
            # earlier attempt by the rack being approved right now.
            #
            # A DIFFERENT rack that merely shares a hostname is deliberately
            # NOT reclaimed: the `INSERT` below raises `IntegrityError`, the
            # transaction rolls back, the claim stays pending and deniable,
            # and the route answers 409. Loud and recoverable, which is what
            # the sweep traded away.
            connection.execute(
                "DELETE FROM daemon WHERE name = ? AND last_seen IS NULL AND claim_fingerprint = ?",
                (row["hostname"], row["fingerprint"]),
            )
            cursor = connection.execute(
                "INSERT INTO daemon"
                " (name, key_hash, version, status, paired_at, created_at, claim_fingerprint)"
                " VALUES (?, ?, ?, 'paired', ?, ?, ?)",
                (
                    row["hostname"],
                    _fingerprint(key),
                    row["version"],
                    stamp,
                    stamp,
                    row["fingerprint"],
                ),
            )
            daemon_id = int(cursor.lastrowid)
            updated = connection.execute(
                "UPDATE claim SET granted_at = ?, granted_key = ?, daemon_id = ?"
                " WHERE id = ? AND granted_at IS NULL RETURNING id",
                (now, blob, daemon_id, claim_id),
            ).fetchone()
            # Not checked for `None`. `BEGIN IMMEDIATE` took the write lock
            # before the guarded `SELECT` above ran, so no other writer can
            # have set `granted_at` in between -- a branch here would be
            # unreachable code hiding a `return None` that cannot happen.
            assert updated is not None, "the guarded SELECT and UPDATE disagree"
        except BaseException:
            _rollback(connection)
            raise
        connection.execute("COMMIT")
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
