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

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from ors_server.api.claims import CLAIM_RATE_LIMIT_MAX_ATTEMPTS, CLAIM_RATE_LIMIT_WINDOW_S
from ors_server.app import AppSettings, create_app
from ors_server.claims import MAX_PENDING
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


def claim_body(*, n: int = 1, public_key: str | None = None) -> dict:
    """A well-formed filing body, fields chosen so no two calls collide."""
    return {
        "hostname": f"rack-{n}",
        "fingerprint": f"fp-{n:06x}",
        "short_code": f"CODE{n:02d}",
        "version": "1.2.3",
        "public_key": public_key or ephemeral_keypair()[1],
    }


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
    rather than imported from `api.claims._HKDF_INFO`.
    """
    peer_public = X25519PublicKey.from_public_bytes(
        base64.b64decode(sealed["ephemeral_public_key"])
    )
    shared = private_key.exchange(peer_public)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ors-claim-v1").derive(shared)
    nonce = base64.b64decode(sealed["nonce"])
    ciphertext = base64.b64decode(sealed["ciphertext"])
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()


def approve_by_fingerprint(client: TestClient, fingerprint: str) -> dict:
    response = client.post(f"/api/claims/{fingerprint}/approve")
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
    assert client.post("/api/claims/fp-000001/approve").status_code == 401
    assert client.post("/api/claims/fp-000001/deny").status_code == 401


def test_get_claims_with_a_session_lists_what_was_filed(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))

    pending = client.get("/api/claims").json()

    assert len(pending) == 1
    row = pending[0]
    assert row["hostname"] == "rack-1"
    assert row["fingerprint"] == "fp-000001"
    assert row["short_code"] == "CODE01"
    assert row["version"] == "1.2.3"
    assert isinstance(row["first_seen"], float)


def test_approving_with_a_session_creates_the_rack(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))

    approved = approve_by_fingerprint(client, "fp-000001")

    assert approved["name"] == "rack-1"
    assert isinstance(approved["id"], int)
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
    approve_by_fingerprint(client, "fp-000001")

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

    approved = approve_by_fingerprint(client, "fp-000001")

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
    denied = client.post("/api/claims/fp-000001/deny")
    assert denied.status_code == 200
    assert denied.json() == {"ok": True}

    refiled = client.post("/api/racks/claims", json=claim_body(n=1))

    assert refiled.status_code == 429
    assert client.get("/api/claims").json() == []


def test_approving_a_daemon_name_collision_answers_409_not_500(tmp_path):
    """`claims.approve`'s own docstring: a `daemon.name` collision with a
    genuinely different rack raises `sqlite3.IntegrityError` out of a rolled
    -back transaction, and the route's job is to turn that into a 409 the
    admin can act on, not let it become a 500."""
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))
    approve_by_fingerprint(client, "fp-000001")

    body = claim_body(n=2)
    body["hostname"] = "rack-1"  # collides with the daemon just minted
    client.post("/api/racks/claims", json=body)

    response = client.post("/api/claims/fp-000002/approve")

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


def test_a_public_key_that_is_not_base64_is_refused_at_filing(tmp_path):
    client = build(tmp_path)

    body = claim_body(n=1, public_key="not base64 at all !!")
    response = client.post("/api/racks/claims", json=body)

    assert response.status_code == 422


def test_approving_an_already_denied_fingerprint_answers_404(tmp_path):
    client = logged_in(build(tmp_path))
    client.post("/api/racks/claims", json=claim_body(n=1))
    client.post("/api/claims/fp-000001/deny")

    response = client.post("/api/claims/fp-000001/approve")

    assert response.status_code == 404


def test_denying_an_unknown_fingerprint_answers_404(tmp_path):
    client = logged_in(build(tmp_path))

    assert client.post("/api/claims/no-such-fingerprint/deny").status_code == 404
