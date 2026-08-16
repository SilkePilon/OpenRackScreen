from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ors_server.auth import (
    SESSION_COOKIE,
    claim_password,
    now,
    password_is_set,
    require_session,
    set_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class PasswordBody(BaseModel):
    """The one password `setup` claims and `login` proves.

    `repr=False` for the reason `Hello.token` and `IntegrationBody.credential`
    carry it, and `PasswordChange` below: a model's `repr` is what reaches a log
    the moment anybody drops one into an `extra`, and the field this holds is a
    plaintext admin password. There is nothing about being the *login* body that
    makes it safer to log than the change body two screens down.
    """

    password: str = Field(min_length=1, repr=False)


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

    sessions.clear_attempts(client)
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


class PasswordChange(BaseModel):
    """The current password and the one to replace it with.

    Both fields carry `repr=False`, for the reason `Hello.token` and
    `IntegrationBody.credential` do: a model's `repr` is what reaches a log the
    moment anybody drops one into an `extra`, and this body holds two secrets
    rather than one. `extra="forbid"` because a client that sends `password`
    here -- the field the other three auth routes take -- has sent the wrong
    document, and being told so is better than having half of it ignored.
    """

    model_config = ConfigDict(extra="forbid")

    current: str = Field(min_length=1, repr=False)
    new: str = Field(min_length=1, repr=False)


class PasswordChanged(BaseModel):
    """That it happened, and how many other browsers it signed out.

    A model rather than the `dict[str, bool]` the other three answer with,
    because `other_sessions_ended` is a number and because the generated
    TypeScript for a `dict` is an index signature -- every key `boolean`,
    whether the server sends it or not. The count is what lets the interface say
    "two other sign-ins were ended" rather than claiming it in prose that would
    still be there on a server where nothing else was signed in.
    """

    ok: bool
    other_sessions_ended: int


@router.post(
    "/password",
    dependencies=[Depends(require_session)],
    # The two statuses the interface branches on, in the document CI regenerates
    # `web/src/api/schema.d.ts` from -- `PasswordCard.refusal()` tells a 429 from
    # a 403 by number, and a status that is only in the source is a status the
    # drift check cannot notice going away. No `model`: these are FastAPI's own
    # `{"detail": "..."}`, which no route in this server declares a schema for,
    # and inventing one here would put a shape in the artefact that the app's
    # validation handler does not produce for a 422 beside it.
    responses={
        403: {"description": "the current password is wrong"},
        429: {"description": "too many password attempts from this client; see login"},
    },
)
def change_password(
    request: Request, body: PasswordChange, token: SessionCookie = None
) -> PasswordChanged:
    """Replace the admin password, proving the current one first.

    Session-guarded, unlike `login`: the cookie says who is asking, and
    `body.current` says they know what they are replacing. Both, because either
    alone is a password reset for whoever has the other -- a borrowed browser,
    or a guess.

    **A wrong current password is 403 and not 401.** 401 is this API's word for
    "there is no session here", and the interface hangs one handler off every
    response that sends a 401 to the login page. A refusal that said 401 would
    throw the admin out of the settings form for a typo, and the only defence
    would be a second URL exemption beside the login route's -- one that stops
    matching silently the day the SPA is served under a sub-path. This caller is
    authenticated; what is refused is the request.

    **Rate-limited on `request.client.host`, refused before the hash**, both for
    `login`'s reasons: `X-Forwarded-For` is written by whoever sends it, so
    trusting it would let one attacker be a new client on every request, and a
    limiter that verifies first is a CPU exhaustion endpoint -- argon2 is 0.58s
    by design. The counter is `login`'s own, not a second one: the two routes
    verify the same secret, and separate budgets would double the guessing rate
    for it.

    **Every other session ends; this one does not.** The reason to change a
    password is that it may have leaked, so the other holder must be logged out
    -- a change that left a stolen cookie working is a change that does nothing
    about the thing it was done for. Revoking the caller's own cookie as well
    would sign them out mid-request, which reads as a failure and sends them
    back to prove the password they have just this second set.

    It opens no `change`: no rack's configuration moves, so there is no version
    to bump, no snapshot to assemble and nobody to push to. See
    `test_api_routes.test_the_password_change_writes_a_row_and_so_is_not_an_exemption`
    for why that does not put it on `MUTATES_NOTHING`.
    """
    sessions = request.app.state.sessions
    client = request.client.host if request.client else "unknown"
    if sessions.too_many_attempts(client, now()):
        raise HTTPException(status_code=429, detail="too many attempts")

    if not verify_password(request.app.state.database, body.current):
        sessions.record_attempt(client, now())
        raise HTTPException(status_code=403, detail="the current password is wrong")

    sessions.clear_attempts(client)
    set_password(request.app.state.database, body.new)
    # After the write, so a failure to store the new password is not a building
    # full of sessions logged out for nothing.
    return PasswordChanged(ok=True, other_sessions_ended=sessions.revoke_others(token))


@router.post("/logout")
def logout(request: Request, response: Response, token: SessionCookie = None) -> dict[str, bool]:
    request.app.state.sessions.revoke(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
