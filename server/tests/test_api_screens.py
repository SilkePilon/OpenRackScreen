from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_server.app import AppSettings, create_app

SCREEN = {
    "name": "CPU",
    # Deliberately not 1, and never equal to a row id this file creates: a
    # fixture whose position matched its id hid a frame-routing bug in task 11,
    # and contiguous ids from 1 hid two more in task 12.
    "position": 5,
    "display": {"backend": "virtual", "out_dir": "/tmp/p"},
    "template": "ring-gauge",
    "params": {"title": "CPU"},
}


class Rack:
    """A daemon's end of the hub, with no daemon behind it. See `test_api_daemons`."""

    def __init__(self, client: TestClient, daemon_id: int) -> None:
        self.sent: list[str] = []
        self.connection = client.app.state.hub.register(daemon_id, self.send)

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload if isinstance(payload, str) else payload.decode())

    @property
    def pushes(self) -> list[dict]:
        return [message for message in map(json.loads, self.sent) if message["type"] == "config"]


@pytest.fixture
def client(tmp_path) -> TestClient:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    # A rack this file never edits, so no daemon under test has row id 1 and no
    # screen's id can coincide with the only daemon in the table.
    client.post("/api/daemons", json={"name": "spare-rack"})
    return client


@pytest.fixture
def client_and_daemon(client: TestClient) -> tuple[TestClient, int]:
    return client, client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]


def version_of(client: TestClient, daemon_id: int) -> int:
    return next(
        daemon for daemon in client.get("/api/daemons").json() if daemon["id"] == daemon_id
    )["config_version"]


def create(client: TestClient, daemon_id: int, **overrides) -> dict:
    return client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id, **overrides}).json()


# --- the guard --------------------------------------------------------------


def test_every_screen_route_refuses_an_unauthenticated_caller(tmp_path):
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/screens").status_code == 401
    assert anonymous.get("/api/screens/1").status_code == 401
    assert anonymous.post("/api/screens", json={}).status_code == 401
    assert anonymous.patch("/api/screens/1", json={}).status_code == 401
    assert anonymous.delete("/api/screens/1").status_code == 401
    assert anonymous.post("/api/screens/reorder", json={"ids": [1]}).status_code == 401
    assert anonymous.get("/api/screens/1/preview").status_code == 401


# --- creating, editing, deleting --------------------------------------------


def test_creating_a_screen_bumps_the_daemons_config_version(client_and_daemon):
    client, daemon_id = client_and_daemon
    before = version_of(client, daemon_id)

    created = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id})

    assert created.status_code == 201
    assert version_of(client, daemon_id) > before, "an unchanged version means nothing is pushed"


def test_creating_a_screen_pushes_the_whole_rack_to_it(client_and_daemon):
    client, daemon_id = client_and_daemon
    rack = Rack(client, daemon_id)

    create(client, daemon_id)

    assert [screen["name"] for screen in rack.pushes[-1]["snapshot"]["screens"]] == ["CPU"]
    assert rack.pushes[-1]["version"] == version_of(client, daemon_id)


def test_creating_a_screen_for_a_daemon_that_does_not_exist_is_a_404(client):
    assert client.post("/api/screens", json={**SCREEN, "daemon_id": 4242}).status_code == 404


def test_patching_a_screen_bumps_it_again(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    before = version_of(client, daemon_id)

    assert client.patch(f"/api/screens/{screen_id}", json={"rotation": 90}).status_code == 200
    assert version_of(client, daemon_id) > before


def test_patching_one_field_leaves_the_others_alone(client_and_daemon):
    """A PATCH is not a PUT: a body naming `rotation` must not blank `params`,
    which is what `model_dump()` without `exclude_unset` would do."""
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]

    client.patch(f"/api/screens/{screen_id}", json={"rotation": 270})

    screen = client.get(f"/api/screens/{screen_id}").json()
    assert screen["rotation"] == 270
    assert screen["params"] == {"title": "CPU"}
    assert screen["name"] == "CPU"


def test_an_edit_for_an_offline_daemon_still_saves(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]

    client.patch(f"/api/screens/{screen_id}", json={"rotation": 180})

    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 180


def test_a_rotation_the_schema_refuses_is_a_422_and_changes_nothing(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    before = version_of(client, daemon_id)

    assert client.patch(f"/api/screens/{screen_id}", json={"rotation": 45}).status_code == 422
    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 0
    assert version_of(client, daemon_id) == before, "a refused edit must not look like a change"


def test_a_display_the_daemon_could_not_open_is_refused_at_the_boundary(client_and_daemon):
    """`DisplayConfig` says a virtual backend needs an `out_dir` and a gc9a01
    needs its two pins. The API validates with that model rather than a copy, so
    the answer is the one the daemon would have given, in the request that asked."""
    client, daemon_id = client_and_daemon

    refused = client.post(
        "/api/screens", json={**SCREEN, "daemon_id": daemon_id, "display": {"backend": "gc9a01"}}
    )

    assert refused.status_code == 422
    assert client.get("/api/screens").json() == []


def test_deleting_a_screen_bumps_and_pushes_what_is_left(client_and_daemon):
    client, daemon_id = client_and_daemon
    rack = Rack(client, daemon_id)
    going = create(client, daemon_id, name="CPU")["id"]
    create(client, daemon_id, name="MEM", position=9)

    assert client.delete(f"/api/screens/{going}").status_code == 200

    assert [screen["name"] for screen in rack.pushes[-1]["snapshot"]["screens"]] == ["MEM"]


def test_deleting_a_screen_that_is_already_gone_is_a_404(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]

    client.delete(f"/api/screens/{screen_id}")

    assert client.delete(f"/api/screens/{screen_id}").status_code == 404


def test_a_screen_is_only_ever_pushed_to_its_own_rack(client_and_daemon):
    """The bump is declared per daemon, and the default is every daemon.

    That default is the safe direction -- a route that forgets to declare
    over-pushes rather than silently losing an edit -- but over-pushing is a
    teardown and repaint of a rack nobody touched, so the declaration is pinned.
    """
    client, daemon_id = client_and_daemon
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    other_rack = Rack(client, other)

    create(client, daemon_id)

    assert other_rack.pushes == []
    assert version_of(client, other) == 0


# --- reading ----------------------------------------------------------------


def test_screens_are_listed_in_panel_order(client_and_daemon):
    client, daemon_id = client_and_daemon
    create(client, daemon_id, name="third", position=9)
    create(client, daemon_id, name="first", position=2)
    create(client, daemon_id, name="second", position=5)

    assert [screen["name"] for screen in client.get("/api/screens").json()] == [
        "first",
        "second",
        "third",
    ]


def test_screens_can_be_asked_for_one_rack_at_a_time(client_and_daemon):
    client, daemon_id = client_and_daemon
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    create(client, daemon_id, name="mine")
    create(client, other, name="theirs")

    listed = client.get("/api/screens", params={"daemon_id": daemon_id}).json()

    assert [screen["name"] for screen in listed] == ["mine"]


def test_a_screen_that_does_not_exist_is_a_404(client):
    assert client.get("/api/screens/4242").status_code == 404


# --- reorder ----------------------------------------------------------------


def test_reorder_renumbers_positions(client_and_daemon):
    client, daemon_id = client_and_daemon
    first = create(client, daemon_id, name="CPU")["id"]
    second = create(client, daemon_id, name="MEM", position=9)["id"]

    client.post("/api/screens/reorder", json={"ids": [second, first]})

    positions = {screen["name"]: screen["position"] for screen in client.get("/api/screens").json()}
    assert positions == {"MEM": 1, "CPU": 2}


def test_reorder_pushes_the_new_order(client_and_daemon):
    client, daemon_id = client_and_daemon
    rack = Rack(client, daemon_id)
    first = create(client, daemon_id, name="CPU")["id"]
    second = create(client, daemon_id, name="MEM", position=9)["id"]

    client.post("/api/screens/reorder", json={"ids": [second, first]})

    assert [screen["name"] for screen in rack.pushes[-1]["snapshot"]["screens"]] == ["MEM", "CPU"]


def test_reorder_naming_a_screen_that_does_not_exist_changes_nothing(client_and_daemon):
    client, daemon_id = client_and_daemon
    first = create(client, daemon_id, name="CPU")["id"]

    refused = client.post("/api/screens/reorder", json={"ids": [first, 4242]})

    assert refused.status_code == 404
    assert client.get(f"/api/screens/{first}").json()["position"] == 5


def test_reorder_needs_at_least_one_screen(client_and_daemon):
    client, _ = client_and_daemon

    assert client.post("/api/screens/reorder", json={"ids": []}).status_code == 422


def test_reorder_refuses_to_give_two_panels_one_position(client_and_daemon):
    """`ids` is the new order, so a repeated id is a request the schema cannot
    represent: two panels would be numbered from one entry and a third would
    keep a number nothing assigned."""
    client, daemon_id = client_and_daemon
    first = create(client, daemon_id, name="CPU")["id"]

    assert client.post("/api/screens/reorder", json={"ids": [first, first]}).status_code == 422


# --- preview ----------------------------------------------------------------


def test_preview_renders_a_png_without_a_daemon(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]

    response = client.get(f"/api/screens/{screen_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_draws_the_screens_own_template(client_and_daemon):
    """Two templates that cannot render the same image, so the route is really
    reading the screen's row rather than drawing whatever is first in the table."""
    client, daemon_id = client_and_daemon
    ring = create(client, daemon_id, name="ring", template="ring-gauge")["id"]
    text = create(client, daemon_id, name="text", template="text-only", position=9)["id"]

    first = client.get(f"/api/screens/{ring}/preview").content
    second = client.get(f"/api/screens/{text}/preview").content

    assert first != second


def test_preview_of_a_screen_that_does_not_exist_is_a_404(client):
    assert client.get("/api/screens/4242/preview").status_code == 404


def test_preview_of_a_screen_whose_template_is_gone_is_not_a_traceback(client_and_daemon):
    """A screen can outlive its template -- a snapshot refuses that, but a
    preview is asked for from a page that may be showing a stale list."""
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    with closing(client.app.state.database.connect()) as connection:
        connection.execute("UPDATE screen SET template = 'gone' WHERE id = ?", (screen_id,))

    assert client.get(f"/api/screens/{screen_id}/preview").status_code == 404


# --- what a mutation does when the snapshot will not assemble ---------------


def test_an_edit_that_makes_the_rack_unservable_is_refused_and_saves_nothing(client_and_daemon):
    """`build_snapshot` refuses a configuration no daemon can run, and it is
    assembled before the commit, so the edit that would cause it never lands."""
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    before = version_of(client, daemon_id)

    refused = client.patch(f"/api/screens/{screen_id}", json={"template": "no-such-template"})

    assert refused.status_code == 422
    assert "no-such-template" in refused.json()["detail"]
    assert client.get(f"/api/screens/{screen_id}").json()["template"] == "ring-gauge"
    assert version_of(client, daemon_id) == before, "a refused edit mints no version either"


def test_an_edit_to_a_rack_that_was_already_unservable_is_saved_anyway(client_and_daemon):
    """Otherwise the API is a trap you cannot get out of.

    A rack whose configuration cannot be assembled -- an integration holding a
    credential the wire format cannot carry, a column somebody edited by hand --
    would refuse every edit including the one that fixes it, because the refusal
    is about the state of the whole rack and not about the change. So the rule
    is narrower than "the snapshot must assemble": a change is refused only when
    it is what *made* the configuration unservable.
    """
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    with closing(client.app.state.database.connect()) as connection:
        secret_id = connection.execute("INSERT INTO secret (ciphertext) VALUES ('x')").lastrowid
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, enabled)"
            " VALUES (?, 'prometheus', 'prom', '{}', ?, 1)",
            (daemon_id, secret_id),
        )

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.status_code == 202, "saved, and honest that nothing was pushed"
    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 90


def test_a_rack_that_cannot_be_given_a_configuration_says_so_in_the_list(client_and_daemon):
    """The blank-rack signal, and the other half of the push button.

    Without it the only evidence is a log line on the server and four dark
    panels; with it the interface can say which rack is stuck and why.
    """
    client, daemon_id = client_and_daemon
    with closing(client.app.state.database.connect()) as connection:
        secret_id = connection.execute("INSERT INTO secret (ciphertext) VALUES ('x')").lastrowid
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, enabled)"
            " VALUES (?, 'prometheus', 'prom', '{}', ?, 1)",
            (daemon_id, secret_id),
        )

    listed = client.get("/api/daemons").json()

    stuck = next(daemon for daemon in listed if daemon["id"] == daemon_id)
    assert "credential" in stuck["config_error"]
    assert all(daemon["config_error"] is None for daemon in listed if daemon["id"] != daemon_id), (
        "one rack's broken configuration is not another's"
    )
