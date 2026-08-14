from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from ors_server.db import Database

_KEY_FILE = "secret.key"

# Fernet is the `cryptography` recipes layer: AES-128-CBC with an HMAC, one
# decision to make (the key) and no way to make it wrong. The docs recommend the
# recipes layer "whenever possible", so nothing here reaches into hazmat.
#
# The key is the entire boundary. Lose it and every stored credential is gone;
# leak it and every stored credential is readable, because the ciphertext lives
# in a SQLite file sitting right beside it. Hence the mode on the file below.
#
# Nothing here rotates a key, and a changed `ORS_SECRET_KEY` therefore turns
# every stored secret into an `InvalidToken` -- at first use, hours after a boot
# that looked fine. Detecting that belongs at startup, where the store is wired
# up: trial-decrypt one existing row and refuse to start. That is a later task's
# to add; it is recorded here so it is not discovered by an integration failing
# alone at 3am.


def load_or_create_key(data_dir: Path, configured: str | None) -> bytes:
    """The configured key if there is one, else a generated one kept at 0600.

    Raises `ValueError` if `ORS_SECRET_KEY` is set to something that is not a
    Fernet key, and `PermissionError` if the key file on disk is readable by
    anyone but its owner.
    """
    if configured is not None:
        # `is not None`, not truthiness: `ORS_SECRET_KEY=` would otherwise mean
        # "unset" and quietly fall back to the file's key, which is a silent key
        # change -- every stored secret undecryptable, no signal, by omission.
        if not configured.strip():
            raise ValueError("ORS_SECRET_KEY is set but empty; unset it or give it a key")
        key = configured.encode()
        # Checked here rather than left to the first `Fernet(...)`, so a typo in
        # the environment is a startup failure naming the variable rather than
        # one integration failing hours later. The key itself is never in the
        # message.
        try:
            Fernet(key)
        except (ValueError, TypeError) as error:
            raise ValueError(
                "ORS_SECRET_KEY is not a Fernet key: it must be 32 url-safe"
                " base64-encoded bytes, as from Fernet.generate_key()"
            ) from error
        return key

    path = Path(data_dir) / _KEY_FILE
    if path.exists():
        # A key anyone can read is not a key, however it got that way -- a
        # `chmod -R`, a restore that flattened modes, a config-management
        # default. Refuse rather than warn: nothing else would ever go wrong, so
        # a warning in a successful boot log is a finding no one comes back to,
        # and the remedy is one `chmod 600` with nothing lost.
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{path} is mode {mode:o} and readable by more than its owner;"
                " every stored credential is encrypted under it. chmod 600 it."
            )
        return path.read_bytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Written before the mode is set would leave a readable window, so the
    # descriptor is opened with the mode already on it. The mode comes from the
    # open and not from the directory, so a data directory someone left
    # group-writable still gets a private key file -- though anyone with write
    # on that directory can of course replace the file wholesale.
    #
    # O_NOFOLLOW because a symlink planted at this path would otherwise send the
    # new key wherever it points, outside the directory whose mode we control.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


class SecretStore:
    """Credentials at rest. Nothing here ever returns to the browser.

    There is deliberately no accessor for the stored ciphertext: the only ways
    out are `get`, which decrypts for a snapshot being assembled for a daemon,
    and the export, which redacts the column.
    """

    def __init__(self, database: Database, key: bytes) -> None:
        self.database = database
        self._fernet = Fernet(key)

    def put(self, plaintext: str) -> int:
        with closing(self.database.connect()) as connection:
            return self.put_on(connection, plaintext)

    def put_on(self, connection: sqlite3.Connection, plaintext: str) -> int:
        """Store a credential on the caller's connection, and return its row id.

        The configuration API writes a secret in the same transaction as the
        integration row that references it, which is the only ordering that is
        correct in both directions: a `put` on a connection of its own would be
        a second writer against a database the edit has already locked, and if
        it somehow got past that it would leave a ciphertext behind whenever the
        edit that wanted it was rolled back -- unreachable through any route,
        undeletable, and in every export of the database from then on.
        """
        token = self._fernet.encrypt(plaintext.encode()).decode()
        cursor = connection.execute("INSERT INTO secret (ciphertext) VALUES (?)", (token,))
        return int(cursor.lastrowid)

    def get(self, secret_id: int) -> str:
        """The plaintext. `KeyError` if the row is gone, `InvalidToken` if the key is wrong.

        The two are distinct on purpose: one means a secret was deleted, the
        other means this database was encrypted under a different key.
        """
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT ciphertext FROM secret WHERE id = ?", (secret_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"no secret {secret_id}")
        return self._fernet.decrypt(row["ciphertext"].encode()).decode()

    def delete(self, secret_id: int) -> None:
        with closing(self.database.connect()) as connection:
            self.delete_on(connection, secret_id)

    def delete_on(self, connection: sqlite3.Connection, secret_id: int) -> None:
        """Forget a credential on the caller's connection. See `put_on`."""
        connection.execute("DELETE FROM secret WHERE id = ?", (secret_id,))
