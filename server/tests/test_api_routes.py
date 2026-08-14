"""The rules that hold across the whole configuration API, swept rather than named.

Two of them, and both are rules the *next* route someone writes has to obey:

- every route under `/api` is `async def`, because `Hub` is event-loop-affine,
- every mutating route goes through `changes.change`, because that is what bumps
  the version and pushes the snapshot.

Each sweep is followed by a test that plants a violation and watches the sweep
find it, so a sweep that stopped looking fails rather than passing quietly.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server import api
from ors_server.app import AppSettings, create_app
from starlette.routing import WebSocketRoute

API_PACKAGE = Path(api.__file__).parent

MAY_RUN_IN_A_THREADPOOL = {
    "/api/health",
    # argon2 is 0.58s by design, and a hash on the event loop stops every frame
    # the server is relaying for that long. None of these four touches the hub,
    # which is the only reason a threadpool is safe for them -- so this is the
    # list, and anything not on it is a route that has to be `async def`.
    "/api/auth/me",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}

MUTATES_NOTHING = {
    # A POST that changes no row, so there is nothing to bump and nothing to
    # push. `command` sends `identify` or `sleep` down an open socket; `test`
    # dry-runs an integration's queries and answers with what came back.
    "POST /api/daemons/{daemon_id}/command",
    "POST /api/integrations/{integration_id}/test",
}


def routes(app: FastAPI) -> list[tuple[str, object]]:
    """Every route the app will match. `test_auth.py` explains why not `app.routes`."""
    return [
        (context.path or context.original_route.path, context.original_route)
        for context in iter_route_contexts(app.routes)
    ]


def threadpool_routes(app: FastAPI) -> set[str]:
    return {
        path
        for path, route in routes(app)
        if path.startswith("/api")
        and path not in MAY_RUN_IN_A_THREADPOOL
        and not isinstance(route, WebSocketRoute)
        and not inspect.iscoroutinefunction(getattr(route, "endpoint", None))
    }


def unmanaged_mutations(app: FastAPI) -> set[str]:
    """`METHOD /path` for every writing route that does not open a `change`."""
    found = set()
    for path, route in routes(app):
        if not path.startswith("/api") or isinstance(route, WebSocketRoute):
            continue
        if path in MAY_RUN_IN_A_THREADPOOL:
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("GET", "HEAD", "OPTIONS"):
                continue
            if f"{method} {path}" in MUTATES_NOTHING:
                continue
            if "async with change(" not in inspect.getsource(route.endpoint):
                found.add(f"{method} {path}")
    return found


def test_every_api_route_is_async_because_the_hub_is_event_loop_affine(tmp_path):
    """`Hub` may only be touched from the loop, and FastAPI runs a `def` route in
    a threadpool. The read-only-looking routes are the dangerous ones: the
    natural shape of `GET /api/daemons` is a blocking `sqlite3` read plus
    `hub.is_online`, and a set built from a dict a reconnect is resizing raises
    `RuntimeError: Set changed size during iteration` -- which is neither a
    `WebSocketDisconnect` nor a `ValidationError`, so it escapes the daemon
    socket's handler and takes a whole rack offline rather than failing one
    request."""
    app = create_app(AppSettings(data_dir=tmp_path))

    assert threadpool_routes(app) == set()


def test_the_async_sweep_sees_a_route_that_would_run_in_a_threadpool(tmp_path):
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.state.api.get("/blocking")
    def blocking() -> dict[str, bool]:
        return {"ok": True}

    assert threadpool_routes(app) == {"/api/blocking"}


def test_the_threadpool_exemptions_all_exist(tmp_path):
    """A list of paths is a list that rots. Every exemption must be a real route,
    or it is an exemption covering a route somebody renamed."""
    app = create_app(AppSettings(data_dir=tmp_path))

    assert MAY_RUN_IN_A_THREADPOOL <= {path for path, _ in routes(app)}


def test_a_route_that_reads_the_hub_really_runs_on_the_event_loop(tmp_path):
    """The sweep above is about the declaration; this is about the effect.

    `asyncio.get_running_loop()` succeeds only on the thread running the loop,
    so a hub whose methods ask the question answers whether the route that
    called it was in a threadpool. Behavioural, so a future FastAPI that decided
    threadpool-or-not differently would still be caught.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    hub = app.state.hub
    app.state.hub = _WatchfulHub(hub)
    client = TestClient(app)
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    client.post("/api/daemons", json={"name": "pi-rack"})

    client.get("/api/daemons")

    assert app.state.hub.asked, "the route did not touch the hub, so this proves nothing"
    assert app.state.hub.off_the_loop == [], "a hub call from a threadpool"


def test_the_loop_check_would_notice_a_threadpool(tmp_path):
    """The other half: a `def` route reading the same hub is caught."""
    app = create_app(AppSettings(data_dir=tmp_path))
    app.state.hub = _WatchfulHub(app.state.hub)

    @app.get("/api/blocking")
    def blocking(request: Request) -> dict[str, bool]:
        return {"online": request.app.state.hub.is_online(1)}

    client = TestClient(app)
    client.get("/api/blocking")

    assert app.state.hub.off_the_loop == ["is_online"]


def test_every_mutating_route_goes_through_the_bump_and_push(tmp_path):
    """The rule that must not be rememberable. A POST that writes a row without
    opening a `change` saves the edit, answers 200, and leaves the rack showing
    what it showed before -- with nothing anywhere saying so."""
    app = create_app(AppSettings(data_dir=tmp_path))

    assert unmanaged_mutations(app) == set()


def test_the_mutation_sweep_sees_a_route_that_writes_on_its_own(tmp_path):
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.state.api.post("/rogue")
    async def rogue() -> dict[str, bool]:
        return {"ok": True}

    assert unmanaged_mutations(app) == {"POST /api/rogue"}


@pytest.mark.parametrize("module", sorted(path.name for path in API_PACKAGE.glob("*.py")))
def test_no_router_bumps_or_pushes_on_its_own(module):
    """One mechanism, in one file. A router that reached for `push_config` or
    `bump_config_version` directly would be a second implementation of the
    ordering `changes` exists to own -- assemble before commit, commit before
    push -- and the two would disagree the first time one of them was edited."""
    if module in ("changes.py", "__init__.py"):
        return

    # Parsed rather than grepped: every one of these files *discusses* the rule
    # in its docstrings, and a substring search would be a test that passes
    # only while nobody explains anything.
    reached_for = _names_used((API_PACKAGE / module).read_text())

    assert "push_config" not in reached_for
    assert "bump_config_version" not in reached_for
    assert "bump_config_version_on" not in reached_for


def _names_used(source: str) -> set[str]:
    """Every attribute and bare name the code really refers to. Comments excluded."""
    used: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name)
    return used


def test_the_name_sweep_reads_code_rather_than_prose():
    """It has to see a call and ignore a docstring, or it is asserting nothing."""
    assert "push_config" in _names_used("hub.push_config(1, push)")
    assert "push_config" not in _names_used('"""Never calls hub.push_config."""')


class _WatchfulHub:
    """A hub that records whether each call reached it on the event loop."""

    def __init__(self, hub) -> None:  # noqa: ANN001 - the real one
        self._hub = hub
        self.asked: list[str] = []
        self.off_the_loop: list[str] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._hub, name)
        if not callable(attribute):
            return attribute

        def watched(*args, **kwargs):
            self.asked.append(name)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No running loop on this thread, which is what a threadpool
                # worker looks like from the inside.
                self.off_the_loop.append(name)
            return attribute(*args, **kwargs)

        return watched


def test_the_watchful_hub_is_not_fooled_by_a_thread_that_has_a_loop():
    """The probe asks about *this* thread, which is the whole question."""
    hub = _WatchfulHub(_Nothing())
    seen: list[list[str]] = []

    def elsewhere() -> None:
        hub.is_online(1)
        seen.append(list(hub.off_the_loop))

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()

    assert seen == [["is_online"]]


class _Nothing:
    def is_online(self, daemon_id: int) -> bool:
        return False
