from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ors_server import __version__
from ors_server.announce import Announcer, announcing_is_enabled
from ors_server.api.auth import router as auth_router
from ors_server.api.claims import (
    CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS,
    CLAIM_POLL_RATE_LIMIT_WINDOW_S,
    CLAIM_RATE_LIMIT_MAX_ATTEMPTS,
    CLAIM_RATE_LIMIT_WINDOW_S,
)
from ors_server.api.claims import public_router as claims_public_router
from ors_server.api.claims import router as claims_router
from ors_server.api.daemons import router as daemons_router
from ors_server.api.integrations import query_prometheus
from ors_server.api.integrations import router as integrations_router
from ors_server.api.screens import router as screens_router
from ors_server.api.settings import router as settings_router
from ors_server.api.templates import router as templates_router
from ors_server.auth import Sessions, require_session
from ors_server.db import Database
from ors_server.limiter import Limiter
from ors_server.link.hub import Hub
from ors_server.link.ws_daemon import router as daemon_socket_router
from ors_server.link.ws_ui import router as ui_socket_router
from ors_server.secrets import SecretStore, load_or_create_key
from ors_server.snapshot import seed_builtin_templates

log = logging.getLogger(__name__)


DEFAULT_PORT = 8080
"""What this server listens on when nothing says otherwise.

Here rather than in `__main__` because `AppSettings.port` needs a default and
two numbers that have to agree is one number written twice -- the announcement
would name 8080 while uvicorn bound something else, and the only symptom is a
rack that pairs against a port nothing answers on. `__main__.resolve_port` is
the one reader of `ORS_PORT`, and it falls back to this.
"""


@dataclass(frozen=True)
class AppSettings:
    """Everything the app needs from its environment, passed rather than read.

    A settings object rather than module-level `os.environ` reads because the
    tests build a dozen apps against a dozen temp directories, and a global
    would make them share one.
    """

    data_dir: Path
    secret_key: str | None = None
    port: int = DEFAULT_PORT
    """The port this process is listening on, for the mDNS announcement to name.

    Carried rather than read here because the app does not bind it -- `__main__`
    hands the same number to uvicorn and to this, so the announcement cannot
    name a port the server is not on. It defaults to the same 8080 `__main__`
    falls back to, which keeps every test that builds an app out of a temp
    directory unchanged; a deployment that moves the port and forgets this
    announces a wrong one, which is why `resolve_port` is the single place the
    environment is read.
    """
    web_dir: Path | None = None
    """Where the built interface is, or None for the API on its own.

    None rather than a default path because that is what almost every caller
    wants: the whole test suite builds apps that have no business serving an
    interface, and a developer running `vite dev` has the SPA on port 5173 with
    a proxy pointing here. `__main__` is what turns the environment into a path;
    a *missing* directory at that path is the ordinary developer state and is
    handled by `mount_interface`, not by refusing to start.
    """


RESERVED_PREFIXES = ("api", "ws")
"""The first path segment of everything this server answers for itself.

The mount at `/` matches every path there is, so "what belongs to the API" has
to be written down somewhere for the SPA fallback to decline; this is that
somewhere, and `test_spa.py` sweeps every route the app has to keep it true. It
is a prefix list and not a route list because the point is to cover the paths
that *do not* exist -- `/api/nope` is exactly the request that must not be
answered with a page.
"""

IMMUTABLE_DIRECTORY = "assets"
"""The one subdirectory of a build whose file names carry a content hash.

vite's `build.assetsDir`, its default. A file in here is addressed by its own
hash, so a changed file is a changed URL and the old one can never be wrong;
everything outside it -- `index.html`, and whatever `public/` held -- keeps its
name across builds and so may only be reused after revalidating.
"""

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
"""`no-cache` is "keep it, but ask first", not "do not keep it".

`StaticFiles` already sends an `etag`, so asking first costs a 304 with no body.
`no-store` would forbid keeping it at all and buy nothing: the failure being
prevented is a browser reusing the *previous deploy's* `index.html` and loading
a bundle whose hashed name is no longer there, which one revalidation settles.
"""


class SinglePageApp(StaticFiles):
    """A built interface, with client-side routes falling back to its document.

    `StaticFiles(html=True)` is **not** that, which is the one thing worth
    knowing here. Its HTML mode serves `index.html` for a request naming a
    *directory* and `404.html` for anything else -- so `/screens`, which is a
    route only the browser's router knows about and which is no directory on
    disk, comes back 404 and a reload of the page somebody is looking at loses
    the interface. Read from starlette 1.6.0's `staticfiles.py`, not assumed.

    So the fallback is here, and **HTML mode is off**, which is not merely
    redundant with it but actively defeats it: the `404.html` branch answers
    *before* raising, so a build that happens to contain a `404.html` -- one
    file somebody drops in `web/public/` -- turns `/screens` back into a 404 and
    the `except` below never runs. Measured, on a build with one. Off, HTML mode
    would have contributed only that and a 307 to `some-dir/` for a nested
    directory holding its own `index.html`, and a vite build has neither.

    The one namespace the fallback declines is `IMMUTABLE_DIRECTORY`: a missing
    file there stays a 404, because nothing in there is ever a client-side route
    -- it is vite's own directory of hashed files. A stale bundle URL, from a
    cached document or a half-copied deploy, otherwise comes back as an HTML
    page with a 200 that the browser reports as a JavaScript syntax error on
    line 1.

    What this class does *not* do is refuse the API's own paths. That is
    `InterfaceMount`'s job, one level up, and the difference is a real one --
    see its docstring.
    """

    def __init__(self, *, directory: Path, reserved: tuple[str, ...]) -> None:
        super().__init__(directory=directory)
        self.reserved = reserved

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as refusal:
            if refusal.status_code != 404 or self.is_a_hashed_asset(path):
                raise
            # The shell, under the URL the browser asked for: a redirect would
            # lose the path the client-side router is about to read.
            path = "index.html"
            response = await super().get_response(path, scope)
        response.headers["cache-control"] = self.cache_control(path)
        return response

    def belongs_to_the_api(self, path: str) -> bool:
        """Whether this path is one the server answers itself, and so is not ours.

        `path` is what `StaticFiles.get_path` made of the URL: relative, and
        with `.` and `..` already normalised away -- so `/x/../api/nope` is
        `api/nope` here and cannot sneak past a first-segment check. Taking the
        normalised path rather than the raw one is the whole reason this lives
        on the app and is *called* by the mount instead of living there.

        Case-folded, so `/API/nope` is refused too. Starlette's own routing is
        case-sensitive and `/API/health` was a 404 before this mount existed, so
        this is not about reaching a route -- it is about the reserved namespace
        meaning what it says, and about the mount changing nothing an API path
        used to do. The cost is that a page named `/API` or `/WS` could not
        exist, and there is none; a page named `/api-keys` is untouched, because
        this compares a whole first segment and not a prefix.
        """
        first = Path(path).parts[:1]
        return bool(first) and first[0].lower() in self.reserved

    def is_a_hashed_asset(self, path: str) -> bool:
        """Whether this path is vite's own directory, where every name is a hash.

        Two things turn on it and they are the same fact from both ends: a file
        in here may be kept forever because its name changes when it does, and a
        file in here that is missing is missing rather than a page, because a
        name that is a hash is never something a person typed.
        """
        return Path(path).parts[:1] == (IMMUTABLE_DIRECTORY,)

    def cache_control(self, path: str) -> str:
        """Read off the file being served, which is not always the URL asked for.

        Stated as exactly what it is worth: **today the two cannot disagree**,
        because the only paths that fall back to the shell are ones outside
        `assets/`, and those get `no-cache` under either reading -- a mutant
        passing the requested path here survives the whole suite, measured. It
        takes the served path anyway because the day the hashed-asset rule above
        is relaxed, the requested path answers `/assets/gone.js` with the shell
        under a year's caching, and that browser never sees this deploy.
        """
        return IMMUTABLE if self.is_a_hashed_asset(path) else REVALIDATE


class InterfaceMount(Mount):
    """The mount at `/`, declining what it has no business claiming.

    A plain `Mount` at `/` matches *every* path and *both* scope types, and
    starlette's router stops at the first full match -- so it takes two things
    that are not the interface's, both of which were measured against this app
    and neither of which any test would have noticed.

    **A websocket handshake to a path no `WebSocketRoute` claims.** `/nope` is
    not `/ws/ui`, so nothing matches it and the mount does -- and the first
    statement of `StaticFiles.__call__` is `assert scope["type"] == "http"`. An
    unauthenticated stranger gets one uncaught `AssertionError` and one uvicorn
    traceback per connection attempt. Declining the scope hands the handshake
    back to the router's own `not_found`, which closes it cleanly: exactly what
    the API answered before an interface was mounted under it.

    **Every wrong-method request to a route the API really has.** A method
    mismatch is a `Match.PARTIAL` in starlette, and a partial match only wins if
    nothing else claims the path -- so this mount's full match beat it and
    `POST /api/health` turned from 405 into 404, on every route in the server,
    silently. Declining the path leaves the partial match the only one, and the
    405 comes back.

    So the rule is one rule: *this mount claims HTTP requests for paths the
    server does not own*. It declines rather than answering, because "not mine"
    is what a catch-all under an existing app means -- answering would mean
    reimplementing the router's 404, its 405 and its websocket close here, and
    getting a different answer than the API alone gives is the entire defect
    above.
    """

    def __init__(self, interface: SinglePageApp) -> None:
        super().__init__("/", app=interface, name="interface")
        self.interface = interface

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http":
            return Match.NONE, {}
        # `get_path` is the same normalisation `StaticFiles.__call__` will do,
        # and the mount is at the root so the child scope's route path is this
        # one: what is tested here is what would have been served.
        if self.interface.belongs_to_the_api(self.interface.get_path(scope)):
            return Match.NONE, {}
        return super().matches(scope)


def mount_interface(app: FastAPI, directory: Path) -> None:
    """Put the built interface under everything the app already routes.

    Called last on purpose: starlette matches in registration order and this
    mount matches every path, so anything registered after it would be dead.

    A build that is not there is not an error. `pnpm build` has not been run on
    a fresh checkout, and it never will be on a machine where somebody is
    working on the server alone -- so this warns once, at startup, naming both
    the directory it looked in and the command that fills it, and leaves the API
    running. The alternative shapes are both worse: refusing to start turns a
    Python-only checkout into a build step, and staying silent turns "the image
    copied the interface to the wrong path" into a blank page with a clean log.
    """
    if not (directory / "index.html").is_file():
        log.warning(
            "no interface to serve: %s holds no index.html. "
            "Run `pnpm build` in web/, or point ORS_WEB_DIR at a build.",
            directory,
        )
        return
    # `app.router.routes.append` rather than `app.mount`, which is that line
    # with a plain `Mount` hard-coded; everything else about it is the same.
    app.router.routes.append(
        InterfaceMount(SinglePageApp(directory=directory, reserved=RESERVED_PREFIXES))
    )


async def validation_error_without_the_body(
    request: Request, exception: RequestValidationError
) -> JSONResponse:
    """422s, minus the `input` field FastAPI would otherwise quote back.

    A client that misspells `password` sends the password to the validator, and
    the default handler answers with the body it could not parse -- password and
    all. `loc`, `type` and `msg` say what was wrong without repeating it.
    """
    stripped = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in exception.errors()
    ]
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": stripped}))


@asynccontextmanager
async def announce_while_serving(app: FastAPI) -> AsyncIterator[None]:
    """Announce this server over mDNS for exactly as long as it is serving.

    Started here and not in `create_app` because "serving" is what the
    announcement claims: a process that built an app and never bound a port is
    a process no rack should be told to dial, and every test in this repository
    is that process.

    **Nothing it does may keep the server from starting.** `zeroconf` binds
    sockets and joins a multicast group, and there are ordinary hosts where
    that fails -- a container on a bridge network, a machine whose only
    interface is down. A server that refused to serve because it could not
    announce itself would be refusing over the one feature that has `--server
    URL` on the daemon as its documented alternative, so a failure is a warning
    naming what went wrong and a server that comes up anyway. The same applies
    on the way out, where the process is leaving regardless.
    """
    announcer = getattr(app.state, "announcer", None)
    if announcer is not None:
        try:
            announcer.start()
        except Exception:
            log.warning("could not announce this server over mDNS", exc_info=True)
    try:
        yield
    finally:
        if announcer is not None:
            try:
                announcer.stop()
            except Exception:
                log.warning("could not withdraw the mDNS announcement", exc_info=True)


def create_app(settings: AppSettings) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="OpenRackScreen",
        # Every framework route lives under /api, because the root belongs to the
        # SPA. Overriding docs and openapi alone leaves /redoc and
        # /docs/oauth2-redirect squatting there, and a browser asking for a page
        # the interface owns would get FastAPI's instead.
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url="/api/redoc",
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
        lifespan=announce_while_serving,
    )
    app.state.settings = settings
    # Built here and started by the lifespan above, or not built at all where
    # the deployment has said it wants no responder: a container on a bridge
    # network announces a port that is unreachable from the LAN it is
    # announcing onto, and would be better off silent. Constructing one opens
    # nothing -- `Announcer.start` is where `zeroconf` is first touched -- so
    # the whole test suite builds these and none of them transmits.
    app.state.announcer = (
        Announcer(port=settings.port, version=__version__) if announcing_is_enabled() else None
    )
    app.add_exception_handler(RequestValidationError, validation_error_without_the_body)

    database = Database(settings.data_dir / "ors.db")
    export = database.initialise()
    if export is not None:
        log.warning("schema changed; exported and rebuilt", extra={"export": str(export)})
    # Before anything can be pushed: `build_snapshot` refuses a screen naming a
    # template the snapshot does not carry, so on a server whose table is empty
    # the *whole* rack's first push fails rather than one panel.
    seed_builtin_templates(database)
    app.state.database = database
    app.state.sessions = Sessions()
    app.state.secrets = SecretStore(
        database, load_or_create_key(settings.data_dir, settings.secret_key)
    )
    app.state.hub = Hub()
    # A budget of its own, not `sessions`'s -- see `api/claims.py`'s
    # `CLAIM_RATE_LIMIT_MAX_ATTEMPTS` docstring for why sharing one counter
    # between an admin's login and an unauthenticated rack's claim filing
    # would be the wrong kind of "reuse".
    app.state.claim_limiter = Limiter(
        max_attempts=CLAIM_RATE_LIMIT_MAX_ATTEMPTS, window_seconds=CLAIM_RATE_LIMIT_WINDOW_S
    )
    # And a third, for the poll. Not the filing budget above: a daemon polls
    # while it waits and files again when its claim expires, so one shared
    # counter would let a patient rack spend the attempts it needs in order to
    # re-file. See `api/claims.py`'s `CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS`.
    app.state.claim_poll_limiter = Limiter(
        max_attempts=CLAIM_POLL_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=CLAIM_POLL_RATE_LIMIT_WINDOW_S,
    )
    # The one piece of outbound HTTP this server makes, held here rather than
    # imported at its call site so a test can replace it with a function --
    # which is the only way to exercise the Test button without binding a port.
    app.state.probe = query_prometheus

    # Two routers under one prefix, and the difference is the whole access
    # control model: `api` carries the session dependency, so a route added to
    # it is guarded without anyone remembering to say so, and `public` is the
    # short list of routes that answer without a session. `test_auth.py` pins
    # that list, so moving a route onto `public` has to be an argued decision
    # rather than a slip. `api` is on `app.state` because a router is what a
    # later task needs to hang its routes off, and reaching for the guarded one
    # should not mean reaching into this function.
    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
    app.state.api = api

    @public.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    public.include_router(auth_router)
    # Unauthenticated by necessity, not by omission: a rack that has not been
    # approved holds no credential to prove it should be allowed to ask. See
    # `api/claims.py`'s module docstring.
    public.include_router(claims_public_router)

    # The configuration API, all of it on the guarded router. Nothing here says
    # `Depends(require_session)`: `api` carries it, so a route added to any of
    # these five files is guarded without anybody remembering to say so, and
    # `test_auth.py`'s sweep is what fails if one is ever hung off `public`
    # instead.
    for configuration in (
        daemons_router,
        screens_router,
        templates_router,
        integrations_router,
        settings_router,
        claims_router,
    ):
        api.include_router(configuration)

    app.include_router(public)
    app.include_router(api)
    # And a third, at the root and on neither of the two above. The design puts
    # the daemon socket at `/ws/daemon`, which is not a path a prefix can be
    # bolted onto after the fact -- on `api` it would be `/api/ws/daemon`, which
    # is not what a daemon dials -- and it authenticates with a pairing token or
    # a daemon key rather than with the admin's session, so the guard `api`
    # carries would refuse every rack in the building.
    app.include_router(daemon_socket_router)
    # And a fourth, also at the root and for the same half of the reason: the
    # design puts the browser socket at `/ws/ui`, which `api` would turn into
    # `/api/ws/ui`. The other half is the opposite of the daemon socket's --
    # this one carries the session dependency on its own router, because there
    # is no credential in a first message to fall back on and everything it
    # streams belongs to the admin.
    app.include_router(ui_socket_router)
    # And the interface, at the root and below all four of them. One container,
    # one port, one origin: the session cookie needs no CORS and `/ws/ui` needs
    # no second host. It is last because a mount at `/` matches every path there
    # is -- the only thing keeping `/api/health` out of its hands is these lines
    # being above it.
    if settings.web_dir is not None:
        mount_interface(app, settings.web_dir)
    return app
