"""The claim protocol's shared pieces: the HKDF tag, and the short code.

Two things, and they are here for one reason -- a daemon and a server that
disagree about either of them do not fail, they simply never pair, or worse,
pair with the wrong rack. The
claim protocol (design spec S6.3 step 5) hands a newly minted daemon key over
encrypted to an ephemeral X25519 public key: X25519, then HKDF-SHA256 with the
tag below, then AES-256-GCM. A peer that derives with a different tag derives
a different key and cannot open anything the other sealed -- whatever else the
two ends agree on, and with no error either end can attribute. It is protocol,
exactly like `PROTOCOL_VERSION` and `ors_daemon.discovery.SERVICE_TYPE`, and
this package is where this repository keeps protocol.

**It lives here now, and it deliberately did not before.** Until the daemon
had a claim client there was one production speller of these twelve bytes
(`ors_server.api.claims`) and one independent literal in the server's own test
(`test_api_claims.unseal`), and that independence was the only thing standing
between a one-sided edit and a silent protocol rename. Sharing the constant
then would have deleted the check and put nothing in its place. What replaced
it is the frozen wire vector, and the pin is the test at each end that feeds
production: `test_api_claims.test_the_frozen_vector_is_the_format_the_live_seal_still_produces`
(which seals with the live `_seal` and opens with the test's own literal) and
`daemon/tests/test_join.test_the_daemon_opens_a_frozen_wire_vector` (whose
reader *is* production). Each carries a ciphertext produced before this tag
could be edited, so no edit here -- one-sided or coordinated, in this file or
in either end's -- can leave the suite green.
`test_api_claims.test_the_sealed_blob_matches_a_frozen_wire_vector` is not one
of the two, verified by mutation: it opens the frozen blob with the test's own
`unseal`, and no production code runs in it at all.

With the format pinned from outside, one definition for the two
production ends is the safer arrangement: a daemon and a server that disagree
about this string do not fail, they simply never pair.

The server's test keeps its own literal copy. That is not an oversight either:
it is the peer implementation a daemon's author would copy, and it means the
round-trip tests still fail if this module and `api.claims` drift apart.

`derive_short_code` joined it in M3c's final review, and for a sharper reason
than drift: until then only one end computed the code at all. See that
function.
"""

from __future__ import annotations

import base64

CLAIM_HKDF_INFO = b"ors-claim-v1"
"""The HKDF `info` tag for the claim key handover.

Versioned in its own text rather than by `PROTOCOL_VERSION`, because it is not
carried in any message: there is nowhere on this wire to negotiate it, so a
change to it is a new tag (`ors-claim-v2`) and an explicit decision about every
rack already in the field, not a number a server can compare and complain
about.
"""


FINGERPRINT_HEX_CHARS = 64
"""A fingerprint's length, and therefore its shape: 64 lowercase hex
characters, which is what `hashlib.sha256(...).hexdigest()` produces and the
only thing `ors_daemon.identity` ever sends.

Pinned as a *shape*, not merely as a length, because the fingerprint is a
database key at the server end. `claim_pending_fingerprint` is unique on it,
`claims.deny` suppresses re-filings by it, and SQLite compares TEXT
byte-for-byte -- so `"AB..."` and `"ab..."` are two different racks to every
one of those, and a claimant that uppercased its own hex would file a second
pending row and walk straight out from under a deny it had just been given.
One spelling per digest is what closes that, and it is free: no honest daemon
has ever sent any other.
"""

SHORT_CODE_CHARS = 6
"""How much of the fingerprint a person is asked to compare.

Six base32 characters is 30 bits: about a billion, which is far more than the
number of racks anyone will ever approve, and few enough to read off a
terminal without losing your place. It is a check against *confusion*, not
against a determined collision -- the fingerprint is what the server keys on,
and this is what the admin looks at (design spec S6.4).
"""


def derive_short_code(fingerprint: str) -> str:
    """The six characters a human compares, computed from the fingerprint.

    **Protocol, and here for the same reason `CLAIM_HKDF_INFO` is**: two ends
    have to agree, and until this milestone only one of them computed it at
    all. `ors_daemon.identity` derived the code and printed it;
    `ors_server.api.claims` took whatever the claimant put in the field and
    stored it verbatim. That is not a drift risk, it is a hole -- design spec
    S6.4 argues that matching a code already seen on somebody's screen costs
    about 2^30 hashes, and a server that never checks makes it cost nothing:
    anyone who reads a code off a screen or out of `journalctl` files with
    that code, their own fingerprint and their own key, and is indistinguishable
    from the real rack in the admin's queue. `ClaimRequest` now recomputes this
    and refuses a pair that does not agree, which is only meaningful if the two
    ends compute it identically -- so they compute it here, once.

    Base32 and not hex: hex is 4 bits a character, so six characters would be
    24 bits, and it contains no letters past F -- which makes a short code look
    like a number and reads worse aloud. Uppercase, because that is base32's
    alphabet as `base64.b32encode` emits it and as the Pi prints it.

    Raises `ValueError` for anything that is not a fingerprint. The daemon can
    never reach that -- it passes a `hexdigest()` -- so the only caller that
    can is the server, validating a body a stranger sent, which is exactly
    where an exception is wanted rather than a code derived from garbage.
    """
    if len(fingerprint) != FINGERPRINT_HEX_CHARS:
        raise ValueError(
            f"fingerprint must be {FINGERPRINT_HEX_CHARS} hex characters, not {len(fingerprint)}"
        )
    if fingerprint != fingerprint.lower():
        # Not a cosmetic rule; see `FINGERPRINT_HEX_CHARS`. `bytes.fromhex`
        # accepts either case, so nothing below would catch this.
        raise ValueError("fingerprint must be lowercase hex")
    try:
        digest = bytes.fromhex(fingerprint)
    except ValueError as error:
        raise ValueError("fingerprint must be hex") from error
    return base64.b32encode(digest).decode("ascii")[:SHORT_CODE_CHARS]
