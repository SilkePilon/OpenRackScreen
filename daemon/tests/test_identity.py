"""The secret this rack is, and the six characters a human compares.

The claim endpoint is unauthenticated by necessity -- a daemon that has not
been approved holds no credential -- so the only thing standing between an
admin's click and a stranger's rack is that the code on the screen matches the
code on the Pi.
"""

from __future__ import annotations

import base64
import json
import stat

import pytest
from ors_daemon.identity import _VERSION, IDENTITY_BYTES, SHORT_CODE_CHARS, load_or_create


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
    # `write_text` leaves whatever mode the umask allows, which on a typical
    # 022 umask is world-readable -- pin it to 0600 so this test exercises
    # the corrupt-content path and not the mode check below.
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_missing_its_secret_is_an_error(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_with_an_empty_secret_is_an_error(tmp_path):
    """`{"secret": ""}` decodes to `b""`, whose SHA-256 is a fixed, publicly
    computable value -- the short code of every rack whose secret field was
    ever emptied by a truncate, a zeroed restore, or a crash mid-write."""
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1, "secret": ""}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_with_a_short_secret_is_an_error(tmp_path):
    path = tmp_path / "identity.json"
    short = base64.b64encode(b"xx").decode()
    path.write_text(json.dumps({"version": 1, "secret": short}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_with_a_non_string_secret_is_an_error(tmp_path):
    """`123` or `null` reach `base64.b64decode` as a `TypeError`, which must
    be caught and reworded rather than escaping as a bare Python error with no
    mention of the file or what to do about it."""
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1, "secret": 123}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_with_unparseable_base64_is_an_error(tmp_path):
    """`validate=True`: the default decoder discards non-alphabet characters
    instead of raising, which turns garbage into a short, wrong secret rather
    than an error."""
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1, "secret": "!!!not base64!!!"}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_path_that_is_a_directory_is_an_error(tmp_path):
    """`IsADirectoryError` is an `OSError`, not previously in the caught set,
    so it used to escape as a bare traceback instead of the guided message."""
    path = tmp_path / "identity.json"
    path.mkdir()
    path.chmod(0o700)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_the_version_is_pinned():
    """A hypothetical v2 file must not silently load as if it were v1."""
    assert _VERSION == 1


def test_an_identity_with_the_wrong_version_is_an_error(tmp_path):
    path = tmp_path / "identity.json"
    secret = base64.b64encode(b"x" * IDENTITY_BYTES).decode()
    path.write_text(json.dumps({"version": 2, "secret": secret}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_file_anyone_else_can_read_is_refused(tmp_path):
    """A loose mode is silent forever otherwise: nothing stops working, so
    nothing tells. Reachable by a `chmod -R`, a backup restore that flattens
    modes, or a config-management default -- and it leaves the fingerprint
    and short code recomputable by anyone who can read the file, which is the
    whole of what the admin checks."""
    path = tmp_path / "identity.json"
    identity = load_or_create(path)
    path.chmod(0o644)

    with pytest.raises(PermissionError) as raised:
        load_or_create(path)

    message = str(raised.value)
    assert str(path) in message and "644" in message, "an operator has to be told which file"
    assert identity.secret.hex() not in message, "and never told the secret"


def test_an_identity_file_only_the_owner_can_read_is_accepted(tmp_path):
    path = tmp_path / "identity.json"
    identity = load_or_create(path)
    path.chmod(0o400)

    assert load_or_create(path) == identity


def test_the_identity_file_is_private_even_in_a_permissive_directory(tmp_path):
    """The mode comes from the open, not from the directory it lands in.

    A data directory left group- or world-writable is a plausible deployment
    mistake, and the identity file has to survive it: it is the whole of what
    the admin's click depends on.
    """
    data_dir = tmp_path / "loose"
    data_dir.mkdir()
    # chmod rather than `mkdir(mode=...)`, which the umask would take the
    # group and other bits straight back off again, leaving the test not
    # testing this.
    data_dir.chmod(0o777)
    load_or_create(data_dir / "identity.json")

    assert stat.S_IMODE((data_dir / "identity.json").stat().st_mode) == 0o600


def test_the_identity_path_is_not_followed_through_a_symlink(tmp_path):
    """A planted symlink would otherwise write the new identity outside the
    directory whose mode this module controls."""
    elsewhere = tmp_path / "elsewhere.json"
    (tmp_path / "identity.json").symlink_to(elsewhere)

    with pytest.raises(OSError):
        load_or_create(tmp_path / "identity.json")
    assert not elsewhere.exists(), "no identity was written through the link"
