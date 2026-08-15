from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_schema.link import (
    MAX_SCREEN_NAME,
    DetectResult,
    PanelCandidate,
    parse_daemon_message,
)
from ors_server.api.changes import UNSERVABLE_HEADER
from ors_server.api.screens import MAX_NAME
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


def make_unservable(client: TestClient, daemon_id: int) -> None:
    """Put this rack into the state `build_snapshot` refuses, behind the API's back.

    An enabled integration holding a credential, which no wire format in M3a can
    carry. Written straight to the database because the API refuses to create it
    -- which is the point: this is the state a hand-edited or restored file
    arrives in, and the whole reason the "already unservable" branch exists.
    """
    with closing(client.app.state.database.connect()) as connection:
        secret_id = connection.execute("INSERT INTO secret (ciphertext) VALUES ('x')").lastrowid
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, enabled)"
            " VALUES (?, 'prometheus', 'prom', '{}', ?, 1)",
            (daemon_id, secret_id),
        )


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


def test_preview_draws_the_screens_own_parameters(client_and_daemon):
    """A template is a scene with holes in it and the parameters are what fill
    them, so a preview that rendered without them would draw the same picture
    for every screen using one template -- which is most of a rack."""
    client, daemon_id = client_and_daemon
    one = create(client, daemon_id, name="one", params={"title": "CPU"})["id"]
    two = create(client, daemon_id, name="two", position=9, params={"title": "MEMORY"})["id"]

    first = client.get(f"/api/screens/{one}/preview").content
    second = client.get(f"/api/screens/{two}/preview").content

    assert first != second


def test_preview_falls_back_to_the_templates_declared_defaults(client_and_daemon):
    """`bind_params` is the bridge between `params_schema`, where a default is
    written, and what a renderer reads. A preview using the row's parameters
    alone would draw a screen that has overridden nothing as an empty panel."""
    client, daemon_id = client_and_daemon
    client.post(
        "/api/templates",
        json={
            "name": "defaulted",
            "params_schema": {"label": {"type": "string", "default": "from the template"}},
            "scenes": [{"elements": [{"type": "text", "text": "{{params.label}}", "size": 40}]}],
        },
    )
    plain = create(client, daemon_id, name="plain", template="defaulted", params={})["id"]
    overridden = create(
        client,
        daemon_id,
        name="overridden",
        position=9,
        template="defaulted",
        params={"label": "from the screen"},
    )["id"]

    default_drawn = client.get(f"/api/screens/{plain}/preview").content
    override_drawn = client.get(f"/api/screens/{overridden}/preview").content

    assert default_drawn != override_drawn
    blank = client.get(f"/api/screens/{_blank(client, daemon_id)}/preview").content
    assert default_drawn != blank, "the declared default has to reach the canvas"


def _blank(client: TestClient, daemon_id: int) -> int:
    """A screen of the same template whose parameter really is empty."""
    client.post(
        "/api/templates",
        json={
            "name": "undefaulted",
            "params_schema": {"label": {"type": "string"}},
            "scenes": [{"elements": [{"type": "text", "text": "{{params.label}}", "size": 40}]}],
        },
    )
    return create(client, daemon_id, name="blank", position=11, template="undefaulted")["id"]


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
    make_unservable(client, daemon_id)

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.status_code == 202, "saved, and honest that nothing was pushed"
    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 90


def test_a_rack_that_cannot_be_given_a_configuration_says_so_in_the_list(client_and_daemon):
    """The blank-rack signal, and the other half of the push button.

    Without it the only evidence is a log line on the server and four dark
    panels; with it the interface can say which rack is stuck and why.
    """
    client, daemon_id = client_and_daemon
    make_unservable(client, daemon_id)

    listed = client.get("/api/daemons").json()

    stuck = next(daemon for daemon in listed if daemon["id"] == daemon_id)
    assert "credential" in stuck["config_error"]
    assert all(daemon["config_error"] is None for daemon in listed if daemon["id"] != daemon_id), (
        "one rack's broken configuration is not another's"
    )


# --- one unreadable column must not take the whole server down --------------


TOO_DEEP = "[" * 20_000 + "]" * 20_000
"""A well-formed JSON document `json.loads` cannot reach the bottom of.

`RecursionError` derives from `RuntimeError`, not from `ValueError`, so it goes
straight past every guard written for a column that will not parse. Only
reachable from a database somebody hand-edited or restored -- which is exactly
the state `config_error` exists to report, and the state in which a 500 leaves
the operator with no page to read it on.
"""


def corrupt(client: TestClient, screen_id: int, column: str, value: str) -> None:
    with closing(client.app.state.database.connect()) as connection:
        connection.execute(
            f"UPDATE screen SET {column} = ? WHERE id = ?",  # noqa: S608 - from the caller
            (value, screen_id),
        )


@pytest.mark.parametrize("value", ["not json at all", TOO_DEEP])
def test_one_unreadable_column_does_not_take_the_daemon_list_down(client_and_daemon, value):
    """`GET /api/daemons` is the one place `config_error` is reported, so it is
    the last route that may fail because a rack's configuration is broken. It
    assembles a snapshot per rack, and an exception from any of them answers 500
    for *every* rack -- including the ones that are fine."""
    client, daemon_id = client_and_daemon
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    screen_id = create(client, daemon_id)["id"]
    corrupt(client, screen_id, "params", value)

    listed = client.get("/api/daemons")

    assert listed.status_code == 200
    stuck = next(daemon for daemon in listed.json() if daemon["id"] == daemon_id)
    assert stuck["config_error"] and "params" in stuck["config_error"]
    assert next(d for d in listed.json() if d["id"] == other)["config_error"] is None


@pytest.mark.parametrize("value", ["not json at all", TOO_DEEP])
def test_one_unreadable_column_does_not_make_the_screen_list_unloadable(client_and_daemon, value):
    """One bad row on one rack made the editor's whole list a 500, so there was
    no page from which to find or fix it. Reported the way an unreadable
    `daemon.capabilities` is -- empty, logged, and named in `config_error` --
    rather than by refusing to answer."""
    client, daemon_id = client_and_daemon
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    kept = create(client, other, name="MEM", position=9)["id"]
    screen_id = create(client, daemon_id)["id"]
    corrupt(client, screen_id, "params", value)

    listed = client.get("/api/screens")

    assert listed.status_code == 200
    assert {screen["id"] for screen in listed.json()} == {screen_id, kept}
    assert next(s for s in listed.json() if s["id"] == screen_id)["params"] == {}
    assert next(s for s in listed.json() if s["id"] == kept)["params"] == {"title": "CPU"}


def test_the_daemon_list_survives_an_assembly_that_recurses(client_and_daemon, monkeypatch):
    """`_json_column` turns the reachable case into a `SnapshotError`, so this
    drives the guard behind it: anything *else* in the assembly that recurses
    over a document a hand-edited database can make arbitrarily deep. It is the
    outer net, and a net is only there for what got past the first one -- so it
    is asserted by injection rather than left to a path that happens not to
    exist today."""
    client, daemon_id = client_and_daemon

    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("ors_server.api.changes.build_snapshot", too_deep)

    listed = client.get("/api/daemons")

    assert listed.status_code == 200
    stuck = next(daemon for daemon in listed.json() if daemon["id"] == daemon_id)
    assert stuck["config_error"] and "deep" in stuck["config_error"]


def test_an_edit_survives_an_assembly_that_recurses_anywhere(client_and_daemon, monkeypatch):
    """The same net at `_servable`, which is the question "was this rack already
    broken". Escaping there makes the *edit* a 500 -- so the repair would be
    refused along with everything else, which is the trap the whole branch
    exists to avoid."""
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    make_unservable(client, daemon_id)

    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("ors_server.api.changes.build_snapshot", too_deep)

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.status_code == 202, "already broken, so saved and not pushed rather than refused"
    assert saved.headers[UNSERVABLE_HEADER] == str(daemon_id)


def test_an_edit_to_a_rack_with_an_unreadable_column_is_still_saved(client_and_daemon):
    """The trap this closes from the other side: the rack is unservable, so the
    edit is saved and not pushed rather than refused -- which is what makes the
    edit that repairs it possible at all."""
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    corrupt(client, screen_id, "params", TOO_DEEP)

    saved = client.patch(f"/api/screens/{screen_id}", json={"params": {"title": "fixed"}})

    assert saved.status_code == 200, "the repair itself makes the rack servable again"
    assert client.get(f"/api/screens/{screen_id}").json()["params"] == {"title": "fixed"}


# --- what a mutation *says* when it could not reach a rack ------------------


def test_the_response_names_the_racks_the_edit_could_not_reach(client_and_daemon):
    """202 alone says only that something somewhere is wrong.

    The body of a 202 is byte-identical to the body of a 200 and names no rack,
    so an interface reading it has to re-list every daemon and diff
    `config_error` to find out which one it was. The header is the answer, and it
    is on every mutation whose affected set was not fully reached -- 200 and 201
    included, which is where the status code cannot say it.
    """
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]
    make_unservable(client, daemon_id)

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.headers[UNSERVABLE_HEADER] == str(daemon_id)


def test_an_edit_that_reached_every_rack_says_nothing_about_unservable_ones(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = create(client, daemon_id)["id"]

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.status_code == 200
    assert UNSERVABLE_HEADER not in saved.headers


def test_a_create_keeps_its_201_and_reports_the_rack_in_the_header(client_and_daemon):
    """201 is a stronger claim than 202 and this must not silently overwrite it.

    The row *was* created and its representation is in the body -- which is what
    the interface routes on -- and the fact that no rack was given it is the
    header's to carry. Narrowing the status is only ever done to the default.
    """
    client, daemon_id = client_and_daemon
    make_unservable(client, daemon_id)

    created = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id})

    assert created.status_code == 201
    assert created.headers[UNSERVABLE_HEADER] == str(daemon_id)
    assert created.json()["id"]


def test_one_broken_rack_does_not_make_every_racks_edit_read_as_unapplied(client):
    """202 is per edit, not per server.

    `POST /api/screens/reorder` may name screens on several racks, and the
    rack-wide routes affect every rack there is. If one broken rack were enough,
    202 would stop meaning "your edit was not applied" and start meaning
    "something somewhere is wrong" -- and the edit that really was not applied
    would be indistinguishable from one that reached three racks out of four.
    """
    broken = client.post("/api/daemons", json={"name": "broken-rack"}).json()["id"]
    healthy = client.post("/api/daemons", json={"name": "healthy-rack"}).json()["id"]
    rack = Rack(client, healthy)
    first = create(client, broken, name="A", position=3)["id"]
    second = create(client, healthy, name="B", position=4)["id"]
    make_unservable(client, broken)

    reordered = client.post("/api/screens/reorder", json={"ids": [second, first]})

    assert reordered.status_code == 200, "the healthy rack was pushed, so this was applied"
    assert reordered.headers[UNSERVABLE_HEADER] == str(broken)
    assert rack.pushes, "and the rack that could be reached really was"


def test_the_racks_are_named_in_a_settled_order(client):
    """One edit may name screens on several racks, so the header may name
    several -- and it is read by an interface, not by a person. Ascending row id,
    which is the contract M3b is given, rather than whatever order the racks
    were reached in.

    The screens are named in the reorder highest rack first, so the request's
    own order is the opposite of the answer's.
    """
    lower = client.post("/api/daemons", json={"name": "one-rack"}).json()["id"]
    higher = client.post("/api/daemons", json={"name": "two-rack"}).json()["id"]
    assert lower < higher, "the fixture proves nothing if these are the same way round"
    on_higher = create(client, higher, name="A", position=3)["id"]
    on_lower = create(client, lower, name="B", position=4)["id"]
    make_unservable(client, higher)
    make_unservable(client, lower)

    reordered = client.post("/api/screens/reorder", json={"ids": [on_higher, on_lower]})

    assert reordered.status_code == 202
    assert reordered.headers[UNSERVABLE_HEADER] == f"{lower},{higher}"


def test_an_edit_no_rack_at_all_received_is_the_one_that_is_a_202(client):
    """The other side of the same rule, and what 202 now means exactly."""
    broken = client.post("/api/daemons", json={"name": "broken-rack"}).json()["id"]
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    screen_id = create(client, broken, name="A", position=3)["id"]
    make_unservable(client, broken)
    make_unservable(client, other)

    saved = client.patch(f"/api/screens/{screen_id}", json={"rotation": 90})

    assert saved.status_code == 202
    assert saved.headers[UNSERVABLE_HEADER] == str(broken), (
        "and it names the rack this edit was for, not every broken rack on the server"
    )


# --- the name bound the wire restates --------------------------------------


def test_a_name_this_server_will_create_still_fits_in_a_detect_result(client_and_daemon):
    """Two constants that have to agree and cannot import each other.

    `ors_schema.link.MAX_SCREEN_NAME` bounds `PanelCandidate.claimed_by` -- the
    name of the screen already driving an SPI device -- and it is the *server's*
    `MAX_NAME` restated, because `ors-schema` may not import `ors-server`. Only a
    comment holds them together at either end. This suite may import both, so the
    assertion lives here, the way the daemon's suite pins `MAX_WATCHED_SCREENS`
    against `_MAX_TRACKED_SCREENS`.

    What the drift costs is silent: raise `MAX_NAME` to 128 and a screen created
    with a 100-character name becomes a claim no `DetectResult` can carry, so the
    whole detection reply fails to parse, the wait expires, and the wizard tells
    an operator the rack did not answer.

    The longest name this server will actually create is created here and then
    carried over the wire, rather than only compared: the equality is the claim,
    and a name a `POST /api/screens` accepts arriving in a `DetectResult` is the
    thing the equality is *for*.
    """
    client, daemon_id = client_and_daemon

    assert MAX_SCREEN_NAME == MAX_NAME

    longest = "x" * MAX_NAME
    created = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id, "name": longest})

    assert created.status_code == 201, "the server creates a name this long"

    claimed = parse_daemon_message(
        DetectResult(
            request_id="detect-1",
            panels=[PanelCandidate(bus=3, cs=1, claimed_by=longest)],
        ).model_dump_json()
    )

    assert claimed.panels[0].claimed_by == longest
