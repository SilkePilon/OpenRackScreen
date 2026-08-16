"""The two halves on one origin: the API's routes, and the interface under them.

Everything here is about a *precedence*, which is why almost every test in this
file asserts two things at once -- that the SPA answered where it should, and
that it did not answer where the API already does. A mount at `/` matches every
path there is, so the only thing keeping `/api/health` out of its hands is that
it is registered last; a test that only looked at `/screens` would pass just as
happily against a server that had swallowed the whole API.

The interface directory is a fixture built here rather than `web/dist`, for two
reasons. The suite must pass on a checkout where nobody has run `pnpm build`
(that is the ordinary developer state, and the state task 18's image is the only
thing that changes). And a real build's file names carry vite's content hashes,
which move on every rebuild -- a test naming one would be a test that fails on
somebody else's machine.

**No two strings in the fixture are the same.** The shell, the bundle, the
stylesheet and the file planted *outside* the build all carry different marker
text, so "the SPA answered" cannot be confused with "the asset answered" and a
traversal that escaped could not be mistaken for either.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server.app import RESERVED_PREFIXES, AppSettings, create_app
from starlette.routing import Mount
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

SHELL = '<!doctype html><title>OpenRackScreen</title><div id="root">the shell</div>'
BUNDLE = 'console.log("the bundle, which is not the shell")'
STYLESHEET = ":root { --the-stylesheet: 1 }"
OUTSIDE = "the file one directory above the build, which no request may reach"
NOT_FOUND_PAGE = "<!doctype html><title>404</title>the page a public/ directory left behind"

# vite's own layout: hashed file names under `assets/`, and the entry document
# at the top. The hash here is invented -- what matters is that it *is* one, so
# the caching rule below has something whose name changes with its content.
BUNDLE_NAME = "index-B7f3a91c.js"
STYLESHEET_NAME = "index-Dq2e0f5b.css"

PASSWORD = "nine amber lanterns"


@pytest.fixture
def built(tmp_path) -> Path:
    """A directory shaped like `web/dist`, with a file planted just outside it."""
    directory = tmp_path / "web" / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(SHELL)
    (directory / "assets" / BUNDLE_NAME).write_text(BUNDLE)
    (directory / "assets" / STYLESHEET_NAME).write_text(STYLESHEET)
    (directory / "favicon.svg").write_text("<svg />")
    (directory.parent / "secret.txt").write_text(OUTSIDE)
    return directory


@pytest.fixture
def client(tmp_path, built) -> TestClient:
    """The server as it ships: the API, the sockets, and the interface under them."""
    return TestClient(create_app(AppSettings(data_dir=tmp_path / "data", web_dir=built)))


def signed_in(client: TestClient) -> None:
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def warnings_about_the_build(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "pnpm build" in record.getMessage()
    ]


def test_the_api_still_wins_over_the_spa(client):
    """/api/health must not be swallowed by the catch-all.

    Both halves, because either alone is satisfied by a server that does not do
    the job: a mount registered *before* the routers answers `/api/health` with
    the shell, and a mount that was never registered at all leaves `/` a 404
    while `/api/health` looks perfect.
    """
    health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.headers["content-type"].startswith("application/json")
    assert client.get("/").text == SHELL, "the SPA is not mounted, so this proved nothing"


def test_a_client_side_route_survives_a_reload(client):
    """GET /screens returns index.html, not 404.

    `/screens` is a route the browser's router owns and the server has never
    heard of. Typing it, bookmarking it, or pressing reload on it is an ordinary
    HTTP GET, and the only way the interface is still there afterwards is if the
    server answers with the shell and lets the bundle sort out the rest.
    """
    reloaded = client.get("/screens")

    assert reloaded.status_code == 200
    assert reloaded.text == SHELL
    assert reloaded.headers["content-type"].startswith("text/html")


def test_a_deep_client_side_route_survives_a_reload_too(client):
    """One segment is the easy case: `/screens` could be answered by a directory
    that happened to exist. Two is the shape a detail page takes, and nothing on
    disk is ever going to look like it."""
    assert client.get("/screens/7/edit").text == SHELL


def test_an_unknown_api_path_is_still_404_rather_than_the_spa(client):
    """Otherwise a typo in a fetch returns HTML and fails as a JSON parse error.

    Which is the worst possible way to learn about it: the status says 200, the
    body says `<!doctype html>`, and the exception names the parser rather than
    the URL. The 404 has to be JSON too, because that is what every other
    refusal on this server is and what the interface's error handling reads.
    """
    missing = client.get("/api/nope")

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
    assert SHELL not in missing.text


def test_an_unknown_api_path_under_a_real_router_is_404_too(client):
    """The near miss, which is the one somebody actually types. `/api/screens`
    exists, so a typo in the segment after it is a path the SPA would otherwise
    be delighted to answer."""
    assert client.get("/api/screens/1/nope").status_code == 404
    assert client.post("/api/daemosn", json={"name": "pi-rack"}).status_code == 404


def test_the_wrong_method_on_a_real_api_route_is_still_405_and_not_404(client, tmp_path):
    """The contract the mount quietly rewrote across the *whole* API until it
    declined instead of answering.

    A method mismatch is a `Match.PARTIAL` in starlette and a partial match only
    wins if nothing claims the path outright -- so a mount at `/` beat it, and
    every wrong-method request to a route that really exists came back 404
    "there is no such thing" instead of 405 "not like that". Asserted against
    the same app without an interface, because the number alone is only half the
    claim: what is being pinned is that mounting the interface changed *nothing*
    about what the API answers.

    Both directions, because one of them proves nothing on its own: `StaticFiles`
    answers *every* non-GET with a 405 of its own, so a mount that swallowed
    `POST /api/health` would still hand back 405 by coincidence. `GET
    /api/auth/login` is the mismatch the other way round -- a route that exists
    for POST, asked for with a verb `StaticFiles` is delighted to serve -- and a
    mount that answered it would return the shell with a 200.
    """
    alone = TestClient(create_app(AppSettings(data_dir=tmp_path / "data-alone")))

    for method, path in (("POST", "/api/health"), ("GET", "/api/auth/login")):
        mounted = client.request(method, path)

        assert mounted.status_code == 405, f"{method} {path}"
        assert mounted.status_code == alone.request(method, path).status_code
        assert mounted.json() == {"detail": "Method Not Allowed"}
        assert SHELL not in mounted.text


def test_a_reserved_path_is_refused_after_normalisation_not_by_its_first_letters(client):
    """`/x/../api/nope` is `api/nope` by the time anything is served.

    Which is why the mount asks `StaticFiles.get_path` what the path *is* before
    deciding it is not its own, rather than reading the segment off the URL: a
    raw first-segment check sees `x`, hands the request to the interface, and
    the interface serves the shell for an API path.
    """
    assert client.get("/x/%2e%2e/api/nope").status_code == 404
    assert SHELL not in client.get("/x/%2e%2e/api/nope").text


def test_a_page_whose_name_merely_starts_with_a_reserved_word_is_still_a_page(client):
    """`api` and `ws` are whole first segments, not prefixes.

    `/api-keys` is a perfectly ordinary name for a settings page, and a
    `startswith` check turns a reload of it into 404 JSON. `/wsl` is the same
    trap one letter along.
    """
    assert client.get("/api-keys").text == SHELL
    assert client.get("/apidocs").text == SHELL
    assert client.get("/wsl").text == SHELL


def test_a_reserved_path_in_the_wrong_case_is_not_a_page_either(client):
    """`/API/nope` reaches no route -- starlette's routing is case-sensitive, so
    it was a 404 before the interface existed -- and it must not become a 200
    page now. Nothing emits it; the point is that the reserved namespace covers
    what it says it covers, and that the mount changed nothing here."""
    refused = client.get("/API/nope")

    assert refused.status_code == 404
    assert refused.json() == {"detail": "Not Found"}


def test_a_handshake_to_a_path_no_socket_owns_closes_rather_than_tracebacking(client, tmp_path):
    """A `Mount` matches a websocket scope, and `StaticFiles` asserts it does not.

    `/nope` is no `WebSocketRoute`, so the mount is the only thing that claims
    it, and the first statement of `StaticFiles.__call__` is `assert
    scope["type"] == "http"` -- one uncaught `AssertionError` and one uvicorn
    traceback per attempt, from any unauthenticated stranger, forever. Pinned
    against the same app with no interface, because "closes cleanly" is not the
    interesting half: the interesting half is that it closes *the same way*.
    """
    alone = TestClient(create_app(AppSettings(data_dir=tmp_path / "data-alone")))

    with pytest.raises(WebSocketDisconnect) as mounted:
        with client.websocket_connect("/nope"):
            pass  # pragma: no cover -- the connection never opens
    with pytest.raises(WebSocketDisconnect) as unmounted:
        with alone.websocket_connect("/nope"):
            pass  # pragma: no cover -- the connection never opens

    assert mounted.value.code == unmounted.value.code == 1000


def test_a_socket_path_is_not_answered_with_the_spa_either(client):
    """A plain GET to `/ws/ui` matches no route -- a `WebSocketRoute` only
    matches a websocket scope -- so it reaches the mount exactly as `/api/nope`
    does. Handing it the shell would tell a client whose upgrade header got
    stripped by a proxy that everything is fine."""
    answered = client.get("/ws/ui")

    assert answered.status_code == 404
    assert SHELL not in answered.text


def test_the_sockets_are_not_shadowed(client):
    """Both of them, and they fail differently if the mount ever moves in front.

    A `Mount` matches a websocket scope as readily as an HTTP one, so a mount
    registered before these routers does not 404 the sockets -- it hands the
    handshake to `StaticFiles`, which asserts the scope is HTTP and dies inside
    the connect. That mechanism is not hypothetical and it is not only about
    ordering: it fired on *every* unrouted path until the mount learned to
    decline a websocket, which is
    `test_a_handshake_to_a_path_no_socket_owns_closes_rather_than_tracebacking`.
    """
    signed_in(client)

    with client.websocket_connect("/ws/ui") as socket:
        assert json.loads(socket.receive_text()) == {"type": "daemons", "online": []}

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(
            '{"type": "hello", "token": "", "hostname": "pi-rack", "daemon_version": "0.1.0"}'
        )
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 4401, "the daemon socket's own refusal, not the mount's"


def test_a_missing_build_is_a_clear_error_rather_than_a_500(tmp_path, caplog):
    """Running the server without `pnpm build` is the ordinary developer state.

    So it is not a startup failure and not an exception per request: the API
    comes up, the pages 404 the way a path with nothing behind it should, and
    the log says once what is missing and what to run.
    """
    caplog.set_level(logging.WARNING)
    absent = tmp_path / "web" / "dist"

    app = create_app(AppSettings(data_dir=tmp_path / "data", web_dir=absent))
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200
    refused = client.get("/screens")
    assert refused.status_code == 404, "not a 500, and not a RuntimeError at startup"
    assert refused.json() == {"detail": "Not Found"}
    assert warnings_about_the_build(caplog) == [
        f"no interface to serve: {absent} holds no index.html. "
        "Run `pnpm build` in web/, or point ORS_WEB_DIR at a build."
    ]


def test_the_warning_about_a_missing_build_is_logged_once_and_not_per_request(tmp_path, caplog):
    """A line per request is a line nobody reads, and on a server that is polled
    by a healthcheck every thirty seconds it is also the whole log."""
    caplog.set_level(logging.WARNING)
    absent = tmp_path / "web" / "dist"
    client = TestClient(create_app(AppSettings(data_dir=tmp_path / "data", web_dir=absent)))

    for _ in range(3):
        client.get("/screens")
    client.get("/")

    assert len(warnings_about_the_build(caplog)) == 1


def test_a_build_directory_without_an_index_is_a_missing_build_too(tmp_path, caplog):
    """An interrupted `pnpm build` leaves the directory and not the document.

    Checking that the directory exists would mount it, and then `/` would 404
    with nothing anywhere saying why -- while `StaticFiles` happily served
    whatever half-written asset was in there.
    """
    caplog.set_level(logging.WARNING)
    empty = tmp_path / "web" / "dist"
    empty.mkdir(parents=True)

    client = TestClient(create_app(AppSettings(data_dir=tmp_path / "data", web_dir=empty)))

    assert client.get("/").status_code == 404
    assert len(warnings_about_the_build(caplog)) == 1


def test_no_interface_configured_is_the_api_alone_and_says_nothing(tmp_path, caplog):
    """`web_dir=None` is what every other test file in this suite builds, and it
    has to stay silent: a warning per app would put one in front of every
    assertion in the suite, and a warning that is always there is a warning
    nobody sees when it is about them."""
    caplog.set_level(logging.WARNING)

    client = TestClient(create_app(AppSettings(data_dir=tmp_path / "data")))

    assert client.get("/api/health").status_code == 200
    assert client.get("/screens").status_code == 404
    assert warnings_about_the_build(caplog) == []


def test_the_assets_themselves_are_served(client):
    """The shell is one file of a build and the least interesting one. A mount
    that answered every path with `index.html` -- which is what a catch-all
    written without a file lookup does -- would pass every test above and serve
    a page whose script tag returns HTML."""
    bundle = client.get(f"/assets/{BUNDLE_NAME}")
    stylesheet = client.get(f"/assets/{STYLESHEET_NAME}")

    assert bundle.text == BUNDLE
    assert bundle.headers["content-type"].startswith("text/javascript")
    assert stylesheet.text == STYLESHEET
    assert stylesheet.headers["content-type"].startswith("text/css")


def test_a_missing_asset_is_not_answered_with_the_shell(client):
    """The one place the SPA fallback is actively wrong. A bundle whose hash has
    moved -- an old tab, a stale cache, a half-copied deploy -- must 404, not be
    handed an HTML document with a 200 that the browser then tries to parse as
    JavaScript and reports as a syntax error on line 1."""
    stale = client.get("/assets/index-0000000.js")

    assert stale.status_code == 404
    assert SHELL not in stale.text


def test_a_404_page_in_the_build_does_not_defeat_the_fallback(tmp_path, built):
    """The one file that would turn every deep link back into a 404, silently.

    `StaticFiles(html=True)` answers with `404.html` *before* it raises, so the
    fallback's `except` never runs -- and `404.html` is not an exotic thing to
    find in a build, it is whatever somebody dropped in `web/public/`. Measured:
    with HTML mode on and this file present, `GET /screens` was 404 and the 404
    page. HTML mode is off, and this is what says so.
    """
    (built / "404.html").write_text(NOT_FOUND_PAGE)
    client = TestClient(create_app(AppSettings(data_dir=tmp_path / "data", web_dir=built)))

    reloaded = client.get("/screens")

    assert reloaded.status_code == 200
    assert reloaded.text == SHELL
    assert NOT_FOUND_PAGE not in reloaded.text


def test_an_interface_that_is_there_says_nothing_at_all(client, caplog):
    """The other half of the missing-build warning, and the reason it is worth
    anything: a log line per served page is a log line nobody reads, and this
    mount answers every request a browser makes. Not `pnpm build` warnings --
    *any* warning, because a chatty fallback is the same defect wherever the
    line is written."""
    caplog.set_level(logging.WARNING)

    client.get("/")
    client.get("/screens")
    client.get(f"/assets/{BUNDLE_NAME}")
    client.get("/api/nope")

    said = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]

    assert said == []


@pytest.mark.parametrize(
    "attempt",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/....//secret.txt",
    ],
)
def test_a_traversal_cannot_reach_outside_the_build(client, attempt):
    """The file is planted one directory above the build, which is where
    `web/dist`'s neighbour actually is -- and in the image, where the venv is.

    A traversal is not an error to answer: the path is simply not a file in the
    build, so it comes back as the shell like any other unknown path. What must
    never come back is the file.
    """
    answered = client.request("GET", attempt)

    assert OUTSIDE not in answered.text
    assert answered.status_code in (200, 404)


def test_the_shell_is_not_cached_but_a_hashed_asset_is(client):
    """The asymmetry the file names already encode.

    `index.html` is fetched by name and rewritten by every build, so a browser
    allowed to reuse it runs the *previous* deploy's bundle -- and the cure is
    telling every operator to hard-refresh, once per upgrade, forever. The
    assets under `assets/` carry a content hash in the name, so a changed file
    is a changed URL and the old one can be kept until the heat death.

    `no-cache` rather than `no-store`: it may be kept, it must be revalidated,
    and the `etag` `StaticFiles` already sends makes that a 304 rather than a
    download.
    """
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/screens").headers["cache-control"] == "no-cache"
    assert (
        client.get(f"/assets/{BUNDLE_NAME}").headers["cache-control"]
        == "public, max-age=31536000, immutable"
    )


def test_an_unhashed_file_beside_the_shell_is_not_cached_forever(client):
    """`favicon.svg` is copied from `public/` under its own name, so it is the
    same trap as the shell: cached for a year, it survives a rebrand."""
    assert client.get("/favicon.svg").headers["cache-control"] == "no-cache"


def test_the_shell_is_served_without_a_session_because_the_login_page_is_in_it(client):
    """Deliberate, and worth saying out loud next to `test_auth.py`'s sweep.

    The sweep guards `/api` and the sockets; the mount is under neither, and it
    must not be. The document served here is the same static bundle for every
    visitor and carries no row from this rack's database -- and the page a
    visitor without a session needs is the login form, which is inside it. A
    guard here would be a login page you have to log in to reach.
    """
    assert "ors_session" not in client.cookies
    assert client.get("/").status_code == 200
    assert client.get("/screens").status_code == 200
    assert client.get("/api/screens").status_code == 401, "and the data behind it is still shut"


def test_the_mount_is_registered_after_every_route_it_could_shadow(client, tmp_path, built):
    """Behavioural tests above cover what the ordering buys; this says what it is.

    Starlette matches in registration order and a mount at `/` matches
    everything, so every route on this app has to be in front of it. Stated as
    "the mount is last" rather than "there is one at the root", because a second
    router included after it would be dead and nothing else would notice.
    """
    app = create_app(AppSettings(data_dir=tmp_path / "data-2", web_dir=built))

    mounts = [route for route in app.routes if isinstance(route, Mount)]

    assert len(mounts) == 1
    assert mounts[0].path == ""
    assert app.routes[-1] is mounts[0], "anything after the mount is unreachable"


def test_every_route_this_server_owns_is_under_a_reserved_prefix(tmp_path, built):
    """What keeps `RESERVED_PREFIXES` from rotting.

    The fallback refuses to answer a path whose first segment is one of these,
    which is the only reason `/api/nope` is a 404 rather than the shell. A route
    added at `/metrics` tomorrow would be shadowed by the SPA the moment it
    404s, and this is what fails instead.
    """
    app = create_app(AppSettings(data_dir=tmp_path / "data", web_dir=built))

    owned = {
        (context.path or context.original_route.path).split("/")[1]
        for context in iter_route_contexts(app.routes)
        if not isinstance(context.original_route, Mount)
    }

    assert owned <= set(RESERVED_PREFIXES), f"a route the SPA would shadow: {sorted(owned)}"
    assert owned == {"api", "ws"}, "the sweep proves nothing if it swept nothing"


def test_the_app_is_still_an_app_without_an_interface(tmp_path):
    """`create_app` returns the app whether or not there is a build, and nothing
    about the interface may be able to stop the server coming up."""
    assert isinstance(create_app(AppSettings(data_dir=tmp_path, web_dir=tmp_path / "no")), FastAPI)


def test_a_socket_is_still_refused_without_a_session_with_the_spa_mounted(client):
    """The other side of the public shell: mounting a document everyone may read
    changes nothing about the socket that streams every panel in the rack."""
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/ws/ui"):
        pass  # pragma: no cover -- the connection never opens

    assert denied.value.status_code == 401
