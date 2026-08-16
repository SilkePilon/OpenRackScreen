from __future__ import annotations

import secrets as _secrets
import threading
import time
from contextlib import closing
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Cookie, HTTPException
from starlette.requests import HTTPConnection

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

    def revoke_others(self, token: str | None) -> int:
        """End every session but this one, and answer how many that was.

        For a password change: whoever else is holding a cookie issued against
        the old password is holding one issued against a password that is being
        replaced, most often because somebody thinks it leaked. The caller's own
        is kept, because revoking it would sign them out of the request they are
        making -- with the new password set and a login form to prove it at.

        `token` is whatever cookie the caller sent, and it is kept only if it
        really is a session: a token nobody issued keeps nobody, rather than
        being added to the set on its way through.

        The whole set is rebuilt rather than discarded from, so a login that
        raced this one -- with the old password, since the new one is written a
        line earlier -- loses its token instead of surviving the revocation.
        That is the direction to be wrong in: the cost is one admin logging in
        again, and the alternative is the holder this method exists to remove.
        """
        with self._lock:
            keep = self._tokens & ({token} if token is not None else set())
            ended = len(self._tokens) - len(keep)
            self._tokens = keep
            return ended

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

    def clear_attempts(self, client: str) -> None:
        """Called when the password was right, so failures already answered for
        cannot add up to a lockout for the one caller who has proved they know it.
        """
        with self._lock:
            self._attempts.pop(client, None)


def require_session(
    connection: HTTPConnection,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> None:
    """Every API route and both sockets sit behind this.

    `HTTPConnection` and not `Request`, because that is the base `Request` and
    `WebSocket` share. FastAPI fills a `Request` parameter only in an HTTP scope,
    so a `Request` here was never supplied on a socket and this raised
    `TypeError` before reading the cookie -- refusing the admin and the stranger
    alike, which reads as a broken socket rather than as a guard.
    """
    sessions: Sessions = connection.app.state.sessions
    if not sessions.valid(token):
        raise HTTPException(status_code=401, detail="not authenticated")


def now() -> float:
    return time.monotonic()
