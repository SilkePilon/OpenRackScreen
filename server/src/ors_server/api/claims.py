"""Routes on top of `ors_server.claims`: a rack files, an admin decides, a rack collects.

Two routers, not one, and the split is the whole access-control story (design
spec S6.3-S6.5). `public_router` carries the two a daemon reaches with no
credential at all -- filing a claim and polling one -- because a rack that has
not been approved holds nothing to authenticate with; that is the entire
reason the claim protocol exists rather than a shared secret. `router` carries
the three an admin's browser reaches, and is mounted on `app.py`'s
session-guarded `api` router exactly as every configuration route is, so
nobody has to remember to say `Depends(require_session)` here either.

**The admin-facing routes key on `fingerprint`, not on `claim.id`.** The claim
id is the bearer credential a daemon's poll authenticates with (design spec
S6.3 step 4, `claims.file_claim`'s own comment) -- the one thing that lets its
holder collect a pending grant -- and `GET /api/claims` is a page anyone who
can read the admin's screen can also read over the wire. Putting the id there
would publish every pending rack's capability to anything that could see one
response; the interface never needs it, so it never asks. `fingerprint` is the
one other column a pending claim carries, it is unique among still-pending
rows (`claim_pending_fingerprint`, `db.py`), and knowing it lets a caller
correlate rows and nothing else -- unlike the id, it decrypts no key and it
does not let a caller past `approve`'s own guard, which still sits behind a
session on the router in front of it. `claims.approve` and `claims.deny`
still take the true `claim.id` (that is `claims.py`'s own interface); this
module translates `fingerprint -> id` with one guarded `SELECT` first, on the
admin's own connection, and treats "no pending row for this fingerprint" as
404 -- the same answer a claim that was already granted or denied out from
under the click gives.

**The key handover is the one new piece of cryptography this task owns**
(design spec S6.3 step 5): `_seal` encrypts the minted daemon key to the
claim's ephemeral X25519 public key with X25519 + HKDF-SHA256 + AES-GCM, and
is passed to `claims.approve` as its `seal=` seam. It runs with SQLite's write
lock held (`approve`'s own docstring), so it is pure computation -- no I/O, no
network, nothing that can block the lock for longer than a key exchange and an
AEAD encrypt take.

**No discard-on-read.** `claims.approve` stores `seal`'s ciphertext in
`claim.granted_key` and never clears it; `GET /api/racks/claims/{id}` is
therefore a plain read of that column, the same ciphertext on every poll,
exactly the idempotent option design spec S6.3's forward note describes and
the store this module is built on already picked. A route that discarded the
blob after the first read would reopen the failure that note warns about: a
poll that merely observes or guesses a claim id would consume the one
delivery, and the legitimate daemon's next poll would then find nothing left
to collect and could never pair.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
from contextlib import closing
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ors_server import claims as claim_store
from ors_server.api.changes import one_row
from ors_server.auth import now

CLAIM_RATE_LIMIT_MAX_ATTEMPTS = 10
CLAIM_RATE_LIMIT_WINDOW_S = 60.0
"""The numbers `login`'s limiter uses (`auth.py`'s `_MAX_ATTEMPTS`,
`_WINDOW_SECONDS`), reused rather than shared: design spec S6.5 says "reuses
the existing login limiter", and `limiter.py`'s own module docstring is the
more specific authority on what that means in this codebase -- a *second*
`Limiter` instance with its own budget, because a rack filing claims must not
be able to lock an admin out of logging in by sharing a counter with them.
`app.py` constructs it once per app, on `app.state.claim_limiter`, the same
way `Sessions()` is.
"""

_HKDF_INFO = b"ors-claim-v1"
_PUBLIC_KEY_BYTES = 32


def _seal(daemon_key: str, daemon_public_key_b64: str) -> str:
    """Encrypt `daemon_key` to the claim's ephemeral X25519 public key.

    Returns one JSON string rather than the three-field shape design spec
    S6.3 step 5 describes, because `claims.approve`'s `seal` seam is typed
    `Callable[[str, str], str]` and `claim.granted_key` is one `TEXT` column
    (`db.py`) -- the three fields are folded back apart by `poll_claim`
    below, which is the only reader.

    X25519 rather than sealing to a long-lived key: the claim's key pair
    lives for one claim, so a secret recovered from a Pi later does not
    decrypt a handover that was recorded earlier. HKDF and not the raw shared
    secret, because X25519's output is not uniform and AES-GCM wants a key
    that is.

    `daemon_public_key_b64` is validated by `ClaimRequest.public_key` before
    it ever reaches `claim.public_key` in the database -- 32 raw bytes,
    valid base64, checked at filing time -- so this function is never handed
    anything that fails to decode or is the wrong length. That is what keeps
    `approve_claim` below free of a `try` around this seam: there is nothing
    left here that a claimant's input can make raise.
    """
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(daemon_public_key_b64))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(peer)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, daemon_key.encode(), None)
    return json.dumps(
        {
            "ephemeral_public_key": base64.b64encode(
                ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            ).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
    )


class ClaimRequest(BaseModel):
    """`POST /api/racks/claims`'s body (design spec S6.3 step 1).

    No `address` field, on purpose -- design spec S6.3 step 2 is explicit
    that the source address is recorded by the server from the connection,
    never taken from the body, because a field the claimant fills in is a
    field the claimant chooses. A caller that sends one anyway (an old
    client, or an adversarial one) is not refused for it: pydantic's default
    is to ignore unknown fields, which is what lets
    `test_the_recorded_address_is_the_connections_not_the_bodys` prove the
    field is ignored rather than merely testing that it is rejected.
    """

    hostname: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    short_code: str = Field(min_length=1)
    version: str = Field(min_length=1)
    public_key: str = Field(
        min_length=1,
        description="An ephemeral X25519 public key: 32 raw bytes, standard base64.",
    )

    @field_validator("public_key")
    @classmethod
    def _public_key_is_a_curve_point(cls, value: str) -> str:
        """Refuse anything `_seal` could not turn into a working handover.

        Checked here, at filing time, rather than left for `_seal` to
        discover while holding `claims.approve`'s write lock: `_seal` runs
        inside a `BEGIN IMMEDIATE` (its own docstring), and letting a
        malformed key reach it would mean the admin's approve click throws a
        500 out of a locked transaction instead of the filing daemon getting
        a clean 422 for the input it sent. X25519 has no invalid 32-byte
        string (RFC 7748), so length and base64 validity are the whole
        check.
        """
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("public_key must be valid base64") from error
        if len(raw) != _PUBLIC_KEY_BYTES:
            raise ValueError(f"public_key must decode to {_PUBLIC_KEY_BYTES} bytes")
        return value


class ClaimFiled(BaseModel):
    """`202`'s body: the claim id, and nothing else.

    Returned here and nowhere else -- not in `PendingClaim` below, not in any
    log line this module writes -- because it is the bearer credential a
    daemon's later poll authenticates with (design spec S6.3 step 4), and the
    only safe number of places to disclose a bearer credential is one.
    """

    claim_id: str


class ClaimPollResult(BaseModel):
    """`GET /api/racks/claims/{id}`'s body.

    The three sealed fields are `None` for a claim still `pending`, and
    filled in together for one `approved` -- never independently, because
    `claims.approve` writes `granted_key` in the same guarded `UPDATE` that
    sets `granted_at` (its own docstring), so a row this route can see as
    granted always has the blob to go with it.

    There is no `denied` status, although design spec S6.3's route table
    names one. `claims.deny` **deletes** the row and records only the
    fingerprint's suppression (`deny`'s own docstring) -- nothing in the
    schema distinguishes "denied" from "expired" from "never filed" once
    that row is gone, and inventing a way to tell them apart here would
    contradict the one guarantee `claims.deny` and `claims.file_claim` both
    document: those refusals must not be tellable apart by whoever asks.
    `poll_claim` below answers 404 for all three, identically.
    """

    status: Literal["pending", "approved"]
    ephemeral_public_key: str | None = None
    nonce: str | None = None
    ciphertext: str | None = None


class PendingClaim(BaseModel):
    """One row of `GET /api/claims` -- everything design spec S7 shows an
    admin, and nothing else. `claim.id` is deliberately absent; see the
    module docstring.
    """

    fingerprint: str
    hostname: str
    address: str
    short_code: str
    version: str
    first_seen: float


class ClaimApproved(BaseModel):
    """`POST /api/claims/{fingerprint}/approve`'s body: the rack that now
    exists, not the key -- the key reaches the daemon over
    `GET /api/racks/claims/{id}` and nowhere else, sealed, exactly once
    (design spec S6.3 step 5).
    """

    id: int
    name: str


public_router = APIRouter(tags=["claims"])
router = APIRouter(tags=["claims"])


@public_router.post("/racks/claims", status_code=202)
async def file_claim_route(request: Request, body: ClaimRequest) -> ClaimFiled:
    """A rack asks to be paired. Unauthenticated, necessarily -- see the
    module docstring and design spec S6.3 step 1: a daemon that has not been
    approved has no credential, which is the entire point of this route
    existing.

    **The address is `request.client.host`, never `body`** (`ClaimRequest`
    carries no such field at all) -- the connection is the one thing here a
    claimant cannot lie about, for the reason `auth.py`'s `login` gives for
    the identical choice: `X-Forwarded-For` is written by whoever sends it,
    so trusting a claimed address would let one attacker be a new "rack" on
    every request and meet no limit at all.

    **The limiter is asked before the store is touched, and only ever
    `record`ed after `too_many` has already been asked** -- `limiter.py`'s
    own warning: `record` never prunes, only `too_many` does, so a route that
    recorded without checking first would reopen the unbounded-dict leak
    `Limiter` exists to close, driven by anyone on the LAN who can reach this
    unauthenticated endpoint.

    **`file_claim`'s `None` is answered 429 either way**, whether the queue
    is full or this fingerprint is still suppressed from a recent deny.
    `file_claim`'s own docstring is explicit that a caller must not be able
    to tell those two refusals apart, the same way `pairing.claim_token`
    answers `None` for a token nobody minted and one already spent without
    distinguishing them -- a route that answered 403 for one and 429 for the
    other would hand back exactly the distinction the store was built to
    withhold, confirming to anyone probing a fingerprint that it was the one
    an admin had denied. Design spec S6.5's own failure table only ever
    names 429 for this endpoint's refusals, for the same reason.
    """
    state = request.app.state
    client = request.client.host if request.client else "unknown"
    limiter = state.claim_limiter
    moment = now()
    if limiter.too_many(client, moment):
        raise HTTPException(status_code=429, detail="too many claims from this address")
    limiter.record(client, moment)

    claim = claim_store.file_claim(
        state.database,
        hostname=body.hostname,
        address=client,
        fingerprint=body.fingerprint,
        short_code=body.short_code,
        version=body.version,
        public_key=body.public_key,
        now=moment,
    )
    if claim is None:
        raise HTTPException(status_code=429, detail="the claim queue is full")
    return ClaimFiled(claim_id=claim.id)


@public_router.get("/racks/claims/{claim_id}")
async def poll_claim(request: Request, claim_id: str) -> ClaimPollResult:
    """A rack asks whether it has been let in. Unauthenticated: the claim id
    itself is the bearer credential (design spec S6.3 step 4) -- there is no
    session to ask for, and no signature header either (see `claims.py`'s
    module docstring on why an HMAC-over-the-id draft was unimplementable).

    **A claim id that is unknown and one that is malformed answer
    identically** -- both reach `claim_store.get_claim`, which does a plain
    lookup and returns `None` for either, and both land on the same 404
    here. Nothing about this path validates the id's shape first, on
    purpose: a route that answered differently for "not base64" than for
    "well-formed but not in the table" would confirm to somebody enumerating
    ids that they were at least guessing the right shape.

    **The lookup is SQL equality on the primary key, not a scan with
    `hmac.compare_digest`.** An earlier draft of this task called for the
    latter, on the reasoning that a byte-at-a-time comparison of a bearer
    credential leaks its prefix to whoever can time this endpoint. It is not
    followed, for two reasons that are specific to this store rather than
    general. `claims.get_claim` (Task 12, already shipped) is the module's
    own interface for reading one claim and it does `WHERE id = ?`;
    duplicating that read here as `SELECT * FROM claim` plus a Python loop
    would mean this route no longer goes through the store at all, and it
    would still not be constant time -- the loop's length is the number of
    live claims, which is itself a signal, and `_expire` runs a `DELETE`
    ahead of every read on this path whose cost swamps a 43-byte compare.
    What actually keeps a guessed id from being ground out prefix-first is
    that `id` is a SQLite `TEXT PRIMARY KEY`: the comparison happens inside a
    B-tree descent against whichever rows the index visits, not against the
    one true value byte by byte, so the timing is a function of the tree, not
    of how many leading characters the guess got right.

    **`granted_key` is read with a second query, deliberately.**
    `claims.Claim` -- what `get_claim` returns -- does not carry the sealed
    blob at all; only the row itself does. A `granted_at IS NOT NULL` row
    that goes missing between the two reads (aged past
    `claims.CLAIM_LIFETIME_S`, in the moment between them) is the same 404
    as one that was never granted, not a crash: `row is None` is checked,
    not assumed away.
    """
    database = request.app.state.database
    claim = claim_store.get_claim(database, claim_id, now())
    if claim is None:
        raise HTTPException(status_code=404, detail="no such claim")
    if claim.granted_at is None:
        return ClaimPollResult(status="pending")

    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT granted_key FROM claim WHERE id = ? AND granted_at IS NOT NULL",
            (claim_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such claim")

    sealed = json.loads(row["granted_key"])
    return ClaimPollResult(
        status="approved",
        ephemeral_public_key=sealed["ephemeral_public_key"],
        nonce=sealed["nonce"],
        ciphertext=sealed["ciphertext"],
    )


def _pending_claim_id(connection: sqlite3.Connection, fingerprint: str) -> str:
    """The true `claim.id` for this fingerprint's still-pending row, or the
    404 both `approve_claim` and `deny_claim` answer for one that is not
    there -- already granted, already denied, expired, or never filed.
    `claim_pending_fingerprint` (`db.py`) guarantees at most one row matches.
    """
    row = one_row(
        connection,
        "SELECT id FROM claim WHERE fingerprint = ? AND granted_at IS NULL",
        (fingerprint,),
        missing="no pending claim with that fingerprint",
    )
    return str(row["id"])


@router.get("/claims")
async def list_claims(request: Request) -> list[PendingClaim]:
    """Design spec S7's "Waiting to join" list. Session-guarded by `api`
    (`app.py`), not by anything declared here.
    """
    pending = claim_store.list_pending(request.app.state.database, now())
    return [
        PendingClaim(
            fingerprint=claim.fingerprint,
            hostname=claim.hostname,
            address=claim.address,
            short_code=claim.short_code,
            version=claim.version,
            first_seen=claim.first_seen,
        )
        for claim in pending
    ]


@router.post("/claims/{fingerprint}/approve")
async def approve_claim(request: Request, fingerprint: str) -> ClaimApproved:
    """Design spec S7's Approve button: create the rack, mint its key, seal
    the key to the claim's ephemeral public key. The key itself is never in
    this response -- see `ClaimApproved`.

    **`sqlite3.IntegrityError` is a 409, not a 500.** `claims.approve`'s own
    docstring: a `daemon.name` collision with a rack this claim's fingerprint
    does not own raises out of a transaction that has already rolled back,
    leaving the claim exactly as pending and deniable as it was before this
    call -- loud and recoverable, which is what the route owes the admin who
    just clicked, not a stack trace.
    """
    database = request.app.state.database
    moment = now()
    with closing(database.connect()) as connection:
        claim_id = _pending_claim_id(connection, fingerprint)

    try:
        result = claim_store.approve(database, claim_id, moment, seal=_seal)
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="a daemon with that name already exists"
        ) from error
    if result is None:
        # The claim was granted or denied by someone else between the lookup
        # above and this call -- both hold the write lock, so this is a race
        # lost, not a bug. Same 404 as a fingerprint that was never pending.
        raise HTTPException(status_code=404, detail="no pending claim with that fingerprint")
    daemon_id, _key = result

    with closing(database.connect()) as connection:
        name_row = one_row(
            connection,
            "SELECT name FROM daemon WHERE id = ?",
            (daemon_id,),
            missing=f"no daemon {daemon_id}",
        )
    return ClaimApproved(id=daemon_id, name=name_row["name"])


@router.post("/claims/{fingerprint}/deny")
async def deny_claim(request: Request, fingerprint: str) -> dict[str, bool]:
    """Design spec S7's Deny button: remove the claim, suppress the
    fingerprint for `claims.DENY_SUPPRESSION_S`.
    """
    database = request.app.state.database
    moment = now()
    with closing(database.connect()) as connection:
        claim_id = _pending_claim_id(connection, fingerprint)

    if not claim_store.deny(database, claim_id, moment):
        # Same race as `approve_claim`'s: gone between the lookup and here.
        raise HTTPException(status_code=404, detail="no pending claim with that fingerprint")
    return {"ok": True}
