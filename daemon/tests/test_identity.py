"""The secret this rack is, and the six characters a human compares.

The claim endpoint is unauthenticated by necessity -- a daemon that has not
been approved holds no credential -- so the only thing standing between an
admin's click and a stranger's rack is that the code on the screen matches the
code on the Pi.
"""

from __future__ import annotations

import json
import stat

import pytest
from ors_daemon.identity import IDENTITY_BYTES, SHORT_CODE_CHARS, load_or_create


def test_the_two_constants_are_the_numbers_they_are_meant_to_be():
    """Literal, because every other assertion in this file reads the constant.

    `len(secret) == IDENTITY_BYTES` is satisfied by any value of
    `IDENTITY_BYTES`, including 4 -- the constant is on both sides. 32 is the
    input to a SHA-256; 6 base32 characters is 30 bits, which is far more than
    the number of racks anyone approves and few enough to read off a terminal
    without losing your place.
    """
    assert IDENTITY_BYTES == 32
    assert SHORT_CODE_CHARS == 6


def test_a_fresh_identity_is_random_and_persisted(tmp_path):
    path = tmp_path / "identity.json"
    first = load_or_create(path)
    assert len(first.secret) == IDENTITY_BYTES
    # Read back, not regenerated: the fingerprint is what the server stores, and
    # a rack whose identity changed on restart would file a new claim -- with a
    # different short code -- every time it rebooted.
    assert load_or_create(path) == first


def test_two_racks_are_not_the_same_rack(tmp_path):
    """An identity fixture where both sides coincide would hide a constant.

    `secrets.token_bytes` replaced by a fixed value passes any test that only
    ever creates one identity.
    """
    left = load_or_create(tmp_path / "a.json")
    right = load_or_create(tmp_path / "b.json")
    assert left.secret != right.secret
    assert left.fingerprint != right.fingerprint
    assert left.short_code != right.short_code


def test_the_file_is_private(tmp_path):
    """0600. It is the whole of this rack's claim to its own identity."""
    path = tmp_path / "identity.json"
    load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_secret_is_never_in_the_fingerprint(tmp_path):
    identity = load_or_create(tmp_path / "identity.json")
    assert identity.secret.hex() not in identity.fingerprint
    assert len(identity.fingerprint) == 64  # sha256, hex


def test_the_short_code_is_readable_and_short(tmp_path):
    identity = load_or_create(tmp_path / "identity.json")
    assert len(identity.short_code) == SHORT_CODE_CHARS
    # Base32 without padding: no lowercase, no 0/1/8, nothing a person reading
    # it off a terminal can confuse with something else.
    assert set(identity.short_code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_the_short_code_follows_the_fingerprint(tmp_path):
    """Derived, not stored. A code stored beside the fingerprint could drift
    from it, and then the thing a human matched would not be the thing the
    server keyed on."""
    identity = load_or_create(tmp_path / "identity.json")
    reloaded = load_or_create(tmp_path / "identity.json")
    assert reloaded.short_code == identity.short_code


def test_a_corrupt_identity_file_is_an_error_not_a_new_rack(tmp_path):
    """Regenerating silently would mint a second identity for one rack, and the
    pending claim an admin is looking at would stop being this daemon's."""
    path = tmp_path / "identity.json"
    path.write_text("not json")
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_missing_its_secret_is_an_error(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1}))
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)
