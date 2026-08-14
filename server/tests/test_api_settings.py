from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_server.api.changes import UNSERVABLE_HEADER
from ors_server.api.settings import READABLE, _stored
from ors_server.app import AppSettings, create_app

PASSWORD = "correct horse battery staple"


class Rack:
    """A daemon's end of the hub, with no daemon behind it. See `test_api_daemons`."""

    def __init__(self, client: TestClient, daemon_id: int) -> None:
        self.sent: list[str] = []
        self.connection = client.app.state.hub.register(daemon_id, self.send)

    @property
    def pushes(self) -> list[dict]:
        return [message for message in map(json.loads, self.sent) if message["type"] == "config"]

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload if isinstance(payload, str) else payload.decode())


@pytest.fixture
def client(tmp_path) -> TestClient:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": PASSWORD})
    client.post("/api/auth/login", json={"password": PASSWORD})
    client.post("/api/daemons", json={"name": "spare-rack"})
    return client


def version_of(client: TestClient, daemon_id: int) -> int:
    return next(
        daemon for daemon in client.get("/api/daemons").json() if daemon["id"] == daemon_id
    )["config_version"]


def make_unservable(client: TestClient, daemon_id: int) -> None:
    """An enabled integration holding a credential -- see `test_api_screens`."""
    with closing(client.app.state.database.connect()) as connection:
        secret_id = connection.execute("INSERT INTO secret (ciphertext) VALUES ('x')").lastrowid
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, enabled)"
            " VALUES (?, 'prometheus', 'prom', '{}', ?, 1)",
            (daemon_id, secret_id),
        )


def test_both_settings_routes_refuse_an_unauthenticated_caller(tmp_path):
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/settings").status_code == 401
    assert anonymous.patch("/api/settings", json={"timezone": "UTC"}).status_code == 401


def test_a_fresh_server_reports_the_defaults_the_schema_would_have_used(client):
    settings = client.get("/api/settings").json()

    assert settings["timezone"] == "UTC"
    assert settings["night"] == {"enabled": True, "start": "23:00", "end": "07:00"}


def test_the_admin_password_hash_is_not_a_setting_this_route_answers_with(client):
    """`setting` is a key-value table and the password hash lives in it. A
    `SELECT *` here is one line, and it puts an argon2 hash of the admin's
    password into a JSON response that the SPA then holds in memory.

    `SettingsView` is what really does it -- a route answering with the whole
    table fails this test, measured. The `key IN (...)` in `_stored` is a second
    line, and no assertion about a *response* can see it while the model still
    names its two fields; `test_the_settings_read_never_loads_a_key_it_does_not_answer_with`
    is what covers that one, at the function rather than at the route.
    """
    with closing(client.app.state.database.connect()) as connection:
        stored = connection.execute(
            "SELECT value FROM setting WHERE key = 'admin_password_hash'"
        ).fetchone()["value"]

    body = client.get("/api/settings").text

    assert stored.startswith("$argon2"), "the fixture proves nothing without a hash to look for"
    assert stored not in body
    assert "argon2" not in body


def test_a_key_somebody_else_stores_is_not_answered_with_either(client):
    """A second key nobody has thought of yet, black-box at the route.

    This passes with or without the allow-list, because `SettingsView` filters --
    which is what the docstring used to claim it proved. It is kept for what it
    really says: no *response* carries a foreign key, whichever of the two lines
    is doing it.
    """
    with closing(client.app.state.database.connect()) as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('smtp_password', 'hunter2')")

    assert "hunter2" not in client.get("/api/settings").text


def test_the_settings_read_never_loads_a_key_it_does_not_answer_with(client):
    """The allow-list itself, which is only visible below the response model.

    `setting` is a key-value table holding whatever anybody has put there --
    today the argon2 hash of the admin's password and these two. `SettingsView`
    is what stops a foreign key reaching a response *today*; the `key IN (...)`
    is what stops one the day somebody builds a response from this dict, which
    is one obvious refactor away. Asserted here because a test at the route
    cannot tell the two apart, so the second line had no test at all.
    """
    with closing(client.app.state.database.connect()) as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('smtp_password', 'hunter2')")

        loaded = _stored(connection)

    assert "hunter2" not in loaded.values()
    assert set(loaded) <= set(READABLE)
    assert "admin_password_hash" not in loaded, (
        "the fixture proves nothing if the table holds only the two readable keys"
    )


def test_setting_the_timezone_stores_it_and_answers_with_it(client):
    patched = client.patch("/api/settings", json={"timezone": "Europe/Amsterdam"})

    assert patched.status_code == 200
    assert patched.json()["timezone"] == "Europe/Amsterdam"
    assert client.get("/api/settings").json()["timezone"] == "Europe/Amsterdam"


def test_a_timezone_this_host_cannot_resolve_is_refused_and_changes_nothing(client):
    """`DaemonConfig.timezone` is a free string and the daemon's `system_clock`
    is what fails on it -- as a rack that will not start, hours later. The typo
    is caught in the request that made it."""
    refused = client.patch("/api/settings", json={"timezone": "Mars/Olympus_Mons"})

    assert refused.status_code == 422
    assert client.get("/api/settings").json()["timezone"] == "UTC"


def test_a_night_window_the_schema_refuses_is_a_422(client):
    assert client.patch("/api/settings", json={"night": {"start": "25:00"}}).status_code == 422


def test_setting_the_night_window_leaves_the_timezone_alone(client):
    client.patch("/api/settings", json={"timezone": "Europe/Amsterdam"})

    client.patch("/api/settings", json={"night": {"start": "22:30", "end": "06:15"}})

    settings = client.get("/api/settings").json()
    assert settings["timezone"] == "Europe/Amsterdam"
    assert settings["night"]["start"] == "22:30"


def test_a_settings_change_reaches_every_rack(client):
    first = client.post("/api/daemons", json={"name": "one"}).json()["id"]
    second = client.post("/api/daemons", json={"name": "two"}).json()["id"]
    racks = [Rack(client, first), Rack(client, second)]

    client.patch("/api/settings", json={"timezone": "Europe/Amsterdam"})

    for rack in racks:
        assert rack.pushes[-1]["snapshot"]["timezone"] == "Europe/Amsterdam"
    assert version_of(client, first) == 1
    assert version_of(client, second) == 1


def test_a_settings_change_is_a_plain_success_even_when_one_rack_is_broken(client):
    """These routes affect *every* rack, so one stuck rack must not make every
    rack-wide edit read as unapplied.

    Before this rule was per edit rather than per server, a single rack with a
    hand-edited column turned `PATCH /api/settings` and every template edit into
    a 202 forever -- while they went on pushing successfully to every healthy
    rack, with a body byte-identical to the 200 and naming nothing.
    """
    healthy = client.post("/api/daemons", json={"name": "one"}).json()["id"]
    broken = client.post("/api/daemons", json={"name": "two"}).json()["id"]
    rack = Rack(client, healthy)
    make_unservable(client, broken)

    patched = client.patch("/api/settings", json={"timezone": "Europe/Amsterdam"})

    assert patched.status_code == 200
    assert patched.headers[UNSERVABLE_HEADER] == str(broken)
    assert rack.pushes[-1]["snapshot"]["timezone"] == "Europe/Amsterdam"


def test_the_night_window_a_rack_is_pushed_is_the_one_that_was_saved(client):
    daemon_id = client.post("/api/daemons", json={"name": "one"}).json()["id"]
    rack = Rack(client, daemon_id)

    client.patch("/api/settings", json={"night": {"enabled": False}})

    assert rack.pushes[-1]["snapshot"]["night"]["enabled"] is False
