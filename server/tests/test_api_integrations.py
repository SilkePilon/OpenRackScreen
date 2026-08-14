from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from ors_server.api.integrations import ProbeError
from ors_server.app import AppSettings, create_app

CREDENTIAL = "hunter2-the-only-copy"
"""Long and distinctive, so a test looking for it in a response body cannot
match something else by accident and cannot miss it by matching a prefix."""

PROM = {"url": "http://prom.local:9090", "fields": {"cpu": {"query": "up"}}}


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


@pytest.fixture
def daemon_id(client: TestClient) -> int:
    return client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]


def create(client: TestClient, daemon_id: int, **overrides):
    body = {"daemon_id": daemon_id, "name": "prom", "config": PROM, **overrides}
    return client.post("/api/integrations", json=body)


def secrets_in(client: TestClient) -> list[str]:
    with closing(client.app.state.database.connect()) as connection:
        return [row["ciphertext"] for row in connection.execute("SELECT ciphertext FROM secret")]


def test_every_integration_route_refuses_an_unauthenticated_caller(tmp_path):
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/integrations").status_code == 401
    assert anonymous.get("/api/integrations/1").status_code == 401
    assert anonymous.post("/api/integrations", json={}).status_code == 401
    assert anonymous.patch("/api/integrations/1", json={}).status_code == 401
    assert anonymous.delete("/api/integrations/1").status_code == 401
    assert anonymous.post("/api/integrations/1/test").status_code == 401


# --- the ordinary shape -----------------------------------------------------


def test_an_integration_is_created_and_pushed_to_its_rack(client, daemon_id):
    rack = Rack(client, daemon_id)

    created = create(client, daemon_id)

    assert created.status_code == 201
    assert rack.pushes[-1]["snapshot"]["integrations"][0]["url"] == PROM["url"]


def test_the_columns_win_over_a_stale_copy_inside_the_config(client, daemon_id):
    """`snapshot._integrations` overlays name, type and poll_interval from the
    columns, so the API validates the same overlay -- otherwise a body could be
    accepted here and refused when the snapshot was assembled."""
    rack = Rack(client, daemon_id)

    create(client, daemon_id, name="real", poll_interval=11.0, config={**PROM, "name": "stale"})

    pushed = rack.pushes[-1]["snapshot"]["integrations"][0]
    assert pushed["name"] == "real"
    assert pushed["poll_interval"] == 11.0


def test_an_integration_with_no_fields_is_refused(client, daemon_id):
    """`PrometheusConfig` requires at least one: an integration that queries
    nothing polls a server for no reason and can feed no screen."""
    refused = create(client, daemon_id, config={"url": PROM["url"], "fields": {}})

    assert refused.status_code == 422
    assert client.get("/api/integrations").json() == []


def test_an_integration_for_a_daemon_that_does_not_exist_is_a_404(client):
    assert create(client, 4242).status_code == 404


def test_an_integration_can_be_renamed_and_disabled(client, daemon_id):
    created = create(client, daemon_id).json()

    patched = client.patch(
        f"/api/integrations/{created['id']}", json={"name": "prom-2", "enabled": False}
    )

    assert patched.json()["name"] == "prom-2"
    assert patched.json()["enabled"] is False


def test_disabling_an_integration_takes_it_out_of_the_snapshot(client, daemon_id):
    rack = Rack(client, daemon_id)
    created = create(client, daemon_id).json()

    client.patch(f"/api/integrations/{created['id']}", json={"enabled": False})

    assert rack.pushes[-1]["snapshot"]["integrations"] == []


def test_deleting_an_integration_pushes_the_rack_without_it(client, daemon_id):
    rack = Rack(client, daemon_id)
    created = create(client, daemon_id).json()

    assert client.delete(f"/api/integrations/{created['id']}").status_code == 200

    assert rack.pushes[-1]["snapshot"]["integrations"] == []


def test_an_integration_that_does_not_exist_is_a_404(client):
    assert client.get("/api/integrations/4242").status_code == 404
    assert client.patch("/api/integrations/4242", json={"name": "x"}).status_code == 404
    assert client.delete("/api/integrations/4242").status_code == 404
    assert client.post("/api/integrations/4242/test").status_code == 404


# --- the invariant: no credential in `integration.config` -------------------


def test_a_url_carrying_a_password_is_refused(client, daemon_id):
    """`integration.config` is exported verbatim on a schema bump, so this URL
    would land in a plaintext file beside the database."""
    refused = create(
        client, daemon_id, config={**PROM, "url": f"https://admin:{CREDENTIAL}@prom.local:9090"}
    )

    assert refused.status_code == 422
    assert "credential" in refused.json()["detail"]
    assert client.get("/api/integrations").json() == []


def test_a_url_carrying_only_a_username_is_refused_too(client, daemon_id):
    """`https://token@host` is how a bearer is smuggled into a URL, and the half
    that looks harmless is the half that carries it."""
    refused = create(client, daemon_id, config={**PROM, "url": "https://sometoken@prom.local"})

    assert refused.status_code == 422


def test_the_refusal_does_not_quote_the_credential_back(client, daemon_id):
    """FastAPI's own 422 handler echoes the body it could not parse, which is
    why `validation_error_without_the_body` exists. A refusal this module writes
    must not undo that."""
    refused = create(
        client, daemon_id, config={**PROM, "url": f"https://admin:{CREDENTIAL}@prom.local"}
    )

    assert CREDENTIAL not in refused.text


def test_a_credential_nested_deep_in_the_config_is_refused_as_well(client, daemon_id):
    """Walked rather than checked at `url`: `config` is a free JSON document,
    `tunnel` carries fields of its own, and M4's integrations will carry more."""
    refused = create(
        client,
        daemon_id,
        config={**PROM, "extra": {"nested": [f"https://admin:{CREDENTIAL}@prom.local"]}},
    )

    assert refused.status_code == 422
    assert "credential" in refused.json()["detail"], (
        "the walk must run before the model, or this is an unknown-field error instead"
    )


def test_a_query_containing_an_at_sign_is_not_mistaken_for_a_url(client, daemon_id):
    """PromQL has `@` in it -- `up @ 1609746000` is a legal modifier -- and a
    check that refused every string containing one would refuse real configs."""
    created = create(
        client, daemon_id, config={**PROM, "fields": {"cpu": {"query": "up @ 1609746000"}}}
    )

    assert created.status_code == 201


def test_a_url_carrying_only_a_password_is_refused_too(client, daemon_id):
    """The half of the check with no username in it at all.

    `https://:hunter2@prom.local` has a falsy `username` and a real `password`,
    so a check written as `parsed.username` alone accepts it -- and the whole
    plaintext lands in `integration.config` and in the next export. Named
    separately from the username half because a mutant dropping either one is
    invisible to a test that only sends `user:pass`.
    """
    refused = create(
        client, daemon_id, config={**PROM, "url": f"https://:{CREDENTIAL}@prom.local:9090"}
    )

    assert refused.status_code == 422
    assert "credential" in refused.json()["detail"]
    assert CREDENTIAL not in refused.text
    assert client.get("/api/integrations").json() == []


UNPARSEABLE = [
    # `urlsplit` raises `ValueError("Invalid IPv6 URL")` on a netloc holding an
    # unbalanced bracket, and every one of these carries a password through it.
    # Treating "the parser refused" as "not a URL" is what let the first three
    # store and export in plaintext.
    "https://admin:{secret}@[fd00::1:9090",
    "https://user:{secret}@[::1",
    "http://a:{secret}@[::1",
    "https://admin:{secret}@]fd00::1[:9090",
]


@pytest.mark.parametrize("template", UNPARSEABLE)
def test_a_url_the_parser_cannot_read_is_refused_rather_than_waved_through(
    client, daemon_id, template
):
    """A URL the parser cannot parse is not a URL that passed the check.

    Measured before this was refused: 201, the plaintext in `integration.config`,
    in `GET /api/integrations`, and verbatim in `Database.export()` -- the
    artefact that exists so a rack's configuration survives a schema bump.
    """
    refused = create(client, daemon_id, config={**PROM, "url": template.format(secret=CREDENTIAL)})

    assert refused.status_code == 422
    assert CREDENTIAL not in refused.text
    assert client.get("/api/integrations").json() == []
    assert CREDENTIAL not in json.dumps(client.app.state.database.export())


REFUSED = [
    # Every shape that carries userinfo, including the ones that look like they
    # do not. `urlsplit` strips ASCII tab and newline before it parses, lowercases
    # the scheme, and does not decode percent-escapes in userinfo -- so all four
    # of the middle cases are a credential wearing a disguise.
    "https://admin:{secret}@prom.local:9090",
    "https://{secret}@prom.local",
    "https://admin:@prom.local",
    "https://admin:{secret}@[fd00::1]:9090",
    "https://user%40example.com:{secret}@prom.local",
    "https://ad\tmin:{secret}@prom.local",
    "https://ad\nmin:{secret}@prom.local",
    "HTTPS://admin:{secret}@prom.local",
]

ALLOWED = [
    # And every shape that does not. An `@` is not evidence of anything on its
    # own: it is a PromQL modifier, and it is a legal path character.
    "http://prom.local:9090/a@b",
    "http://[fd00::1]:9090",
    "http://prom.local:9090",
]


@pytest.mark.parametrize("template", REFUSED)
def test_every_shape_that_carries_userinfo_is_refused(client, daemon_id, template):
    refused = create(client, daemon_id, config={**PROM, "url": template.format(secret=CREDENTIAL)})

    assert refused.status_code == 422
    assert CREDENTIAL not in refused.text


@pytest.mark.parametrize("url", ALLOWED)
def test_a_url_that_carries_no_credential_is_left_alone(client, daemon_id, url):
    """The other half, and the one a blunter check breaks: refusing every string
    with an `@` or a `[` in it would refuse the addresses people really type."""
    created = create(client, daemon_id, config={**PROM, "url": url})

    assert created.status_code == 201, created.text


def test_a_credential_becomes_an_encrypted_secret_row(client, daemon_id):
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False)

    assert created.status_code == 201
    assert created.json()["has_credential"] is True
    stored = secrets_in(client)
    assert len(stored) == 1
    assert CREDENTIAL not in stored[0], "the column holds ciphertext, not the password"
    assert client.app.state.secrets.get(1) == CREDENTIAL


def test_no_integration_route_ever_answers_with_the_credential(client, daemon_id):
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()

    bodies = [
        created,
        client.get("/api/integrations").json(),
        client.get(f"/api/integrations/{created['id']}").json(),
        client.patch(f"/api/integrations/{created['id']}", json={"name": "prom-2"}).json(),
    ]

    for body in bodies:
        assert CREDENTIAL not in json.dumps(body)


def test_the_export_redacts_the_stored_credential(client, daemon_id):
    create(client, daemon_id, credential=CREDENTIAL, enabled=False)

    exported = json.dumps(client.app.state.database.export())

    assert CREDENTIAL not in exported
    assert secrets_in(client)[0] not in exported, "the ciphertext is redacted as well"


def test_a_credential_on_an_enabled_integration_is_refused_with_the_reason(client, daemon_id):
    """No integration type can carry one on the wire in M3a, so an enabled row
    holding one blocks that rack's configuration entirely. Answered at the field
    rather than as a rolled-back edit naming a table."""
    refused = create(client, daemon_id, credential=CREDENTIAL, enabled=True)

    assert refused.status_code == 422
    assert "disabled" in refused.json()["detail"]
    assert secrets_in(client) == [], "and nothing was stored on the way to refusing"


def test_enabling_an_integration_that_holds_a_credential_is_refused_too(client, daemon_id):
    """The same rule from the other direction, which is the one a route could
    forget: the credential is not in this request at all."""
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()

    refused = client.patch(f"/api/integrations/{created['id']}", json={"enabled": True})

    assert refused.status_code == 422
    assert "disabled" in refused.json()["detail"], (
        "the answer must be about the credential, not a snapshot error naming a table"
    )
    assert client.get(f"/api/integrations/{created['id']}").json()["enabled"] is False


def test_replacing_a_credential_forgets_the_old_one(client, daemon_id):
    """A ciphertext nothing references is one in every export from now on, with
    nothing left that could decide to remove it."""
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()
    first = secrets_in(client)[0]

    client.patch(f"/api/integrations/{created['id']}", json={"credential": "a-different-one"})

    stored = secrets_in(client)
    assert len(stored) == 1
    assert stored[0] != first


def test_clearing_a_credential_removes_the_row_entirely(client, daemon_id):
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()

    patched = client.patch(f"/api/integrations/{created['id']}", json={"credential": ""})

    assert patched.json()["has_credential"] is False
    assert secrets_in(client) == []


def test_deleting_an_integration_forgets_its_credential(client, daemon_id):
    """`integration.secret_id` is `ON DELETE SET NULL`, which protects the
    integration from a deleted secret and does nothing in this direction."""
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()

    client.delete(f"/api/integrations/{created['id']}")

    assert secrets_in(client) == []


def test_a_refused_edit_leaves_no_ciphertext_behind(client, daemon_id):
    """The secret is written on the edit's own transaction, so a change that is
    then rolled back cannot leave an unreachable row."""
    created = create(client, daemon_id, credential=CREDENTIAL, enabled=False).json()

    client.patch(
        f"/api/integrations/{created['id']}",
        json={"credential": "another", "config": {**PROM, "fields": {}}},
    )

    assert len(secrets_in(client)) == 1, "the refused edit's new secret is not in the table"
    assert client.app.state.secrets.get(1) == CREDENTIAL


# --- the Test button --------------------------------------------------------


def test_test_reports_a_live_value_for_every_field(client, daemon_id):
    created = create(
        client,
        daemon_id,
        config={**PROM, "fields": {"cpu": {"query": "up"}, "mem": {"query": "mem"}}},
    ).json()
    client.app.state.probe = lambda url, query, timeout: [f"{query}=1"]

    report = client.post(f"/api/integrations/{created['id']}/test").json()

    assert report["ok"] is True
    assert {field["name"]: field["value"] for field in report["fields"]} == {
        "cpu": "up=1",
        "mem": "mem=1",
    }


def test_test_reports_the_field_that_failed_rather_than_failing(client, daemon_id):
    created = create(
        client,
        daemon_id,
        config={**PROM, "fields": {"good": {"query": "up"}, "bad": {"query": "boom"}}},
    ).json()

    def probe(url: str, query: str, timeout: float) -> list[str]:
        if query == "boom":
            raise ProbeError("Connection refused")
        return ["1"]

    client.app.state.probe = probe

    report = client.post(f"/api/integrations/{created['id']}/test").json()

    assert report["ok"] is False
    failed = next(field for field in report["fields"] if field["name"] == "bad")
    assert failed["error"] == "Connection refused"
    assert next(field for field in report["fields"] if field["name"] == "good")["ok"] is True


def test_a_query_that_matches_no_series_is_a_failure_with_a_reason(client, daemon_id):
    """The case this button exists for: a query that returns nothing is
    indistinguishable on the glass from a panel that has not finished
    connecting."""
    created = create(client, daemon_id).json()
    client.app.state.probe = lambda url, query, timeout: []

    report = client.post(f"/api/integrations/{created['id']}/test").json()

    assert report["ok"] is False
    assert report["fields"][0]["error"] == "the query matched no series"


def test_testing_an_integration_changes_nothing(client, daemon_id):
    """It writes no row, so it must not mint a version -- a Test button that
    repainted the rack would be a strange thing to press."""
    rack = Rack(client, daemon_id)
    created = create(client, daemon_id).json()
    client.app.state.probe = lambda url, query, timeout: ["1"]
    pushes = len(rack.pushes)

    client.post(f"/api/integrations/{created['id']}/test")

    assert len(rack.pushes) == pushes
