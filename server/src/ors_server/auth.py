from __future__ import annotations

import secrets as _secrets
import threading
import time
from contextlib import closing
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Cookie, HTTPException, Request

from ors_server.db import Database

SESSION_COOKIE = "ors_session"
_PASSWORD_KEY = "admin_password_hash"
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 60.0

# argon2-cffi's defaults are `argon2.profiles.RFC_9106_LOW_MEMORY` -- argon2id,
# m=64MiB, t=3, p=4 -- which is above every configuration OWASP's password
# storage cheat sheet lists as sufficient (its largest is 46MiB, t=1, p=1). They
# are deliberately not pinned here: the library moves them when hardware moves,
# and `verify_password` rehashes on the next successful login when it does.
_hasher = PasswordHasher()


def password_is_set(database: Database) -> bool:
    with closing(database.connect()) as connection:
        row = connection.execute("SELECT 1 FROM setting WHERE key = ?", (_PASSWORD_KEY,)).fetchone()
    return row is not None


def set_password(database: Database, password: str) -> None:
    """Set or replace the admin password. Use `claim_password` for the first one."""
    with closing(database.connect()) as connection:
        connection.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PASSWORD_KEY, _hasher.hash(password)),
        )


def claim_password(database: Database, password: str) -> bool:
    """Set the password only if there is none. False if someone got there first.

    `setup` cannot ask `password_is_set` and then write: two requests can both
    read "no password" before either writes, and the second would be a password
    reset performed by whoever sent it. The refusal has to be the write itself,
    which is one statement and therefore one SQLite transaction.
    """
    with closing(database.connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (_PASSWORD_KEY, _hasher.hash(password)),
        )
        return cursor.rowcount == 1


def verify_password(database: Database, password: str) -> bool:
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT value FROM setting WHERE key = ?", (_PASSWORD_KEY,)
        ).fetchone()
    if row is None:
        return False
    stored = row["value"]
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        # `VerificationError` covers the wrong password; `InvalidHashError`
        # covers a `setting` row that is not a hash at all, which is a database
        # someone edited and still only means "not authenticated". Neither the
        # password nor the hash goes anywhere near a message.
        return False
    if _hasher.check_needs_rehash(stored):
        # A successful login is the only moment the plaintext exists, so it is
        # the only chance to move a hash onto the current parameters. OWASP and
        # argon2-cffi both name this as the way to do it.
        set_password(database, password)
    return True


class Sessions:
    """In-memory sessions. A restart logs everyone out, which is acceptable here.

    One admin, one rack, and a server that is expected to be restarted -- there
    is nothing to gain from persisting a token that only ever names "the admin",
    and a session table would outlive the browser that owns it. Tokens carry no
    expiry for the same reason: the cookie is a session cookie, so it dies with
    the browser, and the server forgets everything on the next restart.
    """

    def __init__(self) -> None:
        self._tokens: set[str] = set()
        self._attempts: dict[str, list[float]] = {}
        # A `def` endpoint runs in the threadpool, so several logins really are
        # concurrent. Set operations are atomic enough on their own; the attempt
        # bookkeeping is a read, a rebuild and a write, and a failed attempt lost
        # between them is a failed attempt the limiter did not count.
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = _secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return token is not None and token in self._tokens

    def revoke(self, token: str | None) -> None:
        self._tokens.discard(token or "")

    def too_many_attempts(self, client: str, now: float) -> bool:
        with self._lock:
            # Every client is swept, not just this one: the keys are addresses
            # chosen by whoever can reach the port, nothing else deletes them,
            # and a dict that only grows is a slow leak an unauthenticated
            # caller controls.
            self._attempts = {
                address: recent
                for address, attempts in self._attempts.items()
                if (recent := [at for at in attempts if now - at < _WINDOW_SECONDS])
            }
            return len(self._attempts.get(client, ())) >= _MAX_ATTEMPTS

    def record_attempt(self, client: str, now: float) -> None:
        with self._lock:
            self._attempts.setdefault(client, []).append(now)


def require_session(
    request: Request,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> None:
    """Every API route and both sockets sit behind this."""
    sessions: Sessions = request.app.state.sessions
    if not sessions.valid(token):
        raise HTTPException(status_code=401, detail="not authenticated")


def now() -> float:
    return time.monotonic()
