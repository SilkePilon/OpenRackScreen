"""What this installation of the daemon is, independently of any server.

Generated once by `ors-daemon install` and kept for the life of the machine.
It survives re-pairing and outlives any single server: a rack that is denied,
re-approved, or pointed at a different server is still the same rack, and the
six characters a human compares must not move under them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

IDENTITY_BYTES = 32
"""The secret's length, in bytes. 32 because it is the input to a SHA-256, and
a shorter input adds nothing while a longer one is hashed down anyway."""

SHORT_CODE_CHARS = 6
"""How much of the fingerprint a person is asked to compare.

Six base32 characters is 30 bits: about a billion, which is far more than the
number of racks anyone will ever approve, and few enough to read off a
terminal without losing your place. It is a check against *confusion*, not
against a determined collision -- the fingerprint is what the server keys on,
and this is what the admin looks at."""

_VERSION = 1


@dataclass(frozen=True)
class Identity:
    """A rack's own name for itself.

    `secret` never leaves the Pi. `fingerprint` is what the server stores, and
    `short_code` is what the interface shows.
    """

    secret: bytes
    fingerprint: str
    short_code: str


def _derive(secret: bytes) -> Identity:
    digest = hashlib.sha256(secret).digest()
    # Base32 and not hex: hex is 4 bits a character, so six characters would be
    # 24 bits, and it contains no letters past F -- which makes a short code
    # look like a number and reads worse aloud.
    code = base64.b32encode(digest).decode("ascii")[:SHORT_CODE_CHARS]
    return Identity(secret=secret, fingerprint=digest.hex(), short_code=code)


def load_or_create(path: Path) -> Identity:
    """Read this machine's identity, generating one the first time.

    A file that exists but does not parse raises rather than being replaced.
    Regenerating silently would give one rack a second identity, and the
    pending claim an admin is looking at would quietly stop being this
    daemon's -- so they would be approving something that no longer exists,
    and this rack would file a third claim behind it.

    Also refuses a file readable by more than its owner, matching
    `server/src/ors_server/secrets.py`'s precedent for the same reason: anyone
    who can read this secret recomputes the fingerprint and the short code,
    and can file a claim matching what the Pi's screen printed -- which is the
    whole of what the admin checks. A `chmod -R`, a restore, or a
    config-management default can loosen the mode with nothing else ever
    breaking, so nothing else would ever tell.
    """
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{path} is mode {mode:o} and readable by more than its owner;"
                " it is the entire proof that this rack is this rack. chmod 600 it."
            )
        try:
            document = json.loads(path.read_text())
            # `["secret"]` before `.get("version")`: a document that is valid
            # JSON but not an object (a list, a number) has no `.get`, and
            # indexing it raises `TypeError` -- inside the caught set below --
            # instead of an `AttributeError` that would escape it.
            raw = document["secret"]
            version = document.get("version")
            if version != _VERSION:
                raise ValueError(f"unsupported identity version {version!r}, expected {_VERSION}")
            # `validate=True`: the default silently discards any character
            # outside the base64 alphabet instead of raising, which turns
            # garbage input into a short, wrong secret rather than an error.
            secret = base64.b64decode(raw, validate=True)
            if len(secret) != IDENTITY_BYTES:
                raise ValueError(
                    f"secret is {len(secret)} bytes, not {IDENTITY_BYTES} -- "
                    "an empty or truncated secret is a publicly computable identity"
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as error:
            raise ValueError(
                f"{path} is not a readable identity: {error}. "
                "Delete it to mint a new one, which costs a re-approval."
            ) from error
        return _derive(secret)

    secret = secrets.token_bytes(IDENTITY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened with the mode already on it: writing first and chmod-ing after
    # leaves a window where the file is world-readable, and this file is the
    # entire proof that this rack is this rack.
    #
    # O_NOFOLLOW because a symlink planted at this path would otherwise send
    # the new identity wherever it points, outside the directory whose mode we
    # control.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"version": _VERSION, "secret": base64.b64encode(secret).decode()}, handle)
    return _derive(secret)
