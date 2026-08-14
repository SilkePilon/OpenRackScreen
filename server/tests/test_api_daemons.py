from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_server.api.changes import MAX_EVENTS_PER_DAEMON, UNSERVABLE_HEADER, Change
from ors_server.app import AppSettings, create_app
from ors_server.pairing import authenticate_key, claim_token

DISPLAY = {"backend": "virtual", "out_dir": "/tmp/p"}


class Rack:
    """A daemon's end of the hub, with no daemon behind it.

    The hub holds a `send` callable and nothing else, so this is the whole of
    what a connected rack is as far as every route below is concerned. Running a
    real `/ws/daemon` conversation instead would make each assertion here depend
    on the other socket's hello, which has its own suite.

    Registered from the test thread rather than from the app's, which is safe
    only because no request is in flight while it happens: `TestClient` runs the
    app on a portal thread that is idle between calls.
    """

    def __init__(self, client: TestClient, daemon_id: int) -> None:
        self.sent: list[str] = []
        self.connection = client.app.state.hub.register(daemon_id, self.send)

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload if isinstance(payload, str) else payload.decode())

    def of_type(self, kind: str) -> list[dict]:
        return [message for message in map(json.loads, self.sent) if message["type"] == kind]


def build(tmp_path) -> TestClient:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    return client


@pytest.fixture
def client(tmp_path) -> TestClient:
    client = build(tmp_path)
    # A rack nobody in this file asks about, so that the daemon under test never
    # has row id 1. An id that coincides with a position, a count or a list
    # index hides exactly the mix-up these tests exist to catch, twice already
    # in this milestone.
    client.post("/api/daemons", json={"name": "spare-rack"})
    return client


@pytest.fixture
def daemon_id(client: TestClient) -> int:
    return client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]


def listed(client: TestClient, daemon_id: int) -> dict:
    return next(daemon for daemon in client.get("/api/daemons").json() if daemon["id"] == daemon_id)


def add_screen(client: TestClient, daemon_id: int, *, name: str = "CPU", position: int = 4) -> int:
    body = {
        "daemon_id": daemon_id,
        "name": name,
        "position": position,
        "display": DISPLAY,
        "template": "ring-gauge",
        "params": {},
    }
    return client.post("/api/screens", json=body).json()["id"]


def add_integration(
    client: TestClient, daemon_id: int, *, name: str = "prom", credential: str | None = None
):
    """An integration, disabled when it holds a credential -- which is the only
    kind M3a allows one on, see `_credential_has_somewhere_to_go`."""
    body = {
        "daemon_id": daemon_id,
        "name": name,
        "config": {"url": "http://prom.local:9090", "fields": {"cpu": {"query": "up"}}},
        "enabled": credential is None,
    }
    if credential is not None:
        body["credential"] = credential
    return client.post("/api/integrations", json=body).json()["id"]


def secrets_in(client: TestClient) -> list[str]:
    with closing(client.app.state.database.connect()) as connection:
        return [row["ciphertext"] for row in connection.execute("SELECT ciphertext FROM secret")]


def make_unservable(client: TestClient, daemon_id: int) -> None:
    """An enabled integration holding a credential -- see `test_api_screens`."""
    with closing(client.app.state.database.connect()) as connection:
        secret_id = connection.execute("INSERT INTO secret (ciphertext) VALUES ('x')").lastrowid
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, enabled)"
            " VALUES (?, 'prometheus', 'prom', '{}', ?, 1)",
            (daemon_id, secret_id),
        )


# --- the guard --------------------------------------------------------------


def test_every_daemon_route_refuses_an_unauthenticated_caller(tmp_path):
    """Named one by one as well as swept in `test_api_routes.py`.

    The sweep walks whatever routes exist, which is what covers the ones nobody
    has written yet; this is what says the list it swept was not empty.
    """
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/daemons").status_code == 401
    assert anonymous.post("/api/daemons", json={"name": "x"}).status_code == 401
    assert anonymous.post("/api/daemons/1/rotate-key").status_code == 401
    assert anonymous.post("/api/daemons/1/push").status_code == 401
    assert anonymous.post("/api/daemons/1/command", json={"command": "identify"}).status_code == 401
    assert anonymous.delete("/api/daemons/1").status_code == 401
    assert anonymous.get("/api/events").status_code == 401


def test_an_unauthenticated_caller_cannot_mint_a_token_by_being_refused(tmp_path):
    """The refusal must happen before the row is written, not after.

    A guard that ran after the handler would answer 401 and leave a paired rack
    minted by a stranger, which the status code says nothing about.
    """
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    anonymous.post("/api/daemons", json={"name": "smuggled"})

    with closing(anonymous.app.state.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daemon").fetchone()[0] == 0


# --- pairing, which happens in the browser and nowhere else -----------------


def test_creating_a_daemon_returns_its_token_exactly_once(client):
    created = client.post("/api/daemons", json={"name": "pi-rack"}).json()

    assert created["token"], "the token is shown once, at creation"
    listed = client.get("/api/daemons").json()
    assert all("token" not in daemon for daemon in listed), "and never again, from any route"


def test_the_token_that_was_shown_is_the_one_that_pairs_the_rack(client):
    """The whole point of the endpoint, and the only test that can prove it.

    A route that minted a row and returned a *different* random string would
    pass every assertion about not showing it twice.
    """
    created = client.post("/api/daemons", json={"name": "pi-rack"}).json()

    claimed = claim_token(client.app.state.database, created["token"])

    assert claimed is not None
    assert claimed[0] == created["id"]


def test_no_daemon_route_ever_answers_with_a_stored_credential(client, daemon_id):
    """A sweep, because the leak is a column reaching a response by accident.

    `SELECT *` plus a dict comprehension is how it happens, and the response
    model is what stops it -- so this asserts against the *stored* values rather
    than against a field name, which a rename would slip past.
    """
    client.post(f"/api/daemons/{daemon_id}/rotate-key")
    with closing(client.app.state.database.connect()) as connection:
        stored = connection.execute(
            "SELECT token_hash FROM daemon WHERE id = ?", (daemon_id,)
        ).fetchone()["token_hash"]

    bodies = [
        client.get("/api/daemons").text,
        client.get("/api/events").text,
        client.post(f"/api/daemons/{daemon_id}/push").text,
    ]

    assert stored, "the fixture proves nothing if the row holds no hash"
    for body in bodies:
        assert stored not in body


def test_a_daemon_reports_offline_until_it_connects(client, daemon_id):
    assert listed(client, daemon_id)["online"] is False


def test_a_daemon_the_hub_holds_a_socket_for_reports_online(client, daemon_id):
    Rack(client, daemon_id)

    assert listed(client, daemon_id)["online"] is True
    assert listed(client, 1)["online"] is False, "and one rack being up is not another's"


def test_a_creation_that_then_fails_leaves_no_rack_holding_an_unclaimable_token(
    client, monkeypatch
):
    """The row is written on the edit's own transaction, so a `change` that then
    raises undoes it.

    Written outside it, the failure leaves a `daemon` row holding a token nobody
    was ever shown -- and `daemon.name` is UNIQUE, so that name is occupied
    forever and every retry answers 409, with no event anywhere saying why. The
    injected failure stands for anything that can go wrong between the INSERT
    and the COMMIT; what is being asserted is the ordering, not the cause.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("the transaction failed after the row was written")

    monkeypatch.setattr(Change, "record", boom)

    with pytest.raises(RuntimeError):
        client.post("/api/daemons", json={"name": "half-made"})

    monkeypatch.undo()
    with closing(client.app.state.database.connect()) as connection:
        held = connection.execute(
            "SELECT COUNT(*) FROM daemon WHERE name = 'half-made'"
        ).fetchone()[0]
    assert held == 0
    assert client.post("/api/daemons", json={"name": "half-made"}).status_code == 201, (
        "and the name it would have occupied is free, so the retry is not a 409 forever"
    )


def test_a_second_daemon_by_the_same_name_is_a_conflict_not_a_traceback(client):
    client.post("/api/daemons", json={"name": "pi-rack"})

    again = client.post("/api/daemons", json={"name": "pi-rack"})

    assert again.status_code == 409


def test_a_daemon_needs_a_name(client):
    assert client.post("/api/daemons", json={"name": ""}).status_code == 422


# --- deleting a rack --------------------------------------------------------


def test_deleting_a_daemon_takes_its_screens_with_it(client, daemon_id):
    add_screen(client, daemon_id)

    client.delete(f"/api/daemons/{daemon_id}")

    assert client.get("/api/screens").json() == []


def test_deleting_a_daemon_leaves_another_racks_screens_alone(client, daemon_id):
    """The cascade is a foreign key with `ON DELETE CASCADE` *and* a pragma.

    `foreign_keys` is per-connection and off by default in SQLite, so the
    cascade is one `PRAGMA` away from silently not happening -- which shows up
    as orphaned screens rather than as an error. Two racks, because a delete
    that took every screen in the table would pass the test above.
    """
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    add_screen(client, daemon_id, name="CPU")
    kept = add_screen(client, other, name="MEM", position=7)

    client.delete(f"/api/daemons/{daemon_id}")

    assert [screen["id"] for screen in client.get("/api/screens").json()] == [kept]


def test_deleting_a_daemon_does_not_repaint_the_racks_that_are_left(client, daemon_id):
    """Deleting one rack changes no other rack's configuration.

    `affects_nobody` is what says so, and it has to be said: the default is
    every daemon, so without it every surviving rack is minted a version and
    pushed a snapshot identical to the one it is already running -- which the
    daemon cannot dedupe, because the number is new.
    """
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    other_rack = Rack(client, other)

    client.delete(f"/api/daemons/{daemon_id}")

    assert other_rack.of_type("config") == []
    assert listed(client, other)["config_version"] == 0


def test_deleting_a_daemon_forgets_the_credentials_of_its_integrations(client, daemon_id):
    """`secret` has no `daemon_id`, so no cascade reaches it.

    `screen`, `integration` and `daemon_event` all reference `daemon(id) ON
    DELETE CASCADE` and go; `integration.secret_id` is `ON DELETE SET NULL`,
    which protects the integration from a deleted secret and does nothing in
    this direction. Without this the ciphertext outlives the only row that ever
    referenced it -- unreachable through any route, undeletable, and in every
    export of the database from then on. `delete_integration` handles it; this
    is the same leak reached one level up.
    """
    add_integration(client, daemon_id, credential="hunter2-the-only-copy")
    assert secrets_in(client), "the fixture proves nothing without a ciphertext to orphan"

    client.delete(f"/api/daemons/{daemon_id}")

    assert secrets_in(client) == []


def test_deleting_a_daemon_whose_integration_holds_no_credential_is_not_an_error(client, daemon_id):
    """The commonest rack there is, and the one the credential sweep can break.

    `integration.secret_id` is nullable and most rows leave it null, so a sweep
    that collected every integration's `secret_id` rather than only the ones
    that are set hands `None` where a row id belongs -- and deleting an ordinary
    rack becomes a 500.
    """
    add_integration(client, daemon_id)

    deleted = client.delete(f"/api/daemons/{daemon_id}")

    assert deleted.status_code == 200
    assert client.get("/api/integrations").json() == []


def test_deleting_a_daemon_keeps_another_racks_credentials(client, daemon_id):
    """A delete that emptied the whole `secret` table would pass the test above."""
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    add_integration(client, daemon_id, credential="hunter2-the-only-copy")
    add_integration(client, other, name="theirs", credential="a-different-one")

    client.delete(f"/api/daemons/{daemon_id}")

    assert len(secrets_in(client)) == 1
    assert client.get("/api/integrations").json()[0]["has_credential"] is True


def test_deleting_a_daemon_is_recorded_against_no_rack_rather_than_not_at_all(client, daemon_id):
    """The most destructive action in the API is the one the status panel could
    not show. `daemon_event.daemon_id` is nullable and cascades, so the event has
    to be written against no rack -- against this one it would be deleted by the
    same statement that deletes the rack."""
    client.delete(f"/api/daemons/{daemon_id}")

    events = client.get("/api/events").json()
    deleted = [event for event in events if event["kind"] == "deleted"]
    assert len(deleted) == 1
    assert deleted[0]["daemon_id"] is None
    assert "pi-rack" in deleted[0]["message"]


def test_deleting_a_daemon_that_is_already_gone_is_a_404(client, daemon_id):
    client.delete(f"/api/daemons/{daemon_id}")

    assert client.delete(f"/api/daemons/{daemon_id}").status_code == 404


# --- rotating a leaked key --------------------------------------------------


def test_rotating_a_key_returns_a_new_token_and_unpairs(client, daemon_id):
    rotated = client.post(f"/api/daemons/{daemon_id}/rotate-key").json()

    assert rotated["token"]
    assert listed(client, daemon_id)["status"] == "unpaired"


def test_rotating_a_key_stops_the_leaked_one_authenticating(client, daemon_id):
    """What a rotate button has to mean. Until this endpoint existed, a key read
    off a Pi was that rack's identity for as long as the rack existed."""
    paired = claim_token(client.app.state.database, mint(client, "leaky"))
    assert paired is not None
    leaked_daemon, leaked_key = paired

    client.post(f"/api/daemons/{leaked_daemon}/rotate-key")

    assert authenticate_key(client.app.state.database, leaked_key) is None


def test_rotating_a_key_keeps_the_rack_it_belongs_to(client, daemon_id):
    """The reason this is an endpoint rather than "delete it and pair again".

    Deleting the row cascades its screens away, so the recovery from a leaked
    key would be re-entering the rack's whole configuration.
    """
    screen_id = add_screen(client, daemon_id)
    token = client.post(f"/api/daemons/{daemon_id}/rotate-key").json()["token"]

    claimed = claim_token(client.app.state.database, token)

    assert claimed is not None and claimed[0] == daemon_id
    assert [screen["id"] for screen in client.get("/api/screens").json()] == [screen_id]
    assert listed(client, daemon_id)["status"] == "paired"


def test_rotating_a_key_writes_a_token_onto_a_row_that_holds_a_key(client):
    """The schema's `CHECK (token_hash IS NULL OR key_hash IS NULL)` in one test.

    Writing the new token without clearing the key raises `IntegrityError`,
    which is exactly what the constraint is for -- so a rotate implemented as an
    UPDATE of one column is a 500, and one that cleared nothing would leave the
    leaked key working. This drives the case the constraint guards: a row that
    really does hold a key at the moment it is rotated.
    """
    paired = claim_token(client.app.state.database, mint(client, "paired-rack"))
    assert paired is not None

    rotated = client.post(f"/api/daemons/{paired[0]}/rotate-key")

    assert rotated.status_code == 200
    with closing(client.app.state.database.connect()) as connection:
        row = connection.execute(
            "SELECT token_hash, key_hash FROM daemon WHERE id = ?", (paired[0],)
        ).fetchone()
    assert row["key_hash"] is None
    assert row["token_hash"] is not None


def test_rotating_twice_leaves_only_the_second_token_working(client, daemon_id):
    first = client.post(f"/api/daemons/{daemon_id}/rotate-key").json()["token"]

    second = client.post(f"/api/daemons/{daemon_id}/rotate-key").json()["token"]

    assert first != second
    assert claim_token(client.app.state.database, first) is None
    assert claim_token(client.app.state.database, second) is not None


def test_rotating_a_key_leaves_every_other_rack_alone(client, daemon_id):
    """A rotation changes one rack's credentials and no rack's configuration.

    Declared, because the affected set defaults to every daemon: without the
    declaration this endpoint mints a version for every rack on the server and
    pushes each of them a snapshot, which is a teardown and a repaint of four
    panels apiece because somebody revoked a key somewhere else.
    """
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    other_rack = Rack(client, other)

    client.post(f"/api/daemons/{daemon_id}/rotate-key")

    assert other_rack.of_type("config") == []
    assert listed(client, other)["config_version"] == 0


def test_rotating_a_key_does_not_repaint_the_rack_it_belongs_to_either(client, daemon_id):
    """Its own rack included: the key changed, the configuration did not.

    A push here would be pointless as well as costly -- the daemon is about to
    be unable to authenticate, so the snapshot goes to a socket that is on its
    way out.
    """
    rack = Rack(client, daemon_id)

    client.post(f"/api/daemons/{daemon_id}/rotate-key")

    assert rack.of_type("config") == []
    assert listed(client, daemon_id)["config_version"] == 0


def test_rotating_a_key_for_a_daemon_that_does_not_exist_is_a_404(client):
    assert client.post("/api/daemons/4242/rotate-key").status_code == 404


# --- commands ---------------------------------------------------------------


def test_a_command_reaches_the_rack(client, daemon_id):
    rack = Rack(client, daemon_id)

    sent = client.post(f"/api/daemons/{daemon_id}/command", json={"command": "identify"})

    assert sent.status_code == 200
    assert sent.json() == {"delivered": True}
    assert rack.of_type("command") == [
        {"type": "command", "command": "identify", "screen_id": None}
    ]


def test_identify_carries_the_screen_it_is_meant_to_light_up(client, daemon_id):
    rack = Rack(client, daemon_id)
    screen_id = add_screen(client, daemon_id)

    client.post(
        f"/api/daemons/{daemon_id}/command",
        json={"command": "identify", "screen_id": screen_id},
    )

    assert rack.of_type("command")[-1]["screen_id"] == screen_id


def test_a_command_for_a_rack_that_is_not_connected_says_so(client, daemon_id):
    """The hub drops a send to an unconnected daemon in silence, which is right
    for a config push -- it is saved and pushed on reconnect -- and wrong for a
    button. Nothing about `identify` survives being deferred."""
    refused = client.post(f"/api/daemons/{daemon_id}/command", json={"command": "identify"})

    assert refused.status_code == 409


def test_a_command_naming_a_screen_that_is_a_flag_is_refused(client, daemon_id):
    """`bool` is an `int` in Python and pydantic's lax mode takes it as one.

    `Command` refuses `{"screen_id": true}` -- that is what `_not_a_flag` on the
    field is for -- but the request body is a model of its own, and it widened
    `true` to `1` before the message was ever built. So the validator saw an
    integer and passed it, and `sleep` put a dark circle on whichever panel holds
    row id 1: someone else's rack.
    """
    rack = Rack(client, daemon_id)

    refused = client.post(
        f"/api/daemons/{daemon_id}/command", json={"command": "identify", "screen_id": True}
    )

    assert refused.status_code == 422
    assert rack.of_type("command") == [], "and nothing reached the rack on the way to refusing"


def test_a_command_for_the_whole_rack_still_names_no_screen(client, daemon_id):
    """The other half: `None` is an absence, not a flag, and numbering every
    panel at once is what `identify` is usually for."""
    rack = Rack(client, daemon_id)

    sent = client.post(f"/api/daemons/{daemon_id}/command", json={"command": "identify"})

    assert sent.status_code == 200
    assert rack.of_type("command")[-1]["screen_id"] is None


def test_a_command_nobody_defined_is_refused(client, daemon_id):
    Rack(client, daemon_id)

    refused = client.post(f"/api/daemons/{daemon_id}/command", json={"command": "explode"})

    assert refused.status_code == 422


def test_a_command_for_a_daemon_that_does_not_exist_is_a_404(client):
    assert client.post("/api/daemons/4242/command", json={"command": "identify"}).status_code == 404


@pytest.mark.parametrize("command", ["sleep", "wake", "reload"])
def test_a_command_this_build_cannot_carry_out_says_so_instead_of_claiming_delivery(
    client, daemon_id, command
):
    """The route's own docstring reasons that answering 200 would be a button
    that lies, and then did -- by a different road.

    `identify`, `sleep`, `wake` and `reload` were all sent down a socket whose
    daemon had no handler for any of them, and all four were answered
    `{"delivered": true}`. `identify` is now wired. The other three have no
    mechanism behind them in M3a at all, and a 200 for one is the same lie the
    409 for an offline rack exists to avoid: 501 is what "this server does not
    implement that" is for, where 422 would blame the caller for a request that
    is perfectly well formed.
    """
    rack = Rack(client, daemon_id)

    refused = client.post(f"/api/daemons/{daemon_id}/command", json={"command": command})

    assert refused.status_code == 501
    assert rack.of_type("command") == [], "and nothing was put on the wire on the way to refusing"


@pytest.mark.parametrize(
    ("command", "instead"),
    [("sleep", "sleep_override"), ("wake", "sleep_override"), ("reload", "/push")],
)
def test_the_refusal_names_the_thing_that_does_work(client, daemon_id, command, instead):
    """A 501 that only says no leaves the reader looking for a button that is
    never coming. Each of the three has a real mechanism beside it."""
    refused = client.post(f"/api/daemons/{daemon_id}/command", json={"command": command})

    assert instead in refused.json()["detail"]


@pytest.mark.parametrize("command", ["sleep", "wake", "reload"])
def test_a_command_this_build_cannot_carry_out_is_refused_before_anything_is_read(client, command):
    """Whether this build implements `reload` has nothing to do with which rack
    it was aimed at, so the answer may not depend on one existing. Otherwise the
    same request answers 404 or 501 for reasons that are not about the command,
    and a client cannot tell which of the two facts it has learned."""
    assert client.post("/api/daemons/4242/command", json={"command": command}).status_code == 501


def test_identify_for_a_screen_on_another_rack_is_refused(client, daemon_id):
    """`Command.screen_id` is a row id in a table this rack does not own all of.

    `not_a_flag` stops `true` addressing row 1; nothing stopped an honest
    integer naming a screen that belongs to somebody else. The daemon ignores
    one it does not have -- which is the second lie in the same answer, because
    the route reported it delivered.
    """
    rack = Rack(client, daemon_id)
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    theirs = add_screen(client, other)

    refused = client.post(
        f"/api/daemons/{daemon_id}/command",
        json={"command": "identify", "screen_id": theirs},
    )

    assert refused.status_code == 404
    assert rack.of_type("command") == []


def test_identify_for_a_screen_that_does_not_exist_at_all_is_refused(client, daemon_id):
    rack = Rack(client, daemon_id)

    refused = client.post(
        f"/api/daemons/{daemon_id}/command",
        json={"command": "identify", "screen_id": 4242},
    )

    assert refused.status_code == 404
    assert rack.of_type("command") == []


def test_a_command_that_did_not_get_out_is_not_reported_as_delivered(client, daemon_id):
    """`is_online` is about the socket, not the send -- which is exactly how
    `POST /api/daemons/{id}/push` came to answer `delivered: true` with nothing
    on the wire. A rack that is TCP-alive and no longer reading has its send
    timed out and its socket dropped by the hub, and the button has to say so.
    """

    class Wedged(Rack):
        async def send(self, payload: str | bytes) -> None:
            raise ConnectionResetError("the Pi stopped reading")

    Wedged(client, daemon_id)

    sent = client.post(f"/api/daemons/{daemon_id}/command", json={"command": "identify"})

    assert sent.status_code == 200
    assert sent.json() == {"delivered": False}


def test_a_command_does_not_bump_the_configuration(client, daemon_id):
    """A command changes nothing on the server, so it must not mint a version.

    Bumping here would push the whole rack a fresh configuration -- a teardown
    and a repaint of every panel -- because somebody pressed identify.
    """
    Rack(client, daemon_id)
    before = listed(client, daemon_id)["config_version"]

    client.post(f"/api/daemons/{daemon_id}/command", json={"command": "identify"})

    assert listed(client, daemon_id)["config_version"] == before


# --- push configuration now -------------------------------------------------


def test_pushing_now_sends_the_whole_snapshot(client, daemon_id):
    rack = Rack(client, daemon_id)
    add_screen(client, daemon_id, name="CPU")
    rack.sent.clear()

    assert client.post(f"/api/daemons/{daemon_id}/push").status_code == 200

    pushed = rack.of_type("config")
    assert len(pushed) == 1
    assert [screen["name"] for screen in pushed[0]["snapshot"]["screens"]] == ["CPU"]


def test_pushing_now_mints_a_new_version_because_the_daemon_dedupes_on_it(client, daemon_id):
    """The reason this button is a *bump* and not a re-send of the same number.

    `ors_daemon.link._config` answers a push whose version equals the one it
    believes it is running with an ack and no apply. So a daemon claiming a
    version it does not really have -- the blank rack this endpoint exists for --
    would answer the re-send exactly as it answered the skip, and the panels
    would stay dark.
    """
    rack = Rack(client, daemon_id)
    before = listed(client, daemon_id)["config_version"]

    client.post(f"/api/daemons/{daemon_id}/push")

    assert listed(client, daemon_id)["config_version"] > before
    assert rack.of_type("config")[-1]["version"] > before


def test_the_version_a_push_reports_is_the_version_that_was_sent(client, daemon_id):
    """The number in the response is what the interface shows and what anybody
    debugging a blank rack compares against the daemon's log. A response that
    reported the old version -- or nought -- would say the push was the one the
    daemon has already refused to apply."""
    rack = Rack(client, daemon_id)

    reported = client.post(f"/api/daemons/{daemon_id}/push").json()["version"]

    assert reported == rack.of_type("config")[-1]["version"]
    assert reported == listed(client, daemon_id)["config_version"]


def test_pushing_to_an_offline_rack_is_saved_for_its_next_connect(client, daemon_id):
    """Not an error: the bump is what makes the next connect push, because the
    daemon's claimed version can no longer match."""
    pushed = client.post(f"/api/daemons/{daemon_id}/push")

    assert pushed.status_code == 200
    assert pushed.json()["delivered"] is False
    assert listed(client, daemon_id)["config_version"] == 1


def test_pushing_now_to_a_rack_that_cannot_be_given_a_configuration_admits_it(client, daemon_id):
    """The one button that exists to dig a blank rack out of a stuck state must
    not claim success when nothing left the server.

    A rack whose committed configuration is unservable has no snapshot to send,
    so `_assemble` records it and appends no push. `delivered` asked the hub
    whether a socket was open -- which it is, and which is not the question --
    and answered `{"version": 2, "delivered": true}` with zero messages on that
    socket. Measured.
    """
    rack = Rack(client, daemon_id)
    make_unservable(client, daemon_id)
    rack.sent.clear()

    pushed = client.post(f"/api/daemons/{daemon_id}/push")

    assert pushed.json()["delivered"] is False
    assert rack.of_type("config") == [], "and nothing was sent, which is what it now says"
    assert pushed.status_code == 202
    assert pushed.headers[UNSERVABLE_HEADER] == str(daemon_id)


def test_pushing_now_to_a_connected_rack_reports_the_send_that_happened(client, daemon_id):
    """The other half: `delivered` is still true when a snapshot really went."""
    rack = Rack(client, daemon_id)

    pushed = client.post(f"/api/daemons/{daemon_id}/push")

    assert pushed.status_code == 200
    assert pushed.json()["delivered"] is True
    assert len(rack.of_type("config")) == 1


def test_pushing_to_a_daemon_that_does_not_exist_is_a_404(client):
    assert client.post("/api/daemons/4242/push").status_code == 404


def test_pushing_now_leaves_every_other_rack_alone(client, daemon_id):
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    other_rack = Rack(client, other)

    client.post(f"/api/daemons/{daemon_id}/push")

    assert other_rack.of_type("config") == []
    assert listed(client, other)["config_version"] == 0


# --- the events behind the status panel -------------------------------------


def test_what_the_api_did_to_a_rack_is_recorded_against_it(client, daemon_id):
    client.post(f"/api/daemons/{daemon_id}/rotate-key")

    kinds = [event["kind"] for event in client.get("/api/events").json()]

    assert "rotate-key" in kinds


def test_events_can_be_read_for_one_rack(client, daemon_id):
    other = client.post("/api/daemons", json={"name": "other-rack"}).json()["id"]
    client.post(f"/api/daemons/{other}/rotate-key")

    mine = client.get("/api/events", params={"daemon_id": daemon_id}).json()

    assert all(event["daemon_id"] == daemon_id for event in mine)
    assert any(event["daemon_id"] == other for event in client.get("/api/events").json())


def test_the_event_log_is_a_ring_buffer_rather_than_a_leak(client, daemon_id):
    """One row per action forever is a table nobody prunes on a rack that
    reconnects all day. The cap is per daemon, so a chatty rack cannot push
    another rack's history out."""
    for _ in range(MAX_EVENTS_PER_DAEMON + 5):
        client.post(f"/api/daemons/{daemon_id}/push")

    with closing(client.app.state.database.connect()) as connection:
        kept = connection.execute(
            "SELECT COUNT(*) FROM daemon_event WHERE daemon_id = ?", (daemon_id,)
        ).fetchone()[0]

    assert kept == MAX_EVENTS_PER_DAEMON


def test_the_newest_events_are_the_ones_kept(client, daemon_id):
    for _ in range(MAX_EVENTS_PER_DAEMON + 2):
        client.post(f"/api/daemons/{daemon_id}/push")

    events = client.get("/api/events", params={"daemon_id": daemon_id}).json()

    assert events[0]["id"] > events[-1]["id"], "newest first, which is what a panel shows"
    with closing(client.app.state.database.connect()) as connection:
        oldest = connection.execute(
            "SELECT MIN(id) FROM daemon_event WHERE daemon_id = ?", (daemon_id,)
        ).fetchone()[0]
    assert oldest > 1, "the first events written are the ones that went"


def mint(client: TestClient, name: str) -> str:
    return client.post("/api/daemons", json={"name": name}).json()["token"]
