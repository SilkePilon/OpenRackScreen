from __future__ import annotations

import re
from contextlib import closing

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, WebSocket
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server.app import AppSettings, create_app
from ors_server.auth import Sessions, claim_password, require_session, verify_password
from ors_server.db import Database
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

# And every socket. `/ws/daemon` will belong here when task 8 writes it -- it
# authenticates with a pairing token, not a session -- and naming it here is
# what makes that a decision rather than an omission. `/ws/ui` never will.
OPEN_SOCKETS: set[str] = set()


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
