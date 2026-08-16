from __future__ import annotations

import re
from contextlib import closing

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, WebSocket
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server.api.auth import PasswordChange
from ors_server.app import AppSettings, create_app
from ors_server.auth import Sessions, claim_password, require_session, verify_password
from ors_server.db import Database
from pydantic import BaseModel
from starlette.routing import BaseRoute, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

# The key the hash is stored under, duplicated from `auth.py` rather than
# imported: it is a name on disk, so a rename is a migration, and a test that
# imported the constant would follow the rename silently.
PASSWORD_KEY = "admin_password_hash"

# Every path that answers without a session. Anything else is a route a future
# task hung off the open router -- see the sweeps at the end of this file.
OPEN_PATHS = {
    "/api/health",
    "/api/auth/me",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
    # FastAPI's own. They describe the API, not the rack.
    "/api/docs",
    "/api/docs/oauth2-redirect",
    "/api/redoc",
    "/api/openapi.json",
}

# And every socket. `/ws/daemon` is here because it authenticates with a pairing
# token or the daemon key issued in its place -- credentials a rack holds and the
# admin's browser does not -- so `require_session` would refuse every Pi in the
# building. Naming it here is what makes that a decision rather than an omission,
# and `test_a_daemon_socket_still_refuses_a_stranger` below is what stops the
# entry from becoming a way in. `/ws/ui` never belongs here.
OPEN_SOCKETS = {"/ws/daemon"}


def app_and_client(tmp_path) -> tuple[FastAPI, TestClient]:
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.get("/api/guarded")
    def guarded(_: None = Depends(require_session)) -> dict[str, bool]:
        return {"ok": True}

    @app.state.api.get("/added-later")
    def added_later() -> dict[str, bool]:
        """A route as a later task will write one: on the router, saying nothing."""
        return {"ok": True}

    @app.state.api.websocket("/ws/added-later")
    async def socket_added_later(websocket: WebSocket) -> None:
        """And a socket as task 12 will write one. `/ws/ui` is the real one."""
        await websocket.accept()
        await websocket.send_json({"ok": True})

    return app, TestClient(app)


def setup_password(client: TestClient, password: str = "correct horse") -> None:
    assert client.post("/api/auth/setup", json={"password": password}).status_code == 200


def stored_hash(database: Database) -> str:
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT value FROM setting WHERE key = ?", (PASSWORD_KEY,)
        ).fetchone()
    return row["value"]


def test_a_fresh_server_reports_that_no_password_is_set(tmp_path):
    _, client = app_and_client(tmp_path)

    assert client.get("/api/auth/me").json() == {"authenticated": False, "password_set": False}


def test_setup_sets_the_password_once_and_then_refuses(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    second = client.post("/api/auth/setup", json={"password": "someone else's"})
    assert second.status_code == 409, "a second setup would be a password reset for anyone"


def test_setup_alone_does_not_let_you_in(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.get("/api/guarded").status_code == 401, "setting a password is not logging in"


def test_a_second_claim_never_replaces_the_first_password(tmp_path):
    """The check-then-write in `setup` is two statements; this is the one that matters.

    Two setup requests can both pass `password_is_set` before either writes, so
    the write itself has to be the thing that refuses.
    """
    app, _ = app_and_client(tmp_path)
    database = app.state.database

    assert claim_password(database, "first") is True
    assert claim_password(database, "second") is False
    assert verify_password(database, "first") is True
    assert verify_password(database, "second") is False


def test_login_grants_a_session_and_logout_takes_it_away(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/api/guarded").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/guarded").status_code == 401


def test_a_wrong_password_is_refused(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/guarded").status_code == 401


def test_the_session_cookie_is_http_only_and_same_site_strict(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)
    response = client.post("/api/auth/login", json={"password": "correct horse"})

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header, "a cookie readable by script is a cookie stealable by script"
    assert "samesite=strict" in header


def test_a_guarded_route_refuses_a_forged_cookie(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)
    client.cookies.set("ors_session", "not-a-real-token")

    assert client.get("/api/guarded").status_code == 401


def test_a_guarded_socket_refuses_a_caller_without_a_session(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect("/api/ws/added-later"),
    ):
        pass  # pragma: no cover -- the connection never opens

    assert denied.value.status_code == 401


def test_a_guarded_socket_lets_the_admin_in(tmp_path):
    """The half a guard that raises on every socket would still pass.

    `require_session` took a `Request`, which FastAPI does not fill in a socket
    scope, so it raised `TypeError` before it ever looked at the cookie -- for
    the admin with a valid session exactly as for a stranger. Failing closed for
    everyone is not a guard, it is an outage, and the way out of an outage is to
    move the socket somewhere nothing is checked.
    """
    _, client = app_and_client(tmp_path)
    setup_password(client)
    client.post("/api/auth/login", json={"password": "correct horse"})

    with client.websocket_connect("/api/ws/added-later") as socket:
        assert socket.receive_json() == {"ok": True}


def test_one_session_is_not_another(tmp_path):
    app, first = app_and_client(tmp_path)
    setup_password(first)
    first.post("/api/auth/login", json={"password": "correct horse"})

    second = TestClient(app)
    second.post("/api/auth/login", json={"password": "correct horse"})
    second.post("/api/auth/logout")

    assert first.get("/api/guarded").status_code == 200, "one logout must not log everyone out"


def test_repeated_failures_are_rate_limited(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    codes = [
        client.post("/api/auth/login", json={"password": "wrong"}).status_code for _ in range(12)
    ]

    assert 429 in codes, "an unlimited password endpoint is an offline attack with extra steps"


def test_proving_the_password_clears_the_count_it_had_to_get_past(tmp_path):
    """Otherwise the admin who just logged in is one typo from a lockout.

    Nine wrong, then right, then one slip, and the count is at ten from failures
    that were already answered for. It cannot help an attacker: the branch that
    clears the count is the branch where they already know the password.
    """
    _, client = app_and_client(tmp_path)
    setup_password(client)
    for _ in range(9):
        client.post("/api/auth/login", json={"password": "wrong"})

    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200


def test_the_rate_limit_window_passes_without_a_test_sleeping():
    sessions = Sessions()
    for tick in range(20):
        sessions.record_attempt("10.0.0.9", float(tick))

    assert sessions.too_many_attempts("10.0.0.9", 20.0) is True
    assert sessions.too_many_attempts("10.0.0.9", 3600.0) is False, "the window has to reopen"


def test_one_client_being_locked_out_does_not_lock_out_the_rack():
    sessions = Sessions()
    for tick in range(20):
        sessions.record_attempt("10.0.0.9", float(tick))

    assert sessions.too_many_attempts("10.0.0.10", 20.0) is False


def test_forgotten_clients_do_not_accumulate_forever():
    """Whitebox, because an unbounded dict is invisible from outside until it is not.

    Every attempt is keyed by an address nobody chose, from anyone who can reach
    the port, and nothing else ever deletes those keys.
    """
    sessions = Sessions()
    for host in range(50):
        sessions.record_attempt(f"10.0.0.{host}", 0.0)

    sessions.too_many_attempts("10.0.1.1", 3600.0)

    assert sessions._attempts.keys() <= {"10.0.1.1"}


def test_revoking_the_others_keeps_exactly_the_one_that_asked():
    sessions = Sessions()
    mine = sessions.issue()
    theirs = [sessions.issue() for _ in range(3)]

    assert sessions.revoke_others(mine) == 3
    assert sessions.valid(mine) is True
    assert [sessions.valid(token) for token in theirs] == [False, False, False]


def test_revoking_the_others_with_no_session_to_keep_keeps_nobody():
    """Not reachable through the route, which is guarded and therefore always has
    a valid cookie to keep -- and stated here because "keep the caller's" must
    not quietly become "keep everyone" when there is no caller to keep. A token
    that is not a session is the same case: it cannot be kept, because it is not
    in the set to begin with."""
    sessions = Sessions()
    tokens = [sessions.issue(), sessions.issue()]

    assert sessions.revoke_others(None) == 2
    assert sessions.revoke_others("not a token anybody issued") == 0
    assert [sessions.valid(token) for token in tokens] == [False, False]


def test_the_password_is_not_stored_in_the_clear(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client, "correct horse")

    assert b"correct horse" not in (tmp_path / "ors.db").read_bytes()


def test_no_response_ever_repeats_the_password_back(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)
    bodies = [
        client.post("/api/auth/login", json={"password": "hunter2"}).text,
        # A client that misspells the field hands the password to the validator,
        # which is only too happy to quote the whole body back.
        client.post("/api/auth/login", json={"passwrod": "hunter2"}).text,
        client.post("/api/auth/setup", json={"passwrod": "hunter2"}).text,
    ]

    assert not any("hunter2" in body for body in bodies), bodies
    assert all("password" in body for body in bodies[1:]), "and still says which field was wrong"


def test_a_verified_password_is_rehashed_when_the_parameters_move(tmp_path):
    app, client = app_and_client(tmp_path)
    setup_password(client)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    with closing(app.state.database.connect()) as connection:
        connection.execute(
            "UPDATE setting SET value = ? WHERE key = ?",
            (weak.hash("correct horse"), PASSWORD_KEY),
        )

    assert verify_password(app.state.database, "correct horse") is True
    assert PasswordHasher().check_needs_rehash(stored_hash(app.state.database)) is False


def test_a_hash_that_is_not_a_hash_is_a_refusal_not_a_crash(tmp_path):
    app, client = app_and_client(tmp_path)
    setup_password(client)
    with closing(app.state.database.connect()) as connection:
        connection.execute(
            "UPDATE setting SET value = ? WHERE key = ?", ("not a hash at all", PASSWORD_KEY)
        )

    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 401


# The three passwords the change tests use, all different and none a substring
# of another -- a body that put the current password where the new one belongs,
# or a route that stored the wrong one of the two, is only catchable if no two
# of them are the same string. "correct horse" is `setup_password`'s default and
# is the one already set.
NEW_PASSWORD = "eight red lanterns"
WRONG_GUESS = "a guess nobody set"


def change_password(client: TestClient, current: str, new: str):
    return client.post("/api/auth/password", json={"current": current, "new": new})


def signed_in(tmp_path) -> tuple[FastAPI, TestClient]:
    """An app with a password set and one browser signed in to it."""
    app, client = app_and_client(tmp_path)
    setup_password(client)
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    return app, client


def verify_that_counts(calls: list[str], right: str):
    """A stand-in for `verify_password` that records what it was asked.

    Two things need it. Argon2 is 0.58s by design and the per-test budget is ten
    seconds, so eleven real verifications do not fit in a test about counting to
    ten. And "the limiter refuses **before** the hash" is not observable at all
    from outside -- both orders answer 429 -- so the only way to pin it is to
    watch whether the expensive collaborator was reached.

    It answers a bool, never raises: a test that signalled through an exception
    would be caught by `verify_password`'s own handler in production and would
    pass against code that had stopped verifying anything.
    """

    def verify(database, password: str) -> bool:  # noqa: ANN001 - the real one's `Database`
        calls.append(password)
        return password == right

    return verify


def test_the_password_can_be_changed_and_the_new_one_is_what_logs_in(tmp_path):
    app, client = signed_in(tmp_path)

    changed = change_password(client, "correct horse", NEW_PASSWORD)

    assert changed.status_code == 200
    assert changed.json() == {"ok": True, "other_sessions_ended": 0}
    assert verify_password(app.state.database, NEW_PASSWORD) is True
    assert verify_password(app.state.database, "correct horse") is False
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": NEW_PASSWORD}).status_code == 200


def test_a_change_without_the_current_password_is_refused_and_changes_nothing(tmp_path):
    """The half that makes the field worth asking for. A route that wrote the new
    password whatever the old one was would pass every other test in this block."""
    app, client = signed_in(tmp_path)

    refused = change_password(client, WRONG_GUESS, NEW_PASSWORD)

    assert refused.status_code == 403
    assert verify_password(app.state.database, "correct horse") is True
    assert verify_password(app.state.database, NEW_PASSWORD) is False


def test_a_wrong_current_password_is_not_a_401_because_that_means_the_session_ended(tmp_path):
    """401 is the interface's word for "you have been signed out".

    The SPA hangs one handler off every response and sends every 401 to /login,
    keyed on the endpoint that refused -- `POST /api/auth/login` is the single
    exemption, because a wrong password there is not an expiry. A change route
    answering 401 for a mistyped *current* password would therefore throw the
    admin out of the settings page mid-form and lose the new password they had
    just typed, and the only fix would be a second URL exemption that stops
    matching silently the day the interface is served under a sub-path.

    So the refusal is 403: this caller **is** authenticated, and what is being
    refused is the request.
    """
    _, client = signed_in(tmp_path)

    refused = change_password(client, WRONG_GUESS, NEW_PASSWORD)

    assert refused.status_code == 403
    assert refused.json()["detail"] == "the current password is wrong"
    assert client.get("/api/guarded").status_code == 200, "the session must survive a typo"


def test_changing_the_password_ends_every_other_session_and_keeps_this_one(tmp_path):
    """The decision this route had to make, stated as behaviour.

    Somebody changes the password because they think it leaked, so the other
    holder has to be logged out -- a password change that left a stolen cookie
    working changes nothing about the thing it was done for. Revoking the
    caller's own cookie as well would sign *them* out mid-request, which reads
    as a failure and sends them back to a login form to prove the password they
    have just this moment set.
    """
    app, first = signed_in(tmp_path)
    second = TestClient(app)
    assert second.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200

    changed = change_password(first, "correct horse", NEW_PASSWORD)

    assert changed.json()["other_sessions_ended"] == 1
    assert first.get("/api/guarded").status_code == 200, "the caller must not be signed out"
    assert second.get("/api/guarded").status_code == 401, "the other holder must be"


def test_a_refused_change_ends_nobody(tmp_path):
    """The other half: a wrong current password must not log the rest of the
    building out. Otherwise anyone holding one session has a logout button for
    everybody else's, without knowing the password at all."""
    app, first = signed_in(tmp_path)
    second = TestClient(app)
    second.post("/api/auth/login", json={"password": "correct horse"})

    change_password(first, WRONG_GUESS, NEW_PASSWORD)

    assert second.get("/api/guarded").status_code == 200


def test_the_change_is_refused_before_the_hash_the_way_login_is(tmp_path, monkeypatch):
    """Verifying is expensive on purpose, so a limiter that pays for it first is
    a CPU exhaustion endpoint -- ten guesses a minute per address, each costing
    0.58s of a server that is relaying frames for four panels.

    Both orders answer 429, so the ordering is only visible as whether the
    collaborator was reached at all.
    """
    _, client = signed_in(tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(
        "ors_server.api.auth.verify_password", verify_that_counts(asked, "correct horse")
    )
    codes = [change_password(client, WRONG_GUESS, NEW_PASSWORD).status_code for _ in range(11)]
    assert codes[-1] == 429, "an unlimited password endpoint is an offline attack with extra steps"

    before = len(asked)
    refused = change_password(client, WRONG_GUESS, NEW_PASSWORD)

    assert refused.status_code == 429
    assert asked[before:] == [], "the hash was computed for a request the limiter had refused"


def test_the_limit_is_not_reset_by_a_header_the_caller_writes(tmp_path, monkeypatch):
    """`X-Forwarded-For` is written by whoever sends it.

    Keying on it would let one attacker be a new client on every request and
    never meet the limit at all -- which is not a weaker limit, it is no limit,
    on the route that replaces the password. `request.client.host` is what the
    connection really came from, and behind a reverse proxy it is uvicorn's
    `--proxy-headers` that makes that the real client.
    """
    _, client = signed_in(tmp_path)
    monkeypatch.setattr(
        "ors_server.api.auth.verify_password", verify_that_counts([], "correct horse")
    )

    codes = [
        client.post(
            "/api/auth/password",
            json={"current": WRONG_GUESS, "new": NEW_PASSWORD},
            headers={"X-Forwarded-For": f"198.51.100.{attempt}"},
        ).status_code
        for attempt in range(12)
    ]

    assert 429 in codes, "a new address per request must not be a fresh budget per request"


def test_a_burst_of_wrong_current_passwords_counts_against_the_login_too(tmp_path, monkeypatch):
    """One secret, one budget.

    The two routes verify the same password, so separate counters would hand an
    attacker with a session twice the guessing rate for it -- and the rate is
    the whole of what the limiter buys. The cost is that an admin who fumbles
    the old password ten times waits out the same sixty seconds at the login
    form, which is a rolling window and the same wait the login route already
    imposes on itself.
    """
    _, client = signed_in(tmp_path)
    monkeypatch.setattr(
        "ors_server.api.auth.verify_password", verify_that_counts([], "correct horse")
    )
    for _ in range(10):
        change_password(client, WRONG_GUESS, NEW_PASSWORD)

    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429


def test_proving_the_current_password_clears_the_count_it_had_to_get_past(tmp_path, monkeypatch):
    """`login` clears its count on success and this must too, or the admin who
    just proved they know the password is one typo from a lockout. It cannot
    help an attacker: the branch that clears is the branch where they knew it.

    Nine, right, one slip, right -- `login`'s own shape, and it has to be that
    shape. Nine and then one slip leaves the count at ten either way, and it is
    the *next* request that is refused, so a test that stopped at the slip passes
    against a route that never clears anything. Measured: it did.
    """
    _, client = signed_in(tmp_path)
    monkeypatch.setattr(
        "ors_server.api.auth.verify_password", verify_that_counts([], "correct horse")
    )
    for _ in range(9):
        change_password(client, WRONG_GUESS, NEW_PASSWORD)

    assert change_password(client, "correct horse", NEW_PASSWORD).status_code == 200
    assert change_password(client, WRONG_GUESS, NEW_PASSWORD).status_code == 403
    assert change_password(client, "correct horse", NEW_PASSWORD).status_code == 200


def test_neither_password_reaches_a_repr():
    """A model's `repr` is what reaches a log the moment anybody drops one into
    an `extra`, which is why `Hello.token` and `IntegrationBody.credential` both
    carry `repr=False`. This body carries two secrets, not one."""
    body = PasswordChange(current="correct horse", new=NEW_PASSWORD)

    class Leaky(BaseModel):
        secret: str

    assert NEW_PASSWORD in repr(Leaky(secret=NEW_PASSWORD)), (
        "the fixture proves nothing if a plain pydantic repr hides a field anyway"
    )
    assert "correct horse" not in repr(body)
    assert NEW_PASSWORD not in repr(body)


def test_a_password_change_never_repeats_either_password_back(tmp_path):
    _, client = signed_in(tmp_path)
    bodies = [
        change_password(client, WRONG_GUESS, NEW_PASSWORD).text,
        # A client that misspells a field hands both passwords to the validator,
        # which is only too happy to quote the whole body back.
        client.post("/api/auth/password", json={"currnet": WRONG_GUESS, "new": NEW_PASSWORD}).text,
    ]

    assert not any(WRONG_GUESS in body for body in bodies), bodies
    assert not any(NEW_PASSWORD in body for body in bodies), bodies
    assert "current" in bodies[1], "and still says which field was wrong"


def test_health_stays_open(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.get("/api/health").status_code == 200, "a health check nobody can call is useless"


def test_every_other_api_route_is_guarded_including_ones_not_written_yet(tmp_path):
    """The guard hangs off the router, so a route added to it is guarded by default.

    This is the test that fails when a later task hangs a route off the open
    router instead, which is the only way one can end up unguarded. It asks the
    app what routes it has rather than being told, so it covers the ones nobody
    has written yet -- and it asks by calling them, because a session is only
    really required if the answer is 401.
    """
    app, client = app_and_client(tmp_path)
    setup_password(client)

    assert OPEN_PATHS <= {path for path, _ in registered(app)}, "the open list is out of date"
    assert unguarded_paths(client, app) == set()


def test_the_sweep_sees_a_route_that_is_not_in_the_schema(tmp_path):
    """`include_in_schema=False` is a decorator argument, not a security boundary."""
    app, client = app_and_client(tmp_path)

    @app.get("/api/hidden", include_in_schema=False)
    def hidden() -> dict[str, bool]:
        return {"ok": True}

    assert unguarded_paths(client, app) == {"GET /api/hidden"}


def test_every_socket_is_guarded_too(tmp_path):
    app, client = app_and_client(tmp_path)
    setup_password(client)

    assert unguarded_sockets(client, app) == set()


def test_a_daemon_socket_still_refuses_a_stranger(tmp_path):
    """What being on the open list does and does not buy.

    It opens: a socket with no session is accepted rather than denied at the
    handshake, because the credential travels in the first message. It is then
    refused there instead, and this is the test that says the exemption is about
    *where* the check happens rather than whether there is one.
    """
    app, client = app_and_client(tmp_path)
    setup_password(client)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(
            '{"type": "hello", "token": "", "hostname": "pi-rack", "daemon_version": "0.1.0"}'
        )
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 4401


def test_the_sweep_sees_a_socket_at_all(tmp_path):
    """The sweep this replaced could not: FastAPI puts no socket in the schema.

    Its path list was identical with and without a socket registered, so the one
    route type that streams the whole rack live was the one type it was blind to.
    """
    app, client = app_and_client(tmp_path)

    @app.websocket("/ws/wide-open")
    async def wide_open(websocket: WebSocket) -> None:
        await websocket.accept()

    assert unguarded_sockets(client, app) == {"/ws/wide-open"}


def registered(app: FastAPI) -> list[tuple[str, BaseRoute]]:
    """Every route the app will match, with the path it matches on.

    `app.routes` is not that list: FastAPI 0.141 leaves an internal
    `_IncludedRouter` behind for each `include_router` and resolves it lazily, so
    walking it directly finds neither `/api/health` nor any socket. This is the
    enumeration FastAPI's own OpenAPI generation runs on -- used here instead of
    the OpenAPI document, which contains no websocket at all and drops anything
    marked `include_in_schema=False`.
    """
    return [
        # A socket's effective path comes back empty; its own already carries
        # the prefix of the router it was declared on.
        (context.path or context.original_route.path, context.original_route)
        for context in iter_route_contexts(app.routes)
    ]


def unguarded_paths(client: TestClient, app: FastAPI) -> set[str]:
    """`METHOD /path` for every /api route that answers a caller with no session."""
    return {
        f"{method} {path}"
        for path, route in registered(app)
        if path.startswith("/api")
        and path not in OPEN_PATHS
        and not isinstance(route, WebSocketRoute)
        for method in sorted(getattr(route, "methods", None) or ())
        if client.request(method, fill_in(path)).status_code != 401
    }


def unguarded_sockets(client: TestClient, app: FastAPI) -> set[str]:
    """Every socket, anywhere in the app, that opens for a caller with no session.

    Not filtered to /api: the spec puts `/ws/ui` and `/ws/daemon` at the root, so
    a prefix is exactly the wrong thing to key on.
    """
    opened = set()
    for path, route in registered(app):
        if not isinstance(route, WebSocketRoute) or path in OPEN_SOCKETS:
            continue
        try:
            with client.websocket_connect(fill_in(path)):
                opened.add(path)
        except (WebSocketDenialResponse, WebSocketDisconnect):
            pass
    return opened


def fill_in(path: str) -> str:
    """`/api/screens/{screen_id}` -> `/api/screens/1`, so the route can be called."""
    return re.sub(r"\{[^}]+\}", "1", path)
