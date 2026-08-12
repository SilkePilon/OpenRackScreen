from __future__ import annotations

import re
from contextlib import closing

from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from ors_server.app import AppSettings, create_app
from ors_server.auth import Sessions, claim_password, require_session, verify_password
from ors_server.db import Database

# The key the hash is stored under, duplicated from `auth.py` rather than
# imported: it is a name on disk, so a rename is a migration, and a test that
# imported the constant would follow the rename silently.
PASSWORD_KEY = "admin_password_hash"

# Every documented path that answers without a session. Anything else is a route
# a future task hung off the open router -- see the last test in this file.
# FastAPI's own /api/docs and /api/openapi.json are not in the schema and so not
# in this list: they describe the API, not the rack.
OPEN_PATHS = {
    "/api/health",
    "/api/auth/me",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}


def app_and_client(tmp_path) -> tuple[FastAPI, TestClient]:
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.get("/api/guarded")
    def guarded(_: None = Depends(require_session)) -> dict[str, bool]:
        return {"ok": True}

    @app.state.api.get("/added-later")
    def added_later() -> dict[str, bool]:
        """A route as a later task will write one: on the router, saying nothing."""
        return {"ok": True}

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
    schema = app.openapi()["paths"]

    answers = {
        f"{method.upper()} {path}": getattr(client, method)(fill_in(path)).status_code
        for path, operations in schema.items()
        for method in operations
        if path not in OPEN_PATHS and method in {"get", "post", "put", "patch", "delete"}
    }

    assert OPEN_PATHS <= set(schema), "the open list names a route that no longer exists"
    assert set(answers.values()) == {401}, answers


def fill_in(path: str) -> str:
    """`/api/screens/{screen_id}` -> `/api/screens/1`, so the route can be called."""
    return re.sub(r"\{[^}]+\}", "1", path)
