"""The claim protocol's shared pieces, and where they have to be reachable.

Nothing here proves the HKDF tag is right -- a constant compared to itself
agrees with any edit, and this file could not tell a rename from a fix. What
proves it is the pair of frozen wire vectors, one at each end:
`server/tests/test_api_claims.py::test_the_sealed_blob_matches_a_frozen_wire_vector`
and `daemon/tests/test_join.py::test_the_daemon_opens_a_frozen_wire_vector`,
each carrying a ciphertext produced before this tag could be edited.

What is here is the two things those vectors cannot say: that the name is
reachable where both production ends import it from, and that its value is
spelled out once in a file whose diff says "you are changing a wire protocol"
rather than only appearing inside an `HKDF(...)` call.
"""

from __future__ import annotations

import hashlib

import ors_schema
import pytest
from ors_schema.claim import (
    CLAIM_HKDF_INFO,
    FINGERPRINT_HEX_CHARS,
    SHORT_CODE_CHARS,
    derive_short_code,
)


def test_the_claim_tag_is_reachable_from_the_package_root():
    """`ors_server.api.claims` and `ors_daemon.join` both import it from
    `ors_schema`, not from `ors_schema.claim`, and `__all__` is
    hand-maintained -- so a name added to the module and forgotten there is a
    constant neither end can reach."""
    assert "CLAIM_HKDF_INFO" in ors_schema.__all__
    assert ors_schema.CLAIM_HKDF_INFO is CLAIM_HKDF_INFO


def test_the_claim_tag_is_bytes_and_is_the_v1_tag():
    """A tripwire, not a proof (see the module docstring): every rack in the
    field holds the peer half of this string in a release that shipped months
    ago, so it may not move without a `v2` and a decision about all of them.

    `bytes` and not `str` is worth pinning separately: `HKDF(info=...)` takes
    bytes and a `str` there is a `TypeError` raised inside an admin's approve
    click, on the server, with the write lock held.
    """
    assert isinstance(CLAIM_HKDF_INFO, bytes)
    assert CLAIM_HKDF_INFO == b"ors-claim-v1"


def test_the_short_code_derivation_is_reachable_from_the_package_root():
    """Both production ends import it from `ors_schema`, not from
    `ors_schema.claim`, and `__all__` is hand-maintained."""
    assert "derive_short_code" in ors_schema.__all__
    assert "SHORT_CODE_CHARS" in ors_schema.__all__
    assert ors_schema.derive_short_code is derive_short_code


def test_the_short_code_is_six_base32_characters_of_the_digest():
    """A frozen vector, and the reason it is frozen.

    Unlike the HKDF tag above there are two production computations of this
    one -- `ors_daemon.identity._derive` prints it and `ClaimRequest`
    recomputes it to refuse a claimant that chose its own -- and they only
    mean anything if they are the same computation. They are, because both
    call this function; what this pins is that *it* did not move under them.
    A rack in the field prints its code once, at install, and a person
    compares that printed string months later, so this may no more move than
    the tag can.

    The vector is `sha256(b"shed")`, the same pair `web/tests/claims.test.tsx`
    and `server/tests/test_api_claims.py` carry, spelled out rather than
    computed: a test that hashed and base32'd its own input here would be the
    implementation again and would agree with any edit to it.
    """
    digest = "2f3c3e5cf3c63b648b44850ff5e9a88aac1d4498e94e7575f2fe6ad93f35c66b"

    assert derive_short_code(digest) == "F46D4X"
    assert len(derive_short_code(digest)) == SHORT_CODE_CHARS
    # Upper-case base32's alphabet, and nothing else. Lower case, or a stray
    # `=` from padding, is six characters a person cannot read off a screen
    # and type back with any confidence.
    assert set(derive_short_code(digest)) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_anything_that_is_not_a_lowercase_sha256_raises():
    """The server is the only caller that can reach this -- the daemon passes
    a `hexdigest()` -- and it is validating a body a stranger sent, so garbage
    has to raise rather than derive a code from whatever it got.

    The upper-case case is the one worth naming: `bytes.fromhex` accepts it,
    so it derives the *same* code and would pass a cross-check, while being a
    different string to every `WHERE fingerprint = ?` and to the deny
    suppression that keys on it. See `FINGERPRINT_HEX_CHARS`.
    """
    digest = "2f3c3e5cf3c63b648b44850ff5e9a88aac1d4498e94e7575f2fe6ad93f35c66b"

    for bad in ("", "abc", digest.upper(), digest + "00", digest[:-1] + "z"):
        with pytest.raises(ValueError):
            derive_short_code(bad)


def test_the_fingerprint_length_is_a_sha256_in_hex():
    """64 and not 32: this is the hex spelling's length, which is what crosses
    the wire and what the `claim` table stores."""
    assert FINGERPRINT_HEX_CHARS == 64
    assert len(hashlib.sha256(b"anything").hexdigest()) == FINGERPRINT_HEX_CHARS
