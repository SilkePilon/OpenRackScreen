"""Filing a claim, waiting to be let in, and keeping the key that arrives.

The daemon's half of design spec S6.3, and the last piece of a pairing nobody
types a token for. A freshly installed rack has an identity (`identity.py`),
a list of servers that answered (`discovery.py`) and nothing else: no
credential, which is the entire reason this protocol exists rather than a
shared secret. So it files a claim, an admin compares six characters on a
screen and clicks Approve, and the key comes back sealed to a public key this
rack generated for that one claim.

Four things about the peer are worth having in front of you, because each is a
place a plausible client goes wrong.

*The claim id is the credential.* `POST /api/racks/claims` answers 202 with a
`claim_id` -- `secrets.token_urlsafe(32)`, returned in that one response and
nowhere else -- and the poll authenticates with nothing but that id. There is
no signature header and there is nothing to sign with: the server holds only
`sha256(secret)`, so it could not verify an HMAC keyed on this rack's identity
even if one were sent.

*There is no `denied` status, and this client must not pretend there is.*
`claims.deny` **deletes** the row, so a poll after a deny answers 404 --
byte-identical to a claim id nobody ever filed -- deliberately, so that a
prober holding a fingerprint cannot confirm an admin denied it. What a denied
rack actually does is what an expired one does: file again. Its next filing
meets 429, which is *also* what a full queue answers, with the same
`{"detail": "claim refused"}` body. So there is nothing to branch on anywhere
in here, and a denied rack cannot tell and retries indefinitely. That is by
design; `DENY_SUPPRESSION_S` (24 hours, server-side) is what keeps it from
reappearing every five seconds and training people to click Approve, and the
backoff below is this end of the same bargain.

*The poll is idempotent.* `claim.granted_key` keeps the sealed blob, so an
approved claim answers with the same ciphertext every time. Nothing here needs
to treat a read as one-shot, and nothing here should retry a decrypt failure
in the hope of a different blob: it will be the same bytes.

*The poll has its own rate budget*, sixty in sixty seconds per address, which
is one a second as a ceiling rather than as headroom. `POLL_INTERVAL_S` sits
well inside it, because a client that polls faster meets a 429 it caused
itself on the one endpoint it needs in order to finish pairing.

**Nothing in here raises at its caller**, with the single exception of what
the operator's own Ctrl-C does to `sleeper`. The caller is `__main__._run` on
row 4 of the boot table -- a Pi that has just been installed, whose wifi may
not have associated and whose LAN may hold anything at all on port 8080 -- and
every one of those is a wait-and-try-again, not a traceback into a journal
that repeats every `RestartSec=5`. The two answers are `True` (paired; the
pairing is on disk) and `False` (paired nothing, and said why in the log,
because the thing that stopped it is something a person has to change).
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from ors_schema import CLAIM_HKDF_INFO

from ors_daemon import __version__
from ors_daemon.discovery import Found
from ors_daemon.identity import Identity
from ors_daemon.link import LinkSettings, write_link_settings

log = logging.getLogger(__name__)

CLAIMS_PATH = "/api/racks/claims"
"""Where a rack files and polls. Written out rather than imported from the
server package, the same way `discovery.SERVICE_TYPE` is: this is a wire
protocol between two separately installed programs, and the daemon does not
depend on the server."""

HTTP_TIMEOUT_S = 10.0
"""How long any one request may take.

A socket with no timeout, on a rack that has just come up, is a join flow
parked in `recv` for ever with the panels dark and nothing in the log. Ten
seconds is generous for a LAN, and it is held within a couple of
`POLL_INTERVAL_S` -- the whole flow is one blocking request at a time, so a
timeout much larger than the interval between polls is a stall the admin's
click waits behind rather than a request that is merely slow.
"""

POLL_INTERVAL_S = 5.0
"""How long to wait between polls of a claim that is still pending.

Sized against the server's own budget for that endpoint, which is sixty polls
in a rolling sixty seconds per address -- one a second as a *ceiling*, so a
client at 1 Hz is refused on its next poll. Five seconds spends a twelfth of
it and leaves room for the retries below to share the same budget. The cost is
that an admin's click takes up to five seconds to reach the rack, which is
nothing next to the time it takes to walk to the rack.
"""

BACKOFF_FIRST_S = 5.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_S = 300.0
"""How long to wait after a refusal or a failure, and how fast that grows.

Five seconds first, because the filing endpoint allows ten attempts a minute
per address and a client that retried immediately would spend that budget
inside a second -- and then be refused for a reason it caused itself, which is
indistinguishable from the queue being full.

Capped at five minutes, and the cap is the part worth arguing. A deny
suppresses this fingerprint for twenty-four hours and then it is back in the
admin's queue; an uncapped doubling would be measured in days by then, so the
rack somebody decided to let in after all would not reappear on the day they
changed their mind. Five minutes means a rack is never more than that behind
an admin who has just fixed whatever was wrong.
"""


def open_claim_key(sealed: Mapping[str, Any], private_key: X25519PrivateKey) -> str:
    """Recover the minted daemon key from what `GET /api/racks/claims/{id}`
    returned, using the private half of the key this claim was filed with.

    The peer of `ors_server.api.claims._seal`, and the whole of what this
    daemon knows about that format: X25519 to a shared secret, HKDF-SHA256
    over it with `CLAIM_HKDF_INFO` and no salt to a 32-byte key, AES-256-GCM
    with the transmitted nonce and no associated data.

    **Raises rather than returning a sentinel**, for every way each of those
    steps can fail -- a blob that is not base64, a peer key of the wrong
    length, a nonce that has been altered, a ciphertext that has. There is one
    caller, it catches all of them together, and what it does about them is
    the same thing: refuse to pair. A sentinel would put a plausible-looking
    string one missing `if` away from `write_link_settings`.
    """
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(sealed["ephemeral_public_key"]))
    shared = private_key.exchange(peer)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=CLAIM_HKDF_INFO).derive(shared)
    nonce = base64.b64decode(sealed["nonce"])
    ciphertext = base64.b64decode(sealed["ciphertext"])
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()


def server_from_url(url: str) -> Found | None:
    """`--server URL` as the same record discovery answers with, or None.

    None rather than a raise, and the one word this function exists for.
    `--server rack:8080` names no host -- `urlsplit` reads `rack` as the
    scheme -- which is the typo `connect` already had to learn to reject, and
    it is worth rejecting here for the same reason: without it this rack files
    claims against nothing, once a backoff, for ever, and the only sign is a
    log line nobody is reading on a machine with dark panels.

    The port falls back to the scheme's own, because that is what a URL
    without one means and `http://rack.example` is a perfectly ordinary way to
    name a server behind a reverse proxy.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        log.error("this is not a URL a claim could be filed against", extra={"server": url})
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        log.error(
            "a server URL needs a scheme and a host, e.g. http://rack.local:8080",
            extra={"server": url},
        )
        return None
    try:
        port = parts.port
    except ValueError:
        # `urlsplit` defers parsing the port until it is asked for, so a URL
        # like `http://rack:not-a-port` reaches here intact and raises on the
        # attribute access rather than on the split above.
        log.error("this server URL names no usable port", extra={"server": url})
        return None
    return Found(
        host=parts.hostname,
        port=port or (443 if parts.scheme == "https" else 80),
        scheme=parts.scheme,
    )


class _Backoff:
    """How long to wait after a refusal, growing until it reaches the cap.

    A tiny object rather than a local, because the wait has to survive a
    filing, a poll and a refile -- a client that started it over on every lap
    would hammer exactly as hard as one with no backoff at all, which is what
    the server's limiter is there to refuse.

    `reset` is called **on success**, and only there: an accepted filing and a
    poll the server answered are both evidence that whatever the waiting was
    for is over, so the next refusal starts at `BACKOFF_FIRST_S` again rather
    than at whatever a previous bad patch had grown it to. A rack that waited
    five minutes to be let in, was let in, and then met one 429 should not
    wait five minutes again.
    """

    def __init__(self) -> None:
        self.seconds = BACKOFF_FIRST_S

    def nap(self, sleeper: Callable[[float], None]) -> None:
        sleeper(self.seconds)
        self.seconds = min(self.seconds * BACKOFF_FACTOR, BACKOFF_CAP_S)

    def reset(self) -> None:
        self.seconds = BACKOFF_FIRST_S


def join_a_server(
    *,
    identity: Identity,
    servers: Callable[[], Iterable[Found]],
    link_path: Path,
    sleeper: Callable[[float], None],
    http: Any,
    cache_path: Path | None = None,
    hostname: str | None = None,
    version: str = __version__,
) -> bool:
    """Pair this rack, blocking until an admin approves it. True if it paired.

    `servers` is a callable and not a list, which is the one place this
    departs from the shape the task brief describes. Design spec S8's failure
    table is explicit about the state a first boot is usually in -- "unpaired,
    no config, no server found: logs what it is looking for and keeps
    browsing. It does not exit" -- because a server that boots after the Pi is
    ordinary, and so is a switch that has not finished learning the multicast
    group. A list fixed at the call could only be browsed once, so the browse
    itself is the seam, called again on every lap.

    `False` is returned for exactly the three things waiting cannot fix:
    more than one server answered (design spec S6.1 -- reported as a list and
    paired with *none* of them, because a rack that picked one would pick a
    different one next boot and nothing would record the choice; `--server`
    settles it), a key that will not decrypt, and a pairing that cannot be
    written. Everything else -- no server yet, a refusal, an expired claim, a
    network that is down, a captive portal answering HTML -- is a wait and
    another lap.

    The ephemeral key pair is generated **inside the loop**, once per claim.
    That is what design spec S6.3 step 1 asks for and it is not incidental:
    one long-lived key pair would make every handover this rack ever received
    openable by whoever recovered that secret, including the ones recorded off
    the LAN months earlier.
    """
    cache = _cache_beside(link_path) if cache_path is None else cache_path
    if cache is None:
        log.error(
            "the pairing path names no file, so there is nowhere to keep a snapshot beside it",
            extra={"path": str(link_path)},
        )
        return False

    body = {
        "hostname": hostname if hostname is not None else os.uname().nodename,
        "fingerprint": identity.fingerprint,
        "short_code": identity.short_code,
        "version": version,
    }
    log.info(
        "this rack is not paired; asking to join",
        extra={"short_code": identity.short_code, "fingerprint": identity.fingerprint},
    )

    backoff = _Backoff()
    while True:
        found = list(servers())
        if len(found) > 1:
            log.error(
                "more than one server answered, so this rack will pair with none of them; "
                "name the one you meant with --server URL",
                extra={"servers": [server.url for server in found]},
            )
            return False
        if not found:
            log.info(
                "no server has answered yet; still looking",
                extra={"service": "_openrackscreen._tcp.local.", "retry_in": backoff.seconds},
            )
            backoff.nap(sleeper)
            continue

        server = found[0]
        private_key = X25519PrivateKey.generate()
        public_key = base64.b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode()

        claim_id = _file(http, server, {**body, "public_key": public_key}, backoff, sleeper)
        if claim_id is None:
            continue

        sealed = _wait_for_approval(http, server, claim_id, backoff, sleeper)
        if sealed is None:
            # A 404 is the *only* thing that gets here, and the next statement
            # after `continue` is another filing -- so without this wait a
            # server answering 202 and then 404 (a deny that lands between the
            # two, an expiry race, a peer that simply behaves that way) is
            # filed against as fast as the socket will carry it, and
            # `_file`'s `backoff.reset()` on each accepted claim means the
            # backoff never grows out of it either. The filing endpoint allows
            # ten a minute, which is what this is sized against, and it is the
            # same wait a refusal takes for the same reason.
            backoff.nap(sleeper)
            continue

        try:
            key = open_claim_key(sealed, private_key)
        except (InvalidTag, KeyError, TypeError, ValueError) as error:
            # Not retried *in this process*, deliberately. The poll is
            # idempotent -- the same ciphertext comes back every time -- so
            # another lap here gets the same bytes, and a rack that wrote this
            # anyway would dial for ever with a credential nothing accepts and
            # no sign of why. Under the shipped unit's `Restart=always` the
            # whole flow does come back every `RestartSec=5` with a fresh
            # keypair, which is benign rather than the hammering it looks
            # like: `file_claim` reuses the single pending row per
            # fingerprint, so the queue does not fill, and a re-approve after
            # a one-off corruption is what recovers the rack.
            log.error(
                "the key this server sent cannot be opened, so this rack is not paired; "
                "the two ends disagree about the claim format, most likely a version apart",
                extra={"server": server.url, "error": f"{type(error).__name__}: {error}"},
            )
            return False

        try:
            # The same thing `connect` does, and for the same reason: the
            # cache holds a configuration pushed by *some* server, and this is
            # the moment this rack decides which server it answers to. `_boot`
            # claims the cache's version in `hello`, so one left from another
            # server would have this one skip the push and leave the rack
            # drawing the previous server's screens for ever.
            cache.unlink(missing_ok=True)
            write_link_settings(
                link_path,
                LinkSettings(server_url=server.url, cache_path=cache, key=key),
            )
        except OSError as error:
            log.error(
                "this rack was approved but its pairing could not be written, so the key "
                "the server minted is lost; fix the path and it will file a new claim",
                extra={"path": str(link_path), "error": f"{type(error).__name__}: {error}"},
            )
            return False
        log.info("this rack has been approved and is paired", extra={"server": server.url})
        return True


def _file(
    http: Any,
    server: Found,
    body: Mapping[str, Any],
    backoff: _Backoff,
    sleeper: Callable[[float], None],
) -> str | None:
    """File one claim. The claim id, or None to start over after a wait.

    Every non-answer is the same answer, which is the point: 429 is a full
    queue *and* a fingerprint suppressed after a deny, indistinguishable on
    purpose (`api/claims.py`'s `file_claim_route`), so there is nothing here
    that could branch on which even if it wanted to.
    """
    response = _call(http.post, f"{server.url}{CLAIMS_PATH}", json=dict(body))
    if response is None:
        backoff.nap(sleeper)
        return None
    if response.status_code == 429:
        log.info(
            "the server refused this claim -- its queue is full, or this rack was denied "
            "and is still suppressed; there is no way to tell which. Waiting.",
            extra={"server": server.url, "retry_in": backoff.seconds},
        )
        backoff.nap(sleeper)
        return None
    if response.status_code != 202:
        log.warning(
            "unexpected answer to a claim; waiting before filing another",
            extra={"server": server.url, "status": response.status_code},
        )
        backoff.nap(sleeper)
        return None

    document = _document(response)
    claim_id = document.get("claim_id") if document is not None else None
    if not isinstance(claim_id, str) or not claim_id:
        # The id is the whole of what a 202 carries and the only thing the
        # poll authenticates with; polling without one is a 404 loop against a
        # server that is answering perfectly well.
        log.warning(
            "a claim was accepted but the answer named no claim id",
            extra={"server": server.url},
        )
        backoff.nap(sleeper)
        return None

    log.info(
        "this rack is waiting to be approved; compare the short code in the interface",
        extra={"server": server.url},
    )
    backoff.reset()
    return claim_id


def _wait_for_approval(
    http: Any,
    server: Found,
    claim_id: str,
    backoff: _Backoff,
    sleeper: Callable[[float], None],
) -> Mapping[str, Any] | None:
    """Poll one claim until it is granted. The sealed blob, or None to refile.

    None is a **404**, and only a 404 -- which the server answers identically
    for a claim that expired, one an admin denied and one nobody ever filed,
    deliberately. All three are the same thing from here: this claim is gone,
    so file another. Design spec S8: "claim expires unapproved -- daemon files
    a new one. The short code does not change, so the entry looks the same to
    a human."

    A 429 here is this client's own poll budget rather than a decision about
    the claim, so it waits and polls the *same* claim again: refiling would
    spend the other budget as well and move the row under the cursor of the
    admin who is about to click it.
    """
    while True:
        response = _call(http.get, f"{server.url}{CLAIMS_PATH}/{claim_id}")
        if response is None:
            backoff.nap(sleeper)
            continue
        if response.status_code == 404:
            log.info(
                "this claim is gone -- it expired, or an admin denied it; there is no way "
                "to tell which. Filing a new one.",
                extra={"server": server.url},
            )
            return None
        if response.status_code == 429:
            log.info(
                "polling too fast for this server; waiting longer",
                extra={"server": server.url, "retry_in": backoff.seconds},
            )
            backoff.nap(sleeper)
            continue
        if response.status_code != 200:
            log.warning(
                "unexpected answer while waiting to be approved",
                extra={"server": server.url, "status": response.status_code},
            )
            backoff.nap(sleeper)
            continue

        document = _document(response)
        if document is None:
            backoff.nap(sleeper)
            continue
        backoff.reset()
        if document.get("status") == "approved":
            return document
        sleeper(POLL_INTERVAL_S)


def _call(method: Callable[..., Any], url: str, **kwargs: Any) -> Any | None:
    """One request, or None if the network would not carry it.

    `OSError` and not a bare `except`: every exception `requests` raises for a
    connection, a timeout or a redirect loop descends from
    `requests.RequestException`, which is an `OSError` -- so this catches all
    of them without this module importing `requests` at all, and without
    swallowing a `TypeError` from a client that does not have the shape this
    expects.
    """
    try:
        return method(url, timeout=HTTP_TIMEOUT_S, **kwargs)
    except OSError as error:
        log.info(
            "could not reach the server; waiting",
            extra={"url": url, "error": f"{type(error).__name__}: {error}"},
        )
        return None


def _document(response: Any) -> Mapping[str, Any] | None:
    """The response body as an object, or None for anything else.

    Discovery hands this client whatever answered on that port, so a captive
    portal's HTML and a proxy's error page both arrive here looking like a
    perfectly good HTTP response.
    """
    try:
        document = response.json()
    except ValueError:
        log.warning("the server answered something that is not JSON")
        return None
    if not isinstance(document, dict):
        log.warning("the server answered JSON that is not an object")
        return None
    return document


def _cache_beside(link_path: Path) -> Path | None:
    """Where the pushed snapshot goes, or None if the pairing path names no file.

    `--link /` and `--link .` are paths with no filename, so `with_name` says
    so with `ValueError`. `__main__._cache_beside` makes the same derivation
    for the same reason; this one exists so that a caller passing only the
    five arguments the brief names still cannot reach a traceback.
    """
    try:
        return Path(link_path).with_name("snapshot.json")
    except ValueError:
        return None
