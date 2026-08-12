from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ors_server.auth import (
    SESSION_COOKIE,
    claim_password,
    now,
    password_is_set,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class PasswordBody(BaseModel):
    password: str = Field(min_length=1)


@router.get("/me")
def me(request: Request, token: SessionCookie = None) -> dict[str, bool]:
    """Open, because the SPA has to know whether to show login or first-run setup."""
    return {
        "authenticated": request.app.state.sessions.valid(token),
        "password_set": password_is_set(request.app.state.database),
    }


@router.post("/setup")
def setup(request: Request, body: PasswordBody) -> dict[str, bool]:
    database = request.app.state.database
    # The cheap refusal first, so a configured rack does not pay for an argon2
    # hash per unauthenticated request; `claim_password` is what actually
    # decides, because two of these can be in flight at once.
    if password_is_set(database) or not claim_password(database, body.password):
        raise HTTPException(status_code=409, detail="a password is already set")
    return {"ok": True}


@router.post("/login")
def login(request: Request, response: Response, body: PasswordBody) -> dict[str, bool]:
    sessions = request.app.state.sessions
    # `request.client.host` and not the `X-Forwarded-For` header: that header is
    # written by whoever sends it, so trusting it here would let one attacker be
    # a new client on every request and never meet the limit at all. Behind a
    # reverse proxy it is uvicorn's `--proxy-headers` (on by default, trusting
    # `--forwarded-allow-ips`) that makes this the real client; where it is not
    # configured, every request is the proxy and the limit is one for the whole
    # rack -- 60 seconds of it, since the window rolls.
    client = request.client.host if request.client else "unknown"
    if sessions.too_many_attempts(client, now()):
        # Refused before the hash, not after: verifying is expensive on purpose,
        # and a limiter that still pays for it is a CPU exhaustion endpoint.
        raise HTTPException(status_code=429, detail="too many attempts")

    if not verify_password(request.app.state.database, body.password):
        sessions.record_attempt(client, now())
        raise HTTPException(status_code=401, detail="wrong password")

    response.set_cookie(
        SESSION_COOKIE,
        sessions.issue(),
        httponly=True,
        samesite="strict",
        path="/",
        # No `max_age`: a session cookie, gone when the browser closes. No
        # `secure` either -- this milestone is plain HTTP on a LAN, and a secure
        # cookie would simply never be sent back. TLS is a reverse proxy's job,
        # and the flag belongs with it.
    )
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response, token: SessionCookie = None) -> dict[str, bool]:
    request.app.state.sessions.revoke(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
