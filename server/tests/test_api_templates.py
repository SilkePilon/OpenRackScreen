from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from ors_server.app import AppSettings, create_app

DISPLAY = {"backend": "virtual", "out_dir": "/tmp/p"}

SCENES = [
    {
        "name": "mine",
        "elements": [
            # A rectangle drawn as an outline only. `fill` is nullable with a
            # *non-null* default, so a serialiser using `exclude_none` drops the
            # null and this comes back a solid white box -- measured, in task 6.
            {"type": "rect", "fill": None, "stroke": "#ff0000"},
        ],
    }
]


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
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    client.post("/api/daemons", json={"name": "spare-rack"})
    return client


def by_name(client: TestClient, name: str) -> dict:
    return next(row for row in client.get("/api/templates").json() if row["name"] == name)


def test_every_template_route_refuses_an_unauthenticated_caller(tmp_path):
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/templates").status_code == 401
    assert anonymous.get("/api/templates/1").status_code == 401
    assert anonymous.post("/api/templates", json={}).status_code == 401
    assert anonymous.patch("/api/templates/1", json={}).status_code == 401
    assert anonymous.delete("/api/templates/1").status_code == 401


def test_the_built_ins_are_listed_because_the_editor_draws_from_them(client):
    listed = client.get("/api/templates").json()

    assert {"ring-gauge", "text-only", "system"} <= {row["name"] for row in listed}
    assert by_name(client, "ring-gauge")["builtin"] is True


def test_a_template_can_be_created_and_read_back(client):
    created = client.post("/api/templates", json={"name": "mine", "scenes": SCENES})

    assert created.status_code == 201
    assert created.json()["builtin"] is False
    assert client.get(f"/api/templates/{created.json()['id']}").json()["name"] == "mine"


def test_a_stroke_only_rectangle_comes_back_stroke_only(client):
    """The `exclude_none` trap task 6 measured, at the boundary that now writes
    the column. Storing a scene through anything that drops nulls turns a
    cleared colour back into the field's default -- a solid white box, in this
    case -- and the editor writes those nulls the moment anybody clears one."""
    created = client.post("/api/templates", json={"name": "mine", "scenes": SCENES}).json()

    element = created["scenes"][0]["elements"][0]

    assert element["fill"] is None
    assert element["stroke"] == "#ff0000"


def test_a_second_template_by_the_same_name_is_a_conflict(client):
    client.post("/api/templates", json={"name": "mine", "scenes": SCENES})

    again = client.post("/api/templates", json={"name": "mine", "scenes": SCENES})

    assert again.status_code == 409


def test_renaming_a_template_onto_another_is_a_conflict_not_a_traceback(client):
    client.post("/api/templates", json={"name": "mine", "scenes": SCENES})
    other = client.post("/api/templates", json={"name": "yours", "scenes": SCENES}).json()

    assert client.patch(f"/api/templates/{other['id']}", json={"name": "mine"}).status_code == 409


def test_a_template_cannot_claim_to_be_a_built_in(client):
    """`builtin` says where a row came from, and the daemon reads it to tell an
    override of its own built-in from the server's copy of one."""
    refused = client.post(
        "/api/templates", json={"name": "mine", "scenes": SCENES, "builtin": True}
    )

    assert refused.status_code == 422


def test_a_template_edit_reaches_every_rack(client):
    """A snapshot carries all the templates, so this really is every rack's
    configuration changing -- not an approximation of one."""
    first = client.post("/api/daemons", json={"name": "one"}).json()["id"]
    second = client.post("/api/daemons", json={"name": "two"}).json()["id"]
    racks = [Rack(client, first), Rack(client, second)]
    made = client.post("/api/templates", json={"name": "mine", "scenes": SCENES}).json()
    template_id = made["id"]

    client.patch(f"/api/templates/{template_id}", json={"category": "gauge"})

    for rack in racks:
        assert rack.pushes[-1]["snapshot"]["templates"]["mine"]["category"] == "gauge"


def test_a_built_in_may_be_amended_because_the_seed_will_not_revert_it(client):
    """`seed_builtin_templates` inserts `ON CONFLICT DO NOTHING` precisely so an
    edited built-in survives the next restart."""
    ring = by_name(client, "ring-gauge")

    amended = client.patch(f"/api/templates/{ring['id']}", json={"category": "mine"})

    assert amended.status_code == 200
    assert by_name(client, "ring-gauge")["category"] == "mine"


def test_a_built_in_may_not_be_deleted_because_it_would_come_back(client):
    ring = by_name(client, "ring-gauge")

    refused = client.delete(f"/api/templates/{ring['id']}")

    assert refused.status_code == 409
    assert "re-seeded" in refused.json()["detail"]
    assert by_name(client, "ring-gauge")


def test_a_template_a_screen_is_using_cannot_be_deleted_out_from_under_it(client):
    """Refused by the snapshot rather than by a lookup here: `resolve_screens`
    raises over the *whole* configuration when a screen names a template that is
    not defined, so one deleted template darkens the entire rack."""
    daemon_id = client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]
    template = client.post("/api/templates", json={"name": "mine", "scenes": SCENES}).json()
    client.post(
        "/api/screens",
        json={
            "daemon_id": daemon_id,
            "name": "CPU",
            "position": 5,
            "display": DISPLAY,
            "template": "mine",
        },
    )

    refused = client.delete(f"/api/templates/{template['id']}")

    assert refused.status_code == 422
    assert by_name(client, "mine"), "the row is still there, because the delete was rolled back"


def test_a_template_nothing_uses_can_be_deleted(client):
    template = client.post("/api/templates", json={"name": "mine", "scenes": SCENES}).json()

    assert client.delete(f"/api/templates/{template['id']}").status_code == 200
    assert all(row["name"] != "mine" for row in client.get("/api/templates").json())


def test_a_template_that_does_not_exist_is_a_404(client):
    assert client.get("/api/templates/4242").status_code == 404
    assert client.patch("/api/templates/4242", json={"category": "x"}).status_code == 404
    assert client.delete("/api/templates/4242").status_code == 404


def test_a_scene_the_render_engine_could_not_draw_is_refused(client):
    """Validated with `ors_schema.scene`'s own models, so the API and the rack
    refuse the same document for the same reason."""
    refused = client.post(
        "/api/templates",
        json={"name": "mine", "scenes": [{"elements": [{"type": "no-such-element"}]}]},
    )

    assert refused.status_code == 422
