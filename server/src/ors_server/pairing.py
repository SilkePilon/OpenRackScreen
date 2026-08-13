from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from ors_server.db import Database

TOKEN_BYTES = 24
KEY_BYTES = 32
"""Bytes of randomness behind each credential, before base64.

A pairing token is typed -- or pasted -- into a terminal on the Pi, and lives
only until it is claimed. The key is written to a file by a program and lives as
long as the pairing, so it gets more. Both are far past anything guessable over
a network; the shorter one is 192 bits.
"""


def _fingerprint(credential: str) -> str:
    """What the database stores instead of the credential.

    sha256 and deliberately not argon2, although `auth.py` next door insists on
    argon2 for the admin password -- the two are not the same problem. A
    password is chosen by a human, comes from a space small enough to enumerate,
    and needs a hash slow enough to make enumerating it expensive. These are 192
    and 256 random bits from `secrets`: there is no dictionary to try, and no
    work factor makes 2**192 more infeasible than it already is. What sha256 is
    doing here is stopping the database file, a backup or a stolen volume from
    being a set of working credentials -- and for that, preimage resistance is
    the whole requirement.

    The other half of the reason is cost: this runs on every daemon connect,
    against every candidate row, on a server that may be a container on the same
    Pi. Argon2 at the RFC 9106 profile is 64 MiB and three passes *per row
    scanned*, which would turn a reconnect storm into a memory event.
    """
    return hashlib.sha256(credential.encode()).hexdigest()


# Scans, not lookups, and that is not an oversight. The comparison has to happen
# in Python -- `hmac.compare_digest`, below -- so an index on either column could
# not be used by a `WHERE token_hash = ?` this code does not write. It is also
# not worth wanting: the table holds one row per Pi in the rack, and SQLite is
# reading a handful of rows out of the page cache. If a deployment ever has
# enough daemons for this to matter, the answer is a lookup keyed by a
# non-secret prefix of the credential, not an index on the secret's hash.
_TOKEN_CANDIDATES = "SELECT id, token_hash AS fingerprint FROM daemon WHERE token_hash IS NOT NULL"
_KEY_CANDIDATES = "SELECT id, key_hash AS fingerprint FROM daemon WHERE key_hash IS NOT NULL"


def mint_token(database: Database, name: str) -> tuple[int, str]:
    """Create a daemon record and a one-time token. Only its hash is stored.

    Both halves of the return are needed by the one caller there will ever be:
    the interface's "add a daemon" endpoint shows the token exactly once, beside
    the `ors-daemon connect` command that carries it to the Pi, and addresses
    the daemon it just created by id from then on. There is no CLI for pairing
    by explicit design -- the browser is the mechanism, not a wrapper over one.

    `name` is unique in the schema, so a second daemon by the same name raises
    `sqlite3.IntegrityError` here. That is the API's to turn into a 409; it is
    not this function's business to decide what a name collision means.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with closing(database.connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO daemon (name, token_hash, status, created_at)"
            " VALUES (?, ?, 'unpaired', ?)",
            (name, _fingerprint(token), datetime.now(UTC).isoformat()),
        )
    return int(cursor.lastrowid), token


def claim_token(database: Database, token: str) -> tuple[int, str] | None:
    """Spend a pairing token; return whose it was and the key that replaces it.

    A token is single-use, so something else has to identify this daemon on
    every later connect. That something is the key returned here: the caller
    sends it down the socket that spent the token, the daemon stores it, and the
    server keeps only its fingerprint. Without it the only thing a reconnecting
    daemon offers is the hostname in its `hello`, which anyone on the network
    can type.

    None for a token nobody minted, for one already spent, and for no token at
    all -- the caller must not be able to tell those apart, and neither must
    whoever is listening to what the caller does next.
    """
    if not token:
        # An empty credential is a daemon that was never configured, and it must
        # not be allowed to reach the scan: `token_hash` is NULL on every paired
        # row, and a comparison that ever treated "no credential" as "the row
        # with no token" would hand out every identity on the rack.
        return None
    presented = _fingerprint(token)
    key = secrets.token_urlsafe(KEY_BYTES)
    with closing(database.connect()) as connection:
        daemon_id = _matching_id(connection, _TOKEN_CANDIDATES, presented)
        if daemon_id is None:
            return None
        if not _spend_token(connection, daemon_id, presented, _fingerprint(key)):
            return None
    return daemon_id, key


def authenticate_key(database: Database, key: str) -> int | None:
    """Whose key this is, or None. The credential every connect after the first."""
    if not key:
        return None
    with closing(database.connect()) as connection:
        return _matching_id(connection, _KEY_CANDIDATES, _fingerprint(key))


def _matching_id(connection: sqlite3.Connection, statement: str, presented: str) -> int | None:
    """The row whose stored fingerprint is this one, compared in constant time.

    `hmac.compare_digest` rather than `==` on principle rather than because a
    timing attack here is plausible: what leaks is the stored hash, and
    recovering a credential from it means a preimage of sha256 over 192 random
    bits. The principle is that credential comparisons in this codebase all look
    the same, so nobody has to work out which ones were safe by accident.
    """
    for row in connection.execute(statement):
        if hmac.compare_digest(row["fingerprint"], presented):
            return int(row["id"])
    return None


def _spend_token(
    connection: sqlite3.Connection, daemon_id: int, presented: str, key_fingerprint: str
) -> bool:
    """Swap the token for the key, or refuse because someone else got there first.

    The refusal has to be the write itself. Two sockets can present the same
    token at the same moment -- a daemon retrying a connect it thinks timed out
    is the ordinary way -- and a `SELECT` that found the row followed by an
    `UPDATE` lets both of them believe they claimed it. Both then hold a key for
    one identity and each overwrote the other's, so whichever wrote second is
    the only one that can ever reconnect. This is `claim_password`'s problem and
    it takes the same shape of answer: one statement, therefore one SQLite
    transaction, with the condition that must still hold in its `WHERE`.

    `token_hash = ?` here is a plain SQL comparison rather than a constant-time
    one, which is right: the caller has already matched this fingerprint in
    constant time, and what this clause is asking is not "is this the
    credential" but "is the row still in the state I read".
    """
    row = connection.execute(
        "UPDATE daemon SET token_hash = NULL, key_hash = ?, paired_at = ?, status = 'paired'"
        " WHERE id = ? AND token_hash = ? RETURNING id",
        (key_fingerprint, datetime.now(UTC).isoformat(), daemon_id, presented),
    ).fetchone()
    return row is not None
