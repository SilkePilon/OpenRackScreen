"""The claim routes: a rack files, an admin decides, a rack collects (design
spec S6.3-S6.5).

Two decisions here differ from an earlier draft of this task's brief, in
favour of the spec and the store (`ors_server.claims`, Task 12) this router
is built on -- both are argued in `server/src/ors_server/api/claims.py`'s
module docstring, and restated briefly at each test that turns on them:

- `file_claim`'s `None` (queue full, or a fingerprint still under deny
  suppression) answers **429 either way**, not 403 for one and 429 for the
  other. `claims.file_claim`'s own docstring is explicit that a caller must
  not be able to tell those two refusals apart, and design spec S6.5's
  failure table only ever names 429 for this endpoint.
- A repeat poll of an approved claim answers the **same** ciphertext every
  time, not the ciphertext once and a bare `approved` after. `claims.py`'s
  own docstring: the blob is stored in `granted_key` and never cleared,
  which is the idempotent option design spec S6.3's forward note names, and
  a route that discarded it on first read would let a poll that merely
  observed a claim id consume the legitimate daemon's one delivery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from ors_server.api.claims import (
    CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS,
    CLAIM_POLL_RATE_LIMIT_WINDOW_S,
    CLAIM_RATE_LIMIT_MAX_ATTEMPTS,
    CLAIM_RATE_LIMIT_WINDOW_S,
    _seal,
)
from ors_server.app import AppSettings, create_app
from ors_server.claims import CLAIM_LIFETIME_S, MAX_PENDING
from ors_server.pairing import authenticate_key


def build(tmp_path) -> TestClient:
    return TestClient(create_app(AppSettings(data_dir=tmp_path)))


def logged_in(client: TestClient, password: str = "correct horse") -> TestClient:
    client.post("/api/auth/setup", json={"password": password})
    client.post("/api/auth/login", json={"password": password})
    return client


def ephemeral_keypair() -> tuple[X25519PrivateKey, str]:
    """A fresh X25519 keypair and its public half, base64, as a real daemon
    would generate one per claim (design spec S6.2)."""
    private_key = X25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, base64.b64encode(public_bytes).decode()


def fingerprint(n: int = 1) -> str:
    """Rack `n`'s fingerprint: a real SHA-256 in lowercase hex.

    It has to be real now. `ClaimRequest` recomputes `short_code` from this
    field and refuses a pair that does not agree, so the old `f"fp-{n:06x}"`
    -- which was never a digest and never had a derivable code -- is a body
    the route correctly answers 422. Hashing the hostname keeps the property
    that made the old spelling useful: distinct per `n`, and legible in a
    failure message as "the one belonging to rack-3".
    """
    return hashlib.sha256(f"rack-{n}".encode()).hexdigest()


def claim_body(*, n: int = 1, public_key: str | None = None) -> dict:
    """A well-formed filing body, fields chosen so no two calls collide."""
    return {
        "hostname": f"rack-{n}",
        "fingerprint": fingerprint(n),
        # Derived here rather than written out, because that is what the route
        # now checks; a literal would be a body that has to be re-derived by
        # hand every time `n` moves.
        "short_code": short_code_of(fingerprint(n)),
        "version": "1.2.3",
        "public_key": public_key or ephemeral_keypair()[1],
    }


def short_code_of(digest_hex: str) -> str:
    """The daemon's derivation, spelled out here rather than imported.

    A second, independent implementation, for `unseal`'s reason above: the
    production check is `ClaimRequest`'s call into
    `ors_schema.claim.derive_short_code`, and a test that asked that same
    function what the answer should be would agree with any edit to it. This
    is six characters of the base32 of the fingerprint's bytes -- what
    `ors_daemon.identity._derive` computes and what the Pi prints -- written
    the way a daemon's author would write it.
    """
    return base64.b32encode(bytes.fromhex(digest_hex)).decode("ascii")[:6]


def unseal(sealed: dict, private_key: X25519PrivateKey) -> str:
    """The daemon's half of `api.claims._seal`: recover the plaintext key
    with the private half of the claim's ephemeral keypair. Deliberately a
    second, independent implementation of the KDF/AEAD shape rather than a
    call into `_seal` or a shared helper -- this is the test that proves the
    production `_seal` produced something a real peer can actually open, and
    it would prove nothing if it reused `_seal`'s own code to open it. The
    HKDF `info` tag is the one piece of shared protocol knowledge a real
    daemon would also hardcode (it is not a secret, and both ends must agree
    on it to derive the same key), so it is spelled out here literally
    rather than imported from `api.claims._HKDF_INFO` -- which since Task 15
    is itself an alias for `ors_schema.CLAIM_HKDF_INFO`, shared with the real
    daemon client in `ors_daemon.join`. This copy is what still fails when
    that one definition and this end's use of it drift apart.

    What fails when they move *together* is the frozen vector -- but only
    through the test that runs production code, which at this end is
    `test_the_frozen_vector_is_the_format_the_live_seal_still_produces` below
    and **not** `test_the_sealed_blob_matches_a_frozen_wire_vector`. That one
    opens a constant with this function, so nothing in it is production and a
    coordinated edit leaves it passing; it is verified by mutation to do so.
    The daemon's `test_the_daemon_opens_a_frozen_wire_vector` does both jobs at
    once, because that end's reader is the shipped one.
    """
    peer_public = X25519PublicKey.from_public_bytes(
        base64.b64decode(sealed["ephemeral_public_key"])
    )
    shared = private_key.exchange(peer_public)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ors-claim-v1").derive(shared)
    nonce = base64.b64decode(sealed["nonce"])
    ciphertext = base64.b64decode(sealed["ciphertext"])
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()


def approve_by_fingerprint(client: TestClient, digest: str) -> dict:
    response = client.post(f"/api/claims/{digest}/approve")
    assert response.status_code == 200, response.text
    return response.json()


def test_filing_a_claim_answers_202_with_an_id_and_needs_no_session(tmp_path):
    """A daemon that has not been approved holds no credential (design spec
    S6.3 step 1) -- there is no cookie to send here."""
    client = build(tmp_path)

    response = client.post("/api/racks/claims", json=claim_body())

    assert response.status_code == 202
    body = response.json()
    assert isinstance(body["claim_id"], str)
    assert len(body["claim_id"]) > 20, "looks like the 256-bit token, not a placeholder"


def test_the_recorded_address_is_the_connections_not_the_bodys(tmp_path):
    """Design spec S6.3 step 2: the source address is recorded by the server
    from the connection, never taken from the body. `TestClient`'s default
    fake peer is `testclient`; a claimant that also sends `address` in the
    JSON body must not be able to override it."""
    client = logged_in(build(tmp_path))

    body = claim_body(n=1)
    body["address"] = "10.9.9.9"
    filed = client.post("/api/racks/claims", json=body)
    assert filed.status_code == 202

    pending = client.get("/api/claims").json()
    assert len(pending) == 1
    assert pending[0]["address"] == "testclient"
    assert pending[0]["address"] != "10.9.9.9"


def test_the_33rd_claim_answers_429(tmp_path):
    """`MAX_PENDING` (design spec S6.5's "Pending claims" row) is 32,
    enforced by refusing the claim that would exceed it -- never by evicting
    an older one (`claims.py`'s own docstring on why eviction is the attack
    this cap stops).

    Each claim comes from its own simulated source address -- a fresh
    `TestClient` bound to the same app, one per filing -- so that this test
    exercises the *queue* cap and not the *per-address* rate limiter, which
    is a separate limit with its own test below and would otherwise trip
    first from a single shared address.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    assert MAX_PENDING == 32, "the number this test's math is built on"

    for n in range(MAX_PENDING):
        addressed = TestClient(app, client=(f"10.0.{n // 256}.{n % 256}", 50000))
        response = addressed.post("/api/racks/claims", json=claim_body(n=n))
        assert response.status_code == 202, (n, response.text)

    one_more = TestClient(app, client=("10.9.9.9", 50000))
    response = one_more.post("/api/racks/claims", json=claim_body(n=MAX_PENDING))
    assert response.status_code == 429
    # The same body a *suppressed* refiling gets, in
    # `test_denying_suppresses_a_refiling_within_the_window` -- one string for
    # both refusals is the requirement (`claims.file_claim`'s own docstring),
    # and the string is one that is true of both. This case really is a full
    # queue; the other one usually is not, and the route cannot tell the
    # reader which it is without giving away the thing it is withholding.
    assert response.json() == {"detail": "claim refused"}


def test_the_rate_limiter_refuses_a_burst_from_one_address_before_the_store_is_touched(
    tmp_path,
):
    """Design spec S6.5's "Per-address rate" row. Every filing in this test
    comes from the same, single, default `TestClient` address, so it is the
    per-address limiter and not `MAX_PENDING` (32, well above
    `CLAIM_RATE_LIMIT_MAX_ATTEMPTS` here) that is exercised."""
    client = logged_in(build(tmp_path))
    # Pinned as literals, not compared to themselves: the budget and the
    # window are decisions (design spec S6.5 -- the numbers `login`'s limiter
    # already uses), and a test that only said
    # `CLAIM_RATE_LIMIT_MAX_ATTEMPTS == CLAIM_RATE_LIMIT_MAX_ATTEMPTS` would
    # go on passing if somebody widened the budget to a thousand.
    assert CLAIM_RATE_LIMIT_MAX_ATTEMPTS == 10
    assert CLAIM_RATE_LIMIT_WINDOW_S == 60.0

    for n in range(CLAIM_RATE_LIMIT_MAX_ATTEMPTS):
        response = client.post("/api/racks/claims", json=claim_body(n=n))
        assert response.status_code == 202, (n, response.text)

    over_the_limit = client.post(
        "/api/racks/claims", json=claim_body(n=CLAIM_RATE_LIMIT_MAX_ATTEMPTS)
    )
    assert over_the_limit.status_code == 429

    # "before the store is touched": the refused filing left no row behind,
    # so the pending list holds exactly the ones that were let through.
    assert len(client.get("/api/claims").json()) == CLAIM_RATE_LIMIT_MAX_ATTEMPTS


def test_the_admin_routes_all_answer_401_without_a_session(tmp_path):
    client = build(tmp_path)
    filed = client.post("/api/racks/claims", json=claim_body())
    assert filed.status_code == 202

    assert client.get("/api/claims").status_code == 401
    assert client.post(f"/api/claims/{fingerprint(1)}/approve").status_code == 401
    assert client.post(f"/api/claims/{fingerprint(1)}/deny").status_code == 401


def test_get_claims_with_a_session_lists_what_was_filed(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))

    pending = client.get("/api/claims").json()

    assert len(pending) == 1
    row = pending[0]
    assert row["hostname"] == "rack-1"
    assert row["fingerprint"] == fingerprint(1)
    assert row["short_code"] == short_code_of(fingerprint(1))
    assert row["version"] == "1.2.3"
    assert isinstance(row["first_seen"], float)


def test_approving_with_a_session_creates_the_rack(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))

    approved = approve_by_fingerprint(client, fingerprint(1))

    assert approved["name"] == "rack-1"
    assert isinstance(approved["id"], int)
    # The rack, and *not* the key. `ClaimApproved` carries two fields on
    # purpose (design spec S6.3 step 5): the minted daemon key reaches its
    # rack over `GET /api/racks/claims/{id}` sealed to the claim's ephemeral
    # public key and nowhere else, which is the entire reason `_seal` exists.
    # Adding it here would put a live credential in cleartext on plain HTTP
    # and in the admin's browser -- and every assertion above still passes
    # with it there, so the shape is pinned exactly rather than by name.
    assert set(approved) == {"id", "name"}
    listed = client.get("/api/daemons").json()
    assert [daemon["name"] for daemon in listed] == ["rack-1"]
    # And it is gone from the pending list -- `list_pending` excludes
    # granted rows (`claims.py`'s own docstring).
    assert client.get("/api/claims").json() == []


def test_polling_an_unknown_and_a_malformed_claim_id_answer_identically(tmp_path):
    """Design spec S6.3 step 4's own security property: a claim id nobody
    ever filed and one that is not even the right shape must not be
    tellable apart, or an endpoint that distinguished them would confirm a
    guessed id's validity to whoever is enumerating them."""
    client = build(tmp_path)

    unknown = client.get("/api/racks/claims/" + "a" * 43)
    malformed = client.get("/api/racks/claims/not-a-real-token!!")

    assert unknown.status_code == 404
    assert malformed.status_code == 404
    assert unknown.json() == malformed.json()


def test_the_claim_id_is_not_in_the_body_of_get_api_claims(tmp_path):
    """The claim id is the bearer credential a daemon's poll authenticates
    with; publishing it in the admin's list would hand the same capability
    to anything that could read one page (design spec S6.3 step 4)."""
    client = logged_in(build(tmp_path))
    filed = client.post("/api/racks/claims", json=claim_body(n=1))
    claim_id = filed.json()["claim_id"]

    pending = client.get("/api/claims")

    assert "id" not in pending.json()[0]
    assert claim_id not in pending.text


def test_polling_an_approved_claim_is_idempotent_not_discard_on_read(tmp_path):
    """`claims.approve` stores the sealed blob in `granted_key` and never
    clears it (its own docstring); this store's poll is a plain read, the
    same ciphertext on every call. See this file's module docstring for why
    that is followed here over an earlier brief calling for the opposite."""
    client = logged_in(build(tmp_path))
    private_key, public_key = ephemeral_keypair()
    filed = client.post("/api/racks/claims", json=claim_body(n=1, public_key=public_key))
    claim_id = filed.json()["claim_id"]
    approve_by_fingerprint(client, fingerprint(1))

    first = client.get(f"/api/racks/claims/{claim_id}")
    second = client.get(f"/api/racks/claims/{claim_id}")

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "approved"
    assert first.json() == second.json()
    assert second.json()["ciphertext"] is not None


def test_the_daemon_key_round_trips_and_authenticates(tmp_path):
    """The end-to-end assertion: everything else here could pass with an
    encryption that produces garbage. A real X25519 keypair is generated,
    used to file a claim, and its private half decrypts what an admin's
    approval sealed -- and the recovered key is not merely *some* string,
    it is one `pairing.authenticate_key` accepts for the rack that was
    created.
    """
    client = logged_in(build(tmp_path))
    private_key, public_key = ephemeral_keypair()
    filed = client.post("/api/racks/claims", json=claim_body(n=1, public_key=public_key))
    claim_id = filed.json()["claim_id"]

    approved = approve_by_fingerprint(client, fingerprint(1))

    polled = client.get(f"/api/racks/claims/{claim_id}")
    assert polled.status_code == 200
    sealed = polled.json()
    assert sealed["status"] == "approved"

    recovered_key = unseal(sealed, private_key)

    app = client.app
    daemon_id = authenticate_key(app.state.database, recovered_key)
    assert daemon_id == approved["id"]


def test_denying_suppresses_a_refiling_within_the_window(tmp_path):
    """Deny, then re-file the same rack: the refusal is the same 429
    `file_claim`'s `None` always answers with (see this file's module
    docstring for why not a distinguishing 403)."""
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))
    denied = client.post(f"/api/claims/{fingerprint(1)}/deny")
    assert denied.status_code == 200
    assert denied.json() == {"ok": True}

    refiled = client.post("/api/racks/claims", json=claim_body(n=1))

    assert refiled.status_code == 429
    # Byte-identical to the full-queue refusal above -- see
    # `test_the_33rd_claim_answers_429`. The queue here holds nothing at all,
    # which is why the old "the claim queue is full" was a false sentence
    # printed into the Pi's log for the commonest case of this refusal.
    assert refiled.json() == {"detail": "claim refused"}
    assert client.get("/api/claims").json() == []


def test_approving_a_daemon_name_collision_answers_409_not_500(tmp_path):
    """`claims.approve`'s own docstring: a `daemon.name` collision with a
    genuinely different rack raises `sqlite3.IntegrityError` out of a rolled
    -back transaction, and the route's job is to turn that into a 409 the
    admin can act on, not let it become a 500."""
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))
    approve_by_fingerprint(client, fingerprint(1))

    body = claim_body(n=2)
    body["hostname"] = "rack-1"  # collides with the daemon just minted
    client.post("/api/racks/claims", json=body)

    response = client.post(f"/api/claims/{fingerprint(2)}/approve")

    assert response.status_code == 409
    # Loud and recoverable: the claim is still there to retry with a
    # different name.
    assert len(client.get("/api/claims").json()) == 1


def test_a_public_key_that_is_not_32_bytes_is_refused_at_filing_not_at_approval(tmp_path):
    """Checked by `ClaimRequest`'s validator at filing time, so a malformed
    key never reaches `_seal`, which runs with `claims.approve`'s write lock
    held -- see `api/claims.py`'s docstrings on both."""
    client = build(tmp_path)

    body = claim_body(n=1, public_key=base64.b64encode(b"too-short").decode())
    response = client.post("/api/racks/claims", json=body)

    assert response.status_code == 422


def test_a_short_code_the_fingerprint_does_not_derive_is_refused(tmp_path):
    """**The check design spec S6.4's whole argument assumes.**

    Both fields used to be stored verbatim, so the claimant chose its own
    code: `{"fingerprint": "a"*64, "short_code": "ZZZZZZ"}` answered 202 and
    `ZZZZZZ` was what `GET /api/claims` and the admin's card showed. Anyone
    who had seen a rack's code -- on its screen, in `journalctl -u
    openrackscreen` -- could file under it with their own fingerprint and
    their own ephemeral key and be indistinguishable from that rack in the
    queue. S6.4 prices that attack at about 2^30 hashes; unchecked, it cost
    nothing.

    422 and not 429: see `ClaimRequest`'s validator for why this refusal is
    not one of the two `file_claim` keeps indistinguishable.
    """
    client = logged_in(build(tmp_path))

    body = claim_body(n=1)
    body["short_code"] = "ZZZZZZ"
    response = client.post("/api/racks/claims", json=body)

    assert response.status_code == 422
    # And nothing was recorded, so the refusal is a refusal and not a row an
    # admin could still be shown.
    assert client.get("/api/claims").json() == []


def test_the_code_a_real_daemon_derives_is_the_one_this_route_accepts(tmp_path):
    """The other half, and the reason this check cannot merely be strict.

    A pair spelled the way `ors_daemon.identity._derive` spells it is
    accepted, and the code the admin is shown is the one the Pi printed. The
    values are frozen rather than computed from `claim_body`: they are the
    SHA-256 of `b"shed"` and six characters of its base32, the same pair
    `web/tests/claims.test.tsx` and the daemon's own suite carry, so a
    server-side derivation that drifted from the daemon's -- a different
    alphabet, a different truncation, a `.lower()` -- fails here rather than
    becoming a rack that can never file at all.
    """
    client = logged_in(build(tmp_path))

    body = claim_body(n=1)
    body["fingerprint"] = "2f3c3e5cf3c63b648b44850ff5e9a88aac1d4498e94e7575f2fe6ad93f35c66b"
    body["short_code"] = "F46D4X"
    response = client.post("/api/racks/claims", json=body)

    assert response.status_code == 202, response.text
    [row] = client.get("/api/claims").json()
    assert row["short_code"] == "F46D4X"


def test_a_fingerprint_that_is_not_a_lowercase_sha256_is_refused(tmp_path):
    """Shape, and specifically case.

    `bytes.fromhex` accepts either case, so an uppercased fingerprint derives
    the very same code and would sail through the cross-check above -- while
    being a *different* string to `claim_pending_fingerprint`, to
    `claims.deny`'s suppression table and to every `WHERE fingerprint = ?` in
    this module, because SQLite compares TEXT byte-for-byte. A rack that was
    just denied could re-file by holding shift. One spelling per digest is
    what closes that; see `ors_schema.claim.FINGERPRINT_HEX_CHARS`.
    """
    client = build(tmp_path)

    body = claim_body(n=1)
    body["fingerprint"] = body["fingerprint"].upper()
    assert client.post("/api/racks/claims", json=body).status_code == 422

    # And the ordinary malformations, which used to be accepted too: this
    # column is a digest, not a free-text label.
    for bad in ("fp-000001", "abc", "z" * 64, body["fingerprint"].lower() + "00"):
        wrong = claim_body(n=1)
        wrong["fingerprint"] = bad
        assert client.post("/api/racks/claims", json=wrong).status_code == 422, bad


def test_a_public_key_that_is_not_base64_is_refused_at_filing(tmp_path):
    client = build(tmp_path)

    body = claim_body(n=1, public_key="not base64 at all !!")
    response = client.post("/api/racks/claims", json=body)

    assert response.status_code == 422


def test_approving_an_already_denied_fingerprint_answers_404(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))
    client.post(f"/api/claims/{fingerprint(1)}/deny")

    response = client.post(f"/api/claims/{fingerprint(1)}/approve")

    assert response.status_code == 404


def test_denying_an_unknown_fingerprint_answers_404(tmp_path):
    client = logged_in(build(tmp_path))

    assert client.post("/api/claims/no-such-fingerprint/deny").status_code == 404


def test_sealing_twice_to_the_same_peer_is_fresh_in_both_the_nonce_and_the_key():
    """Three facts about `_seal`, pinned together because each is harmless
    alone and their conjunction is the whole security of AES-GCM here.

    The ephemeral keypair is generated per call, so a frozen nonce alone
    never repeats a (key, nonce) pair; the nonce is random per call, so a
    static module-level keypair alone never repeats one either. **Both** at
    once is key-and-nonce reuse -- the keystream is the same for two
    messages, which is plaintext recovery from the XOR and, because the GHASH
    authentication key is recoverable from two tags under one key/nonce,
    forgery as well. A round-trip test decrypts one message and cannot see
    any of that: it passes with a hardcoded nonce, and it passes with a
    module-level `X25519PrivateKey.generate()`.

    The length is pinned as the literal `12` for the same reason the rate
    limiter's budget is: `os.urandom(16)` also round-trips, because AES-GCM
    accepts any nonce length, and 96 bits is the one size that goes straight
    into the counter block instead of through GHASH -- it is the size every
    peer implementation assumes, and the daemon that has to open this is not
    written yet.

    Peer key held **the same** across both calls on purpose: this is about
    what `_seal` varies by itself, not about what its argument varies for it.
    """
    _, peer_public_key = ephemeral_keypair()

    first = json.loads(_seal("a-daemon-key", peer_public_key))
    second = json.loads(_seal("a-daemon-key", peer_public_key))

    assert first["nonce"] != second["nonce"], "a repeated nonce under a repeated key is reuse"
    assert first["ephemeral_public_key"] != second["ephemeral_public_key"], (
        "a static ephemeral key makes every nonce a repeated one"
    )
    assert len(base64.b64decode(first["nonce"])) == 12
    assert len(base64.b64decode(second["nonce"])) == 12
    # And the two are not merely different headers over one ciphertext.
    assert first["ciphertext"] != second["ciphertext"]


def test_a_fingerprint_holding_a_granted_row_and_a_new_pending_one_resolves_to_the_pending_one(
    tmp_path,
):
    """Design spec S8's own failure row: "the approval arrives while the
    daemon is offline", so the rack files a new claim and is approved again.

    `claim_pending_fingerprint` is a *partial* unique index (`db.py`), so the
    granted row and the new pending row coexist under one fingerprint for up
    to `claims.CLAIM_LIFETIME_S` -- thirty minutes. `_pending_claim_id` reads
    with `fetchone`, so without its `AND granted_at IS NULL` it resolves to
    the older, granted row, `claims.approve` refuses that id, and every
    Approve *and* Deny click on the rack the admin is actually looking at
    answers 404 until the old row ages out.
    """
    client = logged_in(build(tmp_path))
    first_private_key, first_public_key = ephemeral_keypair()
    first_filed = client.post(
        "/api/racks/claims", json=claim_body(n=1, public_key=first_public_key)
    )
    approve_by_fingerprint(client, fingerprint(1))
    first_key = unseal(
        client.get(f"/api/racks/claims/{first_filed.json()['claim_id']}").json(),
        first_private_key,
    )

    # Offline through its own approval; it files again under the same
    # fingerprint, with a fresh ephemeral keypair as a restarted daemon would.
    second_private_key, second_public_key = ephemeral_keypair()
    refiled = client.post("/api/racks/claims", json=claim_body(n=1, public_key=second_public_key))
    assert refiled.status_code == 202, refiled.text
    assert [row["fingerprint"] for row in client.get("/api/claims").json()] == [fingerprint(1)]

    second = approve_by_fingerprint(client, fingerprint(1))

    # The 200 is the finding: without `AND granted_at IS NULL` this is a 404,
    # from `_pending_claim_id` handing `claims.approve` the granted row's id.
    assert second["name"] == "rack-1"
    # And it really is a *second* grant, not the first one read back -- the
    # rack's id is not the evidence for that (`approve` reclaims the name of
    # an uncollected grant to the same fingerprint, and SQLite hands the freed
    # rowid straight back), the credential is: the key sealed to the second
    # claim's keypair authenticates, and the first one no longer does.
    polled = client.get(f"/api/racks/claims/{refiled.json()['claim_id']}")
    assert polled.status_code == 200
    second_key = unseal(polled.json(), second_private_key)
    assert second_key != first_key
    database = client.app.state.database
    assert authenticate_key(database, second_key) == second["id"]
    assert authenticate_key(database, first_key) is None
    assert client.get("/api/claims").json() == []


def test_the_poll_endpoint_is_rate_limited_per_address_with_its_own_budget(tmp_path):
    """`GET /api/racks/claims/{id}` is unauthenticated *and* writes --
    `claims.get_claim` runs `_expire`'s three `DELETE`s before it reads -- so
    an unlimited poll is a write loop anyone on the LAN can drive without
    ever filing a claim. Every request in this test names an id nobody filed,
    which is the cheap case for the caller and the same cost for the server.

    The budget is pinned as a literal for the reason the filing limiter's is:
    a test that only fed the constant back into its own `range()` would go on
    passing if somebody widened it to a thousand.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    assert CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS == 60
    assert CLAIM_POLL_RATE_LIMIT_WINDOW_S == 60.0
    # A budget of its own, not the filing endpoint's: a daemon polls while it
    # waits and files again when its claim expires, and one shared counter
    # would let a patient rack spend the attempts it needs in order to
    # re-file.
    assert CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS != CLAIM_RATE_LIMIT_MAX_ATTEMPTS

    unknown_id = "a" * 43
    one_rack = TestClient(app, client=("10.0.0.1", 50000))
    for n in range(CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS):
        response = one_rack.get(f"/api/racks/claims/{unknown_id}")
        assert response.status_code == 404, (n, response.text)

    over_the_limit = one_rack.get(f"/api/racks/claims/{unknown_id}")

    assert over_the_limit.status_code == 429
    # **Per address**, which is the half of this a single-address test cannot
    # see: with one global counter every assertion above still passes, and
    # pairing then stops on a two-rack site with no attacker in it -- a rack
    # polling every two seconds spends half of the sixty, the second rack
    # spends the rest, and every rack after that is refused for as long as
    # the first two keep waiting on the admin's click.
    another_rack = TestClient(app, client=("10.0.0.2", 50000))
    assert another_rack.get(f"/api/racks/claims/{unknown_id}").status_code == 404

    # The filing budget is untouched by the polling: separate counters.
    assert one_rack.post("/api/racks/claims", json=claim_body(n=1)).status_code == 202


def test_approving_a_claim_records_a_created_event_like_pairing_by_token_does(tmp_path):
    """`POST /api/daemons` writes `info`/`created` for a rack minted from a
    pairing token, and design spec S7's status panel is a reader of that
    list. A rack that joins by claim is the rack this whole milestone exists
    for, and without a line here its history is empty for its entire life --
    the one kind of rack whose panel shows nothing.
    """
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))

    approved = approve_by_fingerprint(client, fingerprint(1))

    events = client.get(f"/api/events?daemon_id={approved['id']}").json()
    assert [(event["level"], event["kind"]) for event in events] == [("info", "created")]
    assert "rack-1" in events[0]["message"]


# A sealed blob produced by `_seal`'s algorithm before any of the constants
# below could be edited, kept as literals so that nothing this repository
# can change will make it decode again if the wire format moves.
#
# Not generated here and not derived from `api.claims`: that is the point.
# `_HKDF_INFO`, the HKDF length and salt, the AEAD and its associated data
# are protocol -- a Pi in the field holds the peer half of them in a release
# that shipped months ago. The round-trip test catches a *one-sided* drift
# (`unseal` above holds its own `b"ors-claim-v1"`, so mutating the production
# tag alone fails it), but it cannot catch a *coordinated* one: rename the
# tag, grep, update both copies, and every round trip still passes while
# every rack already installed stops being able to open its own key. This
# vector is the thing that fails in that case.
VECTOR_PEER_PRIVATE_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
VECTOR_SEALED = {
    "ephemeral_public_key": "NYBy1jZYgNGu6jKa35EhODhR7SGijjt16WXQ0s0WYlQ=",
    "nonce": "AAECAwQFBgcICQoL",
    "ciphertext": "O176Qz6Y6DEl4eOeoibBqifbq9oQP30KYVUgxXwvM0P7CXd1M+kp9gYcyBFtWmf5rRY=",
}
VECTOR_PLAINTEXT = "the-daemon-key-this-vector-freezes"


def test_the_sealed_blob_matches_a_frozen_wire_vector():
    """The wire format is a frozen artefact, not merely a self-consistent one.

    `unseal` is the written peer implementation this repository has, and it
    is what a daemon's author would copy. Everything else in this file proves
    the two halves agree with *each other* right now, which is exactly the
    property a coordinated edit preserves. Here the ciphertext is a constant:
    it was produced by the algorithm this module and `api.claims._seal`
    describe -- X25519, HKDF-SHA256 with `info=b"ors-claim-v1"`, no salt, a
    32-byte key, AES-GCM with a 12-byte nonce and no associated data -- and
    if any one of those moves, `unseal` stops recovering `VECTOR_PLAINTEXT`
    and this test says so, in the repository, before a rack in the field
    discovers it as a pairing that silently never completes.

    It needs no daemon, no server and no database: it is the format alone.
    """
    private_key = X25519PrivateKey.from_private_bytes(base64.b64decode(VECTOR_PEER_PRIVATE_KEY))

    assert unseal(VECTOR_SEALED, private_key) == VECTOR_PLAINTEXT


def test_the_frozen_vector_is_the_format_the_live_seal_still_produces():
    """The other half of the vector above: that it is still *this* code's
    output shape and not a fossil of an algorithm nothing runs any more.

    A frozen vector alone can rot -- if `_seal` moved and `unseal` moved with
    it, the vector would fail and the honest fix would be to regenerate it,
    which is the moment somebody has to decide whether the format may move at
    all. This test makes the pairing explicit: the peer key of the vector is
    fed to the live `_seal`, and what comes back is opened by the same
    `unseal` that opens the frozen blob. Both passing means the field format
    and the current format are one format.
    """
    private_key = X25519PrivateKey.from_private_bytes(base64.b64decode(VECTOR_PEER_PRIVATE_KEY))
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    fresh = json.loads(_seal(VECTOR_PLAINTEXT, base64.b64encode(public_bytes).decode()))

    assert set(fresh) == set(VECTOR_SEALED)
    assert unseal(fresh, private_key) == VECTOR_PLAINTEXT
    # A fresh seal is a fresh ephemeral key and nonce, so it is *not* the
    # vector byte for byte -- which is why the vector has to be a constant.
    assert fresh != VECTOR_SEALED


def test_polling_a_claim_that_aged_out_answers_404_like_one_never_filed(tmp_path, monkeypatch):
    """The route hands `now()` to `claims.get_claim`, and that argument is
    what ages a claim out (`claims._expire`).

    Expiry itself is the store's, exhaustively covered in `test_claims.py`;
    what is covered here is only that this route forwards the current moment
    rather than a constant. A route that passed `0.0` -- or any fixed value --
    would keep every claim ever filed alive and pollable forever from an
    unauthenticated endpoint, and `_expire`'s `DELETE`s would never run, so
    the `claim` table would grow without bound from anyone on the LAN.
    Nothing else in this file would notice: every other test polls inside the
    same instant it files.

    The clock is moved by patching `now` where this module reads it, which is
    the only seam there is -- `poll_claim` takes no time argument.
    """
    client = build(tmp_path)
    filed = client.post("/api/racks/claims", json=claim_body(n=1))
    claim_id = filed.json()["claim_id"]
    assert client.get(f"/api/racks/claims/{claim_id}").status_code == 200

    later = time.time() + CLAIM_LIFETIME_S + 1.0
    monkeypatch.setattr("ors_server.api.claims.now", lambda: later)

    polled = client.get(f"/api/racks/claims/{claim_id}")

    # The same 404, and the same body, an id nobody ever filed gets -- an
    # expired claim is not a distinguishable third answer.
    assert polled.status_code == 404
    assert polled.json() == client.get("/api/racks/claims/" + "a" * 43).json()
