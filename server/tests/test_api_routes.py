"""The rules that hold across the whole configuration API, swept rather than named.

Two of them, and both are rules the *next* route someone writes has to obey:

- every route under `/api` is `async def`, because `Hub` is event-loop-affine,
- every mutating route goes through `changes.change`, because that is what bumps
  the version and pushes the snapshot.

Each sweep is followed by a test that plants a violation and watches the sweep
find it, so a sweep that stopped looking fails rather than passing quietly. A
planted violation has to be a real one: a route containing no write at all
proves the sweep is live and nothing more, and "live" was never the question.

Every sweep here parses. None of them greps, and the two are not
interchangeable: all five router files *discuss* these rules in their prose, so
a substring search over their source is a check each of them passes by talking
about it. A route was planted with `async with change(` in its docstring and a
bare `INSERT` in its body, and the substring form returned `set()`.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_server import api
from ors_server.api.changes import change
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
    # The two questions the add-screen wizard asks a rack. Both read the `daemon`
    # row to answer 404 for a rack nobody created, and then ask the Pi: `detect`
    # lists its SPI devices, `probe` lights one panel with wiring an operator
    # typed. Neither writes anything -- what they are about is hardware this
    # server has no row for yet, which is the whole reason they exist.
    "POST /api/daemons/{daemon_id}/detect",
    "POST /api/daemons/{daemon_id}/probe",
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


WRITING_STATEMENTS = ("insert", "update", "delete", "replace", "create", "drop", "alter")
"""The SQL verbs that change a row. `BEGIN`, `COMMIT` and `ROLLBACK` are not
here: they are `changes.change`'s own, and it is the thing being enforced."""


def unmanaged_mutations(app: FastAPI) -> set[str]:
    """`METHOD /path` for every writing route that does not open a `change`.

    Two questions per route, because there are two places the rule can be broken.
    The endpoint has to open a `change` -- unless it is on `MUTATES_NOTHING`,
    which is then held to writing nothing at all, so an exemption cannot quietly
    become a licence. And nothing the route hangs off `Depends(...)` may write,
    because a dependency runs *before* the endpoint's transaction is open and on
    a connection of its own: the row would be committed, unbumped and unpushed,
    and rolling the endpoint back would not take it with it.

    **What this still cannot see**, stated so nobody reads it as more than it is.
    `writes` reads one function's own source, so a `MUTATES_NOTHING` route or a
    dependency that wrote through a helper it *called* would pass -- the primary
    rule does not depend on that, because "the endpoint must open a `change`" is
    unconditional and needs no notion of writing at all. Nor does it follow a
    route into a background task, or into a thread it hands work to. Neither is
    a shape anything here takes; both would need a call graph rather than a file.
    """
    found = set()
    for path, route in routes(app):
        if not path.startswith("/api") or isinstance(route, WebSocketRoute):
            continue
        if path in MAY_RUN_IN_A_THREADPOOL:
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("GET", "HEAD", "OPTIONS"):
                continue
            name = f"{method} {path}"
            if name in MUTATES_NOTHING:
                if writes(route.endpoint):
                    found.add(name)
            elif not opens_a_change(route.endpoint):
                found.add(name)
            if any(writes(call) for call in dependency_calls(route)):
                found.add(name)
    return found


def opens_a_change(function: object) -> bool:
    """Whether this function really opens `async with change(...)`.

    Parsed rather than grepped, for the reason the name sweep below is: every
    one of these files *discusses* the rule in its docstrings, and a substring
    search over the source is a test that passes for any route that mentions it.
    A route planted with `async with change(` in its docstring and a bare
    `connection.execute("INSERT ...")` in its body was swept clean, measured.
    """
    for node in ast.walk(_tree(function)):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and _called(call.func) == "change":
                return True
    return False


def writes(function: object) -> bool:
    """Whether this function executes a statement that changes a row.

    Read off the statement rather than off the method name, because `execute` is
    how everything reaches SQLite here, reads included. A statement this cannot
    read statically -- one built by concatenation, or passed in from elsewhere --
    counts as a write: the point of the sweep is that a route cannot arrange to
    be believed.
    """
    for node in ast.walk(_tree(function)):
        if not isinstance(node, ast.Call):
            continue
        if _called(node.func) not in ("execute", "executemany", "executescript"):
            continue
        if not node.args:
            return True
        statement = _leading_text(node.args[0])
        if statement is None:
            return True
        head = statement.strip().split(None, 1)
        if head and head[0].lower() in WRITING_STATEMENTS:
            return True
    return False


def dependency_calls(route: object) -> list[object]:
    """Every callable the route hangs off `Depends(...)`, at any depth."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return []
    found: list[object] = []
    pending = list(dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is not None:
            found.append(dependency.call)
        pending.extend(dependency.dependencies)
    return found


def _tree(function: object) -> ast.AST:
    """The function's source as an AST. A source that cannot be read is a
    failure rather than a pass, because a sweep that quietly skips what it
    cannot see is a sweep that reports `set()` forever."""
    return ast.parse(textwrap.dedent(inspect.getsource(function)))


def _called(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _leading_text(node: ast.expr) -> str | None:
    """The statically known start of a string expression, or None.

    An f-string counts -- `f"UPDATE screen SET {assignments} WHERE id = ?"` is a
    write whoever reads it can see -- and its leading literal is what names the
    verb. Anything else is unreadable, and unreadable is not innocent.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _leading_text(node.values[0])
    return None


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
    """The planted violation really writes a row.

    It has to: a route containing no `execute` at all proves the sweep is live
    and nothing more, and "live" was never the question -- the question is
    whether it catches a route that saves an edit no rack is told about.
    `request.app.state.database.connect()` is writable and every router holds
    one, which is exactly why this rule cannot be enforced by a type.
    """
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.state.api.post("/rogue")
    async def rogue(request: Request) -> dict[str, bool]:
        with closing(request.app.state.database.connect()) as connection:
            connection.execute("INSERT INTO setting (key, value) VALUES ('rogue', '1')")
        return {"ok": True}

    assert unmanaged_mutations(app) == {"POST /api/rogue"}


def test_the_mutation_sweep_is_not_fooled_by_a_route_that_explains_the_rule(tmp_path):
    """The five router files all discuss `async with change(` in their prose, so
    a substring search over the source is a sweep every one of them satisfies by
    talking. This route mentions it and does not do it, and was swept clean by
    the substring form -- measured, `set()`."""
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.state.api.post("/sneaky")
    async def sneaky(request: Request) -> dict[str, bool]:
        """Every write in this module goes through `async with change(` -- except
        this one, which is the point."""
        with closing(request.app.state.database.connect()) as connection:
            connection.execute("UPDATE setting SET value = '1' WHERE key = 'sneaky'")
        return {"ok": True}

    assert unmanaged_mutations(app) == {"POST /api/sneaky"}


def test_the_mutation_sweep_sees_a_write_hidden_in_a_dependency(tmp_path):
    """A `Depends(...)` runs before the endpoint's transaction is open and on a
    connection of its own, so a write there is committed, unbumped and unpushed
    -- and rolling the endpoint back does not take it with it. Only
    `route.endpoint` was inspected, so it was invisible."""
    app = create_app(AppSettings(data_dir=tmp_path))

    def stamp(request: Request) -> None:
        with closing(request.app.state.database.connect()) as connection:
            connection.execute("UPDATE setting SET value = '1' WHERE key = 'stamped'")

    @app.state.api.post("/stamped")
    async def stamped(
        # `= Depends(stamp)` rather than `Annotated[..., Depends(stamp)]`,
        # because this module has `from __future__ import annotations` and
        # FastAPI resolves an annotation against the *module* namespace -- a
        # dependency named by a local would silently not be one, and the sweep
        # would be watched finding nothing rather than finding this.
        request: Request,
        response: Response,
        _: None = Depends(stamp),
    ) -> dict[str, bool]:
        async with change(request, response) as edit:
            edit.affects_nobody()
        return {"ok": True}

    assert unmanaged_mutations(app) == {"POST /api/stamped"}


def test_the_routes_exempted_from_the_change_rule_really_write_nothing(tmp_path):
    """`MUTATES_NOTHING` is a list, and a list is a thing that rots. An exemption
    is from opening a `change`, not a licence to write outside one -- so the
    routes on it are held to the stronger half of the rule instead."""
    app = create_app(AppSettings(data_dir=tmp_path))
    exempted = [
        route
        for path, route in routes(app)
        if any(f"{method} {path}" in MUTATES_NOTHING for method in getattr(route, "methods", ()))
    ]

    assert len(exempted) == len(MUTATES_NOTHING), "an exemption naming a route nobody has written"
    assert [route for route in exempted if writes(route.endpoint)] == []


def test_the_write_check_reads_the_statement_rather_than_the_method(tmp_path):
    """`execute` is how every read reaches SQLite too, so the verb is what
    decides -- and a statement it cannot read statically is not given the benefit
    of the doubt."""

    def reads(connection) -> None:  # noqa: ANN001 - a stand-in
        connection.execute("SELECT * FROM daemon ORDER BY id")

    def writes_a_row(connection) -> None:  # noqa: ANN001
        connection.execute("DELETE FROM screen WHERE id = ?", (1,))

    def writes_through_an_f_string(connection, columns) -> None:  # noqa: ANN001
        connection.execute(f"UPDATE screen SET {columns} WHERE id = ?", (1,))

    def writes_through_a_name(connection, statement) -> None:  # noqa: ANN001
        connection.execute(statement, (1,))

    assert writes(reads) is False
    assert writes(writes_a_row) is True
    assert writes(writes_through_an_f_string) is True
    assert writes(writes_through_a_name) is True, "unreadable is not innocent"


def test_the_change_check_reads_code_rather_than_prose():
    """The other half of the substring problem, at the function it is asked of."""

    async def opens(request, response) -> None:  # noqa: ANN001
        async with change(request, response) as edit:
            edit.affects_nobody()

    async def only_talks_about_it(request, response) -> None:  # noqa: ANN001
        """Writes are made under `async with change(request, response)`."""
        return None

    assert opens_a_change(opens) is True
    assert opens_a_change(only_talks_about_it) is False


def router_modules(package: Path) -> list[str]:
    """Every module in the router package, subpackages included.

    A function so the recursion is testable. It was `glob("*.py")`, the top
    level only, so an `api/` subpackage -- the obvious shape the moment one of
    these files needs splitting -- would have been swept by nothing at all, with
    the sweep below still green because it iterated the files that were left.
    """
    return sorted(str(path.relative_to(package)) for path in package.rglob("*.py"))


ROUTER_MODULES = router_modules(API_PACKAGE)

NOT_A_ROUTER = ("changes.py", "__init__.py")
"""`changes.py` is the mechanism being enforced, and `__init__.py` is empty."""


@pytest.mark.parametrize("module", ROUTER_MODULES)
def test_no_router_bumps_or_pushes_on_its_own(module):
    """One mechanism, in one file. A router that reached for `push_config` or
    `bump_config_version` directly would be a second implementation of the
    ordering `changes` exists to own -- assemble before commit, commit before
    push -- and the two would disagree the first time one of them was edited."""
    if Path(module).name in NOT_A_ROUTER:
        return

    # Parsed rather than grepped: every one of these files *discusses* the rule
    # in its docstrings, and a substring search would be a test that passes
    # only while nobody explains anything.
    source = (API_PACKAGE / module).read_text()
    reached_for = _names_used(source)

    assert "push_config" not in reached_for
    assert "bump_config_version" not in reached_for
    assert "bump_config_version_on" not in reached_for
    # And the one thing a name sweep cannot see through. `getattr(hub, "push_" +
    # "config")` refers to no name this reads, so the sweep above is only worth
    # anything while nothing in these files looks an attribute up dynamically.
    # No router has a reason to, so the tool is what is forbidden rather than
    # the strings it might be handed.
    assert "getattr" not in reached_for, "a dynamic attribute lookup makes the sweep above blind"


def test_the_routers_swept_are_the_routers_there_are():
    """A parametrised sweep over an empty list is a green test that ran nothing."""
    assert set(ROUTER_MODULES) >= {"daemons.py", "screens.py", "templates.py", "settings.py"}


def test_a_router_in_a_subpackage_is_swept_too(tmp_path):
    """The sweep above is over the files that exist today, so it stays green
    whether or not it recurses. This is the half that says it does -- and the
    subpackage is the shape the day one of these files is split in two."""
    (tmp_path / "extra").mkdir()
    (tmp_path / "top.py").write_text("")
    (tmp_path / "extra" / "nested.py").write_text("")

    assert router_modules(tmp_path) == ["extra/nested.py", "top.py"]


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


def test_the_name_sweep_is_blind_to_a_dynamic_lookup_which_is_why_getattr_is_banned():
    """Stated as a test rather than as a comment, because it is the reason the
    assertion above exists and the thing that makes it load-bearing."""
    assert "push_config" not in _names_used('getattr(hub, "push_" + "config")(1, push)')
    assert "getattr" in _names_used('getattr(hub, "push_" + "config")(1, push)')


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
