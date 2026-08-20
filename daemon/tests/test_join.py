"""What a freshly installed rack does between `install` and its first push.

Nothing here opens a socket. The HTTP client is injected exactly the way
`discovery` injects its browse -- `FakeHttp` below answers a scripted sequence
per endpoint and records what it was asked -- and the discovery step is a
plain callable, so a test that wants two servers on the LAN just says so.
`sleeper` is injected for the same reason: every backoff in this module is a
number the tests read rather than a wait they sit through.

**The sealed blob is opened by a second implementation of the format**, not by
`join.open_claim_key` (see `seal` below). That is the same discipline
`server/tests/test_api_claims.py::unseal` keeps at the other end, and for the
same reason: a test that opened the ciphertext with the code that has to open
it in the field would agree with any edit to either.

**And the frozen vector is the same bytes the server's test freezes.** It is
copied literally from `server/tests/test_api_claims.py`'s `VECTOR_SEALED`,
deliberately: that blob was produced before either end's constants could be
edited, so a coordinated rename of the HKDF tag -- which every round trip in
this repository would still pass -- fails here, in the daemon, which is the
end that ships to a Pi and has to keep opening keys sealed by servers that
were installed months earlier.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from ors_daemon.discovery import Found
from ors_daemon.identity import Identity
from ors_daemon.join import (
    BACKOFF_CAP_S,
    BACKOFF_FACTOR,
    BACKOFF_FIRST_S,
    HTTP_TIMEOUT_S,
    POLL_INTERVAL_S,
    join_a_server,
    open_claim_key,
    server_from_url,
)
from ors_daemon.link import load_link_settings

# Every field distinct from every other, and none of them a prefix or a
# rotation of another: two values that coincide make a mix-up between them
# invisible, and this record's whole job is to be copied into five different
# fields of one JSON body.
IDENTITY = Identity(
    secret=bytes(range(32)),
    fingerprint="fpr-6d1e9c40aa27",
    short_code="Q7K2ZM",
)
HOSTNAME = "rack-in-the-cupboard"
VERSION = "9.8.7-under-test"

SERVER = Found(host="192.0.2.10", port=8080, version="0.2.0", scheme="http")
OTHER_SERVER = Found(host="198.51.100.77", port=9090, version="0.2.0", scheme="http")

FILING_URL = "http://192.0.2.10:8080/api/racks/claims"

# --- a peer implementation of the seal, and the frozen wire vector ----------


def seal(plaintext: str, public_key_b64: str) -> dict[str, str]:
    """The server's half of the handover, written out here rather than
    imported from `ors_server.api.claims._seal` -- the daemon package does not
    depend on the server package and never will, and a test that sealed with
    the very code the production reader was written beside would prove only
    that the two agree with each other.
    """
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(peer)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ors-claim-v1").derive(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return {
        "ephemeral_public_key": base64.b64encode(
            ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


VECTOR_PEER_PRIVATE_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
VECTOR_SEALED = {
    "ephemeral_public_key": "NYBy1jZYgNGu6jKa35EhODhR7SGijjt16WXQ0s0WYlQ=",
    "nonce": "AAECAwQFBgcICQoL",
    "ciphertext": "O176Qz6Y6DEl4eOeoibBqifbq9oQP30KYVUgxXwvM0P7CXd1M+kp9gYcyBFtWmf5rRY=",
}
VECTOR_PLAINTEXT = "the-daemon-key-this-vector-freezes"


# --- the injected seams -----------------------------------------------------


@dataclass
class Reply:
    """The two things this module reads off an HTTP response."""

    status_code: int
    body: Any = None

    def json(self) -> Any:
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


def pending() -> Reply:
    return Reply(200, {"status": "pending"})


def filed(claim_id: str) -> Reply:
    return Reply(202, {"claim_id": claim_id})


def refused() -> Reply:
    """What the server answers for a full queue *and* for a suppressed
    fingerprint -- one string for both, deliberately indistinguishable
    (`api/claims.py`'s `file_claim_route`)."""
    return Reply(429, {"detail": "claim refused"})


def approving(plaintext: str, which: int = -1):
    """A poll reply that approves, sealed to the public key of a filing this
    client actually sent -- so the daemon has to have kept the private half of
    the key it filed with.
    """

    def build(http: FakeHttp) -> Reply:
        body = http.posted[which][1]
        return Reply(200, {"status": "approved", **seal(plaintext, body["public_key"])})

    return build


class FakeHttp:
    """A scripted client. Opens nothing, and refuses to invent an answer.

    Running a queue dry is an `AssertionError` rather than a stub reply: this
    module loops until it is paired, so a test whose script is short by one is
    a test that would otherwise spin for ever inside a `while True`.
    """

    def __init__(self, filings: list[Any] | None = None, polls: list[Any] | None = None) -> None:
        self.filings = list(filings or [])
        self.polls = list(polls or [])
        self.posted: list[tuple[str, Any]] = []
        self.polled: list[str] = []
        self.timeouts: list[Any] = []

    def post(self, url: str, json: Any = None, timeout: Any = None) -> Reply:
        self.posted.append((url, json))
        self.timeouts.append(timeout)
        return self._next(self.filings, "POST", url)

    def get(self, url: str, timeout: Any = None) -> Reply:
        self.polled.append(url)
        self.timeouts.append(timeout)
        return self._next(self.polls, "GET", url)

    def _next(self, queue: list[Any], verb: str, url: str) -> Reply:
        assert queue, f"unscripted {verb} {url}"
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(self)
        return reply


@dataclass
class Naps:
    """Every sleep this module asked for, in order."""

    seconds: list[float] = field(default_factory=list)

    def __call__(self, delay: float) -> None:
        self.seconds.append(delay)


def join(
    tmp_path: Path,
    http: FakeHttp,
    *,
    servers: Any = None,
    naps: Naps | None = None,
    hostname: str | None = HOSTNAME,
    version: str = VERSION,
) -> tuple[bool, Naps, Path]:
    link = tmp_path / "link.json"
    naps = naps or Naps()
    paired = join_a_server(
        identity=IDENTITY,
        servers=servers if servers is not None else (lambda: [SERVER]),
        link_path=link,
        sleeper=naps,
        http=http,
        hostname=hostname,
        version=version,
    )
    return paired, naps, link


# --- filing -----------------------------------------------------------------


def test_a_claim_carries_this_racks_identity_hostname_and_version(tmp_path: Path) -> None:
    """Design spec S6.3 step 1's body. The short code is what the admin
    compares on screen (S6.4), the fingerprint is what the server keys on, and
    neither is derivable from the other end -- so both travel.
    """
    http = FakeHttp([filed("claim-one")], [approving("daemon-key-1")])

    paired, _naps, _link = join(tmp_path, http)

    assert paired is True
    url, body = http.posted[0]
    assert url == "http://192.0.2.10:8080/api/racks/claims"
    assert body["hostname"] == "rack-in-the-cupboard"
    assert body["fingerprint"] == "fpr-6d1e9c40aa27"
    assert body["short_code"] == "Q7K2ZM"
    assert body["version"] == "9.8.7-under-test"
    assert "address" not in body, (
        "the server records the address from the connection; a field the "
        "claimant fills in is a field the claimant chooses (design spec S6.3 step 2)"
    )
    assert "secret" not in json.dumps(body), "the identity secret never leaves the Pi"
    assert len(base64.b64decode(body["public_key"], validate=True)) == 32


def test_the_hostname_and_version_default_to_this_machines(tmp_path: Path) -> None:
    """Nothing in the caller has to look either up; the defaults are what the
    link's own `hello` already claims (`link.py`'s `_hello`).

    **`join_a_server` is called directly, with neither keyword.** This test
    used to go through the `join` helper above and pass `version=__version__`
    explicitly, which is the one thing it could not do and still be about the
    default: `version: str = __version__` mutated to `"0.0.0"` survived the
    entire daemon suite. The sole production caller
    (`__main__.join_a_server`) passes neither, and that string becomes
    `claim.version`, the "Daemon version" on the admin's card, and
    `daemon.version` after approve -- so on a released rack every one of them
    would have read `0.0.0` with nothing anywhere saying otherwise. It is
    Task 19's self-referential-version finding, one layer up.

    `__version__` on both sides is not circular here: `tests/test_packaging.py`
    pins it against `daemon/pyproject.toml`, so what this asserts is that the
    filing carries *the module's* version rather than a literal that agrees
    with nothing.
    """
    from ors_daemon import __version__

    http = FakeHttp([filed("c")], [approving("k")])

    join_a_server(
        identity=IDENTITY,
        servers=lambda: [SERVER],
        link_path=tmp_path / "link.json",
        sleeper=Naps(),
        http=http,
    )

    body = http.posted[0][1]
    assert body["hostname"] == os.uname().nodename
    assert body["version"] == __version__
    assert __version__ != "0.0.0", "a version nothing published is not a default worth pinning"


def test_every_claim_carries_a_fresh_ephemeral_public_key(tmp_path: Path) -> None:
    """The one thing X25519-per-claim exists for (design spec S6.3 step 1).

    A key pair generated once and reused across claims makes every handover
    this rack ever receives openable by whoever recovers that one secret --
    including the earlier ones, recorded off the LAN months before. Two
    filings, two keys.
    """
    http = FakeHttp(
        [filed("claim-one"), filed("claim-two")],
        [Reply(404, {"detail": "no such claim"}), approving("daemon-key-2")],
    )

    paired, _naps, _link = join(tmp_path, http)

    assert paired is True
    first = http.posted[0][1]["public_key"]
    second = http.posted[1][1]["public_key"]
    assert first != second, "a reused ephemeral key is the whole thing this protects against"


def test_a_202_that_names_no_claim_id_is_not_polled_for(tmp_path: Path) -> None:
    """The claim id is the bearer credential and the only thing the 202 body
    carries; polling `.../None` would be a 404 loop against a server that is
    answering perfectly well. Refile instead."""
    http = FakeHttp(
        [Reply(202, {}), filed("claim-two")],
        [approving("daemon-key-3")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert http.polled == ["http://192.0.2.10:8080/api/racks/claims/claim-two"]
    assert naps.seconds[0] > 0, "and it backs off rather than hammering"


# --- approval ---------------------------------------------------------------


def test_an_approved_claim_is_decrypted_and_written_to_the_link_file(tmp_path: Path) -> None:
    """The end of design spec S6.3: the key arrives sealed, and what lands on
    disk is the plaintext the link will present in `hello`."""
    http = FakeHttp([filed("claim-one")], [pending(), approving("the-minted-daemon-key")])

    paired, _naps, link = join(tmp_path, http)

    assert paired is True
    settings = load_link_settings(link)
    assert settings is not None
    assert settings.key == "the-minted-daemon-key"
    assert settings.token is None, "a claim mints a key, not a pairing token"
    assert settings.server_url == "http://192.0.2.10:8080"
    assert settings.cache_path == tmp_path / "snapshot.json"
    assert settings.credential == "the-minted-daemon-key"


def test_the_pairing_is_written_no_wider_than_its_owner(tmp_path: Path) -> None:
    """It holds the credential to this rack's panels; `write_link_settings`
    is the one writer and this is what it promises."""
    http = FakeHttp([filed("c")], [approving("k")])

    _paired, _naps, link = join(tmp_path, http)

    assert link.stat().st_mode & 0o777 == 0o600


def test_a_stale_snapshot_cache_is_cleared_by_joining(tmp_path: Path) -> None:
    """The same decision `connect` makes, for the same reason: the cache holds
    a configuration pushed by *some* server, and this is the moment the rack
    decides which server it answers to. `_boot` hands the cache's version to
    `hello`, so a cache left from another server would have the new one skip
    the push and leave the rack drawing the old server's rack for ever.
    """
    cache = tmp_path / "snapshot.json"
    cache.write_text('{"version": 12, "snapshot": {}}')
    http = FakeHttp([filed("c")], [approving("k")])

    paired, _naps, _link = join(tmp_path, http)

    assert paired is True
    assert not cache.exists()


def test_a_key_that_does_not_decrypt_is_refused_rather_than_written(tmp_path: Path) -> None:
    """A corrupt handover saved anyway is a rack that dials for ever with a
    credential nothing accepts -- and says nothing, because the pairing file
    looks perfectly well formed.
    """
    # Sealed to a key pair this rack does not hold -- which is what a
    # ciphertext altered in flight, a claim id somebody else's rack filed, or
    # two ends a protocol version apart all look like from here.
    stranger = X25519PrivateKey.generate()
    broken = seal(
        "never-readable",
        base64.b64encode(
            stranger.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode(),
    )
    http = FakeHttp([filed("claim-one")], [Reply(200, {"status": "approved", **broken})])

    paired, _naps, link = join(tmp_path, http)

    assert paired is False
    assert not link.exists(), "nothing is paired by a key that cannot be opened"


def test_an_approval_missing_its_ciphertext_is_refused_rather_than_written(
    tmp_path: Path,
) -> None:
    """`ClaimPollResult`'s three sealed fields are `None` until the claim is
    granted. A body claiming `approved` without them is a server this build
    does not understand, not a pairing."""
    http = FakeHttp([filed("claim-one")], [Reply(200, {"status": "approved"})])

    paired, _naps, link = join(tmp_path, http)

    assert paired is False
    assert not link.exists()


def test_a_pairing_that_cannot_be_written_is_reported_rather_than_raised(
    tmp_path: Path,
) -> None:
    """`write_link_settings` raises, deliberately, and leaves it to its caller.
    Here the caller is a first boot over SSH, so it is a `False` and a log
    line -- a traceback out of the join flow tells the reader about this
    program rather than about their machine."""
    link = tmp_path / "nowhere" / "link.json"
    link.parent.mkdir()
    link.parent.chmod(0o500)
    http = FakeHttp([filed("c")], [approving("k")])
    try:
        paired = join_a_server(
            identity=IDENTITY,
            servers=lambda: [SERVER],
            link_path=link,
            sleeper=Naps(),
            http=http,
        )
    finally:
        link.parent.chmod(0o700)

    assert paired is False


# --- refusal, expiry and the deny that cannot be seen -----------------------


def test_a_refused_claim_backs_off_further_each_time(tmp_path: Path) -> None:
    """429 is the queue being full *and* this fingerprint being suppressed
    after a deny, and the two are byte-identical on purpose. So there is
    nothing to branch on: wait longer each time.

    Hammering is the failure this prevents -- the filing limiter is ten a
    minute per address, so a client that retried immediately would spend its
    whole budget inside a second and then be refused for reasons it caused.
    """
    http = FakeHttp(
        [refused(), refused(), refused(), filed("claim-late")],
        [approving("daemon-key-late")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    backoffs = naps.seconds[:3]
    assert backoffs == sorted(backoffs), "each refusal waits at least as long as the last"
    assert backoffs[0] < backoffs[1] < backoffs[2], "and strictly longer, not a constant"
    assert backoffs[0] >= 1.0, "an immediate retry is what the limiter is there to refuse"


def test_the_backoff_is_capped_so_a_rack_left_refused_still_reappears(tmp_path: Path) -> None:
    """A deny suppresses this fingerprint for 24 hours and then it is back in
    the admin's queue. A backoff that doubled without a ceiling would be days
    long by then, so the rack the admin decided to let in second would not
    reappear on the day they changed their mind."""
    http = FakeHttp([refused()] * 20 + [filed("c")], [approving("k")])

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds[-1] == naps.seconds[-2], "and it settles at the cap rather than growing"
    assert max(naps.seconds) == BACKOFF_CAP_S, "the ceiling is the constant, not some other number"
    assert BACKOFF_CAP_S <= 300.0, (
        "five minutes is the argument in the constant's own docstring: a rack is never "
        "more than that behind an admin who has just fixed whatever was wrong"
    )


def test_a_claim_that_is_gone_is_refiled_rather_than_polled_for_ever(tmp_path: Path) -> None:
    """Design spec S6.5: a claim lives thirty minutes. A daemon polling an
    expired one files a new one rather than waiting for ever -- and the
    server answers 404 for expired, denied and never-filed alike, so this is
    the only thing there is to do about any of them.
    """
    http = FakeHttp(
        [filed("claim-expired"), filed("claim-fresh")],
        [pending(), Reply(404, {"detail": "no such claim"}), approving("daemon-key-fresh")],
    )

    paired, _naps, link = join(tmp_path, http)

    assert paired is True
    assert [body["fingerprint"] for _url, body in http.posted] == [
        "fpr-6d1e9c40aa27",
        "fpr-6d1e9c40aa27",
    ], "two claims were filed, and the short code an admin reads does not move"
    assert http.polled[-1].endswith("/claim-fresh")
    settings = load_link_settings(link)
    assert settings is not None and settings.key == "daemon-key-fresh"


def test_a_claim_that_is_gone_is_refiled_after_a_wait_rather_than_at_full_speed(
    tmp_path: Path,
) -> None:
    """The refile above, at the speed the filing endpoint allows.

    A 404 is the one answer that sends this client straight back to `_file`,
    and `_file` resets the backoff on every claim the server accepts -- so a
    peer that answers 202 to every filing and 404 to every poll (a deny that
    lands between the two, an expiry race, or simply a peer that behaves that
    way) is a lap with nothing in it that waits. Measured before this wait
    existed: forty-one filings and forty polls with an empty sleep list, at
    whatever rate the socket managed. The module docstring's whole argument is
    that this client does not hammer.
    """
    http = FakeHttp(
        [filed("claim-expired"), filed("claim-fresh")],
        [Reply(404, {"detail": "no such claim"}), approving("daemon-key-fresh")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds == [BACKOFF_FIRST_S], (
        "the second filing waited, and waited the interval the filing limiter is sized by"
    )


def test_a_filing_the_server_accepts_starts_the_backoff_over(tmp_path: Path) -> None:
    """`_Backoff.reset()`, which is otherwise invisible: make it a no-op, or
    delete either call site, and every other test here still passes.

    Both call sites are answers from the server that mean the waiting is over
    -- a claim it accepted, and a poll it answered -- and what they buy is that
    a rack which spent five minutes getting in does not spend five minutes
    again over the first 429 after it. The naps below are the whole sequence,
    because it is the only place the value is observable:

    | # | what happened            | wait | backoff after |
    | 1 | filing refused           |    5 |            10 |
    | 2 | filing refused           |   10 |            20 |
    | 3 | filing accepted          |    - |    5 (`_file`) |
    | 4 | poll refused             |    5 |            10 |
    | 5 | poll answered, pending   |    - | 5 (`_wait_for_approval`) |
    | 6 | the interval between polls |  5 |             5 |
    | 7 | poll 404, so refile      |    5 |            10 |
    """
    http = FakeHttp(
        [refused(), refused(), filed("claim-one"), filed("claim-two")],
        [refused(), pending(), Reply(404, {"detail": "no such claim"}), approving("k")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds == [
        BACKOFF_FIRST_S,
        BACKOFF_FIRST_S * BACKOFF_FACTOR,
        BACKOFF_FIRST_S,
        POLL_INTERVAL_S,
        BACKOFF_FIRST_S,
    ], "without both resets these keep doubling from where the last refusal left them"


def test_a_denied_rack_cannot_tell_it_was_denied_and_keeps_trying_behind_a_backoff(
    tmp_path: Path,
) -> None:
    """The whole shape of a deny, from this end. `claims.deny` deletes the
    row, so the poll answers 404 -- byte-identical to a claim id nobody ever
    filed -- and the refile then meets a 429 that is byte-identical to a full
    queue. There is no `denied` status and this client must not pretend there
    is: what it does is back off and try again, for the 24 hours the
    suppression lasts, after which the same fingerprint reappears in the
    admin's queue and can be approved.
    """
    http = FakeHttp(
        [filed("claim-denied"), refused(), refused(), filed("claim-second-chance")],
        [Reply(404, {"detail": "no such claim"}), approving("daemon-key-after-the-wait")],
    )

    paired, naps, link = join(tmp_path, http)

    assert paired is True
    assert len(http.posted) == 4, "it kept filing; nothing here can tell a deny from a queue"
    refusal_waits = [seconds for seconds in naps.seconds if seconds >= 1.0]
    assert refusal_waits[0] < refusal_waits[1], "and waited longer after each refusal"
    settings = load_link_settings(link)
    assert settings is not None and settings.key == "daemon-key-after-the-wait"


def test_a_pending_claim_is_polled_slower_than_the_servers_own_budget(tmp_path: Path) -> None:
    """`GET /api/racks/claims/{id}` allows sixty polls a minute per address --
    one a second as a *ceiling*, not as headroom above one. A client polling
    faster meets a 429 it caused itself, on the one endpoint it needs in order
    to finish pairing.
    """
    http = FakeHttp([filed("c")], [pending(), pending(), approving("k")])

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert len(naps.seconds) == 2, "one wait between each pair of polls"
    assert min(naps.seconds) >= 2.0, "well inside sixty a minute, with room for a retry"


def test_a_status_this_client_does_not_know_is_polled_again_not_taken_as_an_approval(
    tmp_path: Path,
) -> None:
    """`approved` and only `approved`, not "anything that is not `pending`".

    A server one version ahead answering some third status has not granted
    anything, and `ClaimPollResult`'s three sealed fields are `None` until it
    has. A client that read the absence of `pending` as a grant would fail to
    decrypt what is not there, return `False`, and leave a rack that can never
    pair with a server that would have approved it a second later.
    """
    http = FakeHttp([filed("c")], [Reply(200, {"status": "queued"}), approving("k")])

    paired, naps, link = join(tmp_path, http)

    assert paired is True
    settings = load_link_settings(link)
    assert settings is not None and settings.key == "k"
    assert naps.seconds == [POLL_INTERVAL_S], "it waited and polled again, like any other pending"


def test_a_poll_refused_for_rate_backs_off_rather_than_refiling(tmp_path: Path) -> None:
    """A 429 on the poll is this client's own budget, not a decision about the
    claim -- the claim is still pending and still the one an admin is looking
    at. Filing a new one would spend the *other* budget as well and change the
    row under the admin's cursor."""
    http = FakeHttp([filed("claim-one")], [refused(), approving("k")])

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert len(http.posted) == 1, "the claim an admin is looking at is still the claim"
    assert naps.seconds[0] >= 1.0


# --- discovery, and the network that is not there ---------------------------


def test_two_servers_are_both_named_and_neither_is_filed_with(tmp_path: Path) -> None:
    """Design spec S6.1 and S8's failure table: more than one server is
    reported as a list and paired with none of them. A rack that picked one
    would pick a different one on the next boot and nothing anywhere would
    record the choice; `--server` settles it.
    """
    # Scripted to *succeed*, so that "neither of them" is what this test
    # measures rather than what the fake refuses to answer: a client that
    # quietly took the first of the two would pair here, and the assertions
    # below are what say it must not.
    http = FakeHttp([filed("would-have-been-a-mistake")], [approving("the-wrong-servers-key")])

    paired, _naps, link = join(tmp_path, http, servers=lambda: [SERVER, OTHER_SERVER])

    assert paired is False
    assert http.posted == [], "neither of them, not the first one"
    assert not link.exists()


def test_no_server_yet_keeps_looking_rather_than_giving_up(tmp_path: Path) -> None:
    """S8's failure table: "logs what it is looking for and keeps browsing. It
    does not exit" -- a server that boots after the Pi is ordinary, and so is
    a switch that has not finished learning the multicast group.
    """
    heard: list[list[Found]] = [[], [], [SERVER]]
    http = FakeHttp([filed("c")], [approving("k")])

    paired, naps, _link = join(tmp_path, http, servers=lambda: heard.pop(0))

    assert paired is True
    assert len(naps.seconds) >= 2, "it waited between browses rather than spinning"
    assert http.posted[0][0] == FILING_URL


def test_a_network_that_is_down_is_a_backoff_not_a_traceback(tmp_path: Path) -> None:
    """The rack most likely to be running this is a freshly installed one
    whose wifi has not associated yet. `requests`' own exceptions are all
    `OSError`, and none of them is a reason to stop trying."""
    http = FakeHttp(
        [OSError("connection refused"), filed("c")],
        [OSError("connection reset"), approving("k")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds[0] >= 1.0


def test_a_server_answering_something_that_is_not_json_is_a_backoff(tmp_path: Path) -> None:
    """Anything on the LAN may be listening on that port -- and design spec
    S6.1's discovery will happily hand this a captive portal."""
    http = FakeHttp(
        [Reply(202, ValueError("not json")), filed("c")],
        [Reply(200, ValueError("not json")), approving("k")],
    )

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds[0] >= 1.0


def test_a_server_that_answers_500_is_a_backoff_not_a_pairing(tmp_path: Path) -> None:
    http = FakeHttp([Reply(500, None), filed("c")], [Reply(503, None), approving("k")])

    paired, naps, _link = join(tmp_path, http)

    assert paired is True
    assert naps.seconds[0] >= 1.0


def test_every_request_carries_a_timeout(tmp_path: Path) -> None:
    """A socket with no timeout on a rack that has just come up is a join flow
    parked for ever in `recv`, with the panels dark and nothing in the log."""
    http = FakeHttp([filed("c")], [approving("k")])

    join(tmp_path, http)

    assert http.timeouts, "there were requests to check"
    assert all(timeout == HTTP_TIMEOUT_S for timeout in http.timeouts)


def test_the_request_timeout_is_short_against_the_interval_between_polls() -> None:
    """What `HTTP_TIMEOUT_S`'s docstring claims, asserted rather than asserted
    of it that it is positive.

    The flow is one blocking request at a time, so the timeout is not merely a
    bound on a slow request: it is how long the admin's click waits behind a
    socket that has stopped answering. A timeout an order of magnitude above
    the poll interval turns each stall into minutes of a rack that looks
    exactly like one nobody has approved yet.
    """
    assert 0 < HTTP_TIMEOUT_S <= 2 * POLL_INTERVAL_S


# --- `--server URL`, the thing that settles two servers ---------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://rack:8080", Found(host="rack", port=8080, scheme="http")),
        ("https://rack.example", Found(host="rack.example", port=443, scheme="https")),
        ("http://rack.example", Found(host="rack.example", port=80, scheme="http")),
        ("http://192.0.2.9:8080/", Found(host="192.0.2.9", port=8080, scheme="http")),
        ("http://[2001:db8::5]:8080", Found(host="2001:db8::5", port=8080, scheme="http")),
    ],
)
def test_a_server_url_is_read_into_something_a_claim_can_be_filed_against(
    url: str, expected: Found
) -> None:
    assert server_from_url(url) == expected


@pytest.mark.parametrize("url", ["rack:8080", "http://", "", "://rack", "ftp://rack"])
def test_a_server_url_that_names_nothing_dialable_is_refused(url: str) -> None:
    """`--server rack:8080` names no host at all -- the typo `connect` already
    had to learn to reject -- and a scheme this client cannot speak is not a
    server either. Refused here, so the caller can say so, rather than filing
    a claim against nothing once a backoff for ever."""
    assert server_from_url(url) is None


# --- the wire format --------------------------------------------------------


def test_the_daemon_opens_a_blob_sealed_by_the_format_the_server_writes() -> None:
    """A round trip against this file's own `seal`, which shares no code with
    `open_claim_key`."""
    private_key = X25519PrivateKey.generate()
    public = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()

    assert open_claim_key(seal("a-minted-key", public), private_key) == "a-minted-key"


def test_the_daemon_opens_a_frozen_wire_vector() -> None:
    """The one test here that no coordinated edit can satisfy.

    The blob below is a constant, copied from `server/tests/test_api_claims.py`
    -- produced before either end's constants could be edited. Rename the HKDF
    `info` tag, change its length or its salt, swap the AEAD or give it
    associated data, and every round trip in this repository still passes
    while every rack already installed stops being able to open a key its
    server sealed. This is what fails instead.

    It needs no server, no HTTP client and no claim: it is the format alone.
    """
    private_key = X25519PrivateKey.from_private_bytes(base64.b64decode(VECTOR_PEER_PRIVATE_KEY))

    assert open_claim_key(VECTOR_SEALED, private_key) == VECTOR_PLAINTEXT


@pytest.mark.parametrize(
    "damage",
    [
        {"ciphertext": "AAAA"},
        {"nonce": "AAECAwQFBgcICQoM"},
        {"ephemeral_public_key": base64.b64encode(bytes(32)).decode()},
        {"ciphertext": "not base64 at all!!"},
        {"ephemeral_public_key": "AAAA"},
    ],
    ids=["ciphertext", "nonce", "peer-key", "not-base64", "short-key"],
)
def test_a_damaged_blob_raises_rather_than_returning_something(damage: dict[str, str]) -> None:
    """Every way this can fail has to fail the same way -- as an exception the
    one caller catches -- because the alternative is a plausible-looking string
    written to the pairing file."""
    private_key = X25519PrivateKey.from_private_bytes(base64.b64decode(VECTOR_PEER_PRIVATE_KEY))

    with pytest.raises(Exception):  # noqa: B017 - the point is that *nothing* returns
        open_claim_key({**VECTOR_SEALED, **damage}, private_key)
