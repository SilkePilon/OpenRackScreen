from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ors_server import __version__
from ors_server.api.auth import router as auth_router
from ors_server.api.daemons import router as daemons_router
from ors_server.api.integrations import query_prometheus
from ors_server.api.integrations import router as integrations_router
from ors_server.api.screens import router as screens_router
from ors_server.api.settings import router as settings_router
from ors_server.api.templates import router as templates_router
from ors_server.auth import Sessions, require_session
from ors_server.db import Database
from ors_server.link.hub import Hub
from ors_server.link.ws_daemon import router as daemon_socket_router
from ors_server.link.ws_ui import router as ui_socket_router
from ors_server.secrets import SecretStore, load_or_create_key
from ors_server.snapshot import seed_builtin_templates

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppSettings:
    """Everything the app needs from its environment, passed rather than read.

    A settings object rather than module-level `os.environ` reads because the
    tests build a dozen apps against a dozen temp directories, and a global
    would make them share one.
    """

    data_dir: Path
    secret_key: str | None = None
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

    So the fallback is here, and there are exactly two namespaces it declines.

    A path under one of `RESERVED_PREFIXES` is the API's, and it is refused
    before the disk is even looked at: answering it with a page turns a typo'd
    fetch into a JSON parse error pointing at the parser, and refusing it *here*
    rather than in the `except` below is what keeps `POST /api/daemosn` a 404
    rather than the 405 `StaticFiles` answers every non-GET with.

    And a missing file under `IMMUTABLE_DIRECTORY` stays a 404, because nothing
    in there is ever a client-side route -- it is vite's own directory of hashed
    files. A stale bundle URL, from a cached document or a half-copied deploy,
    otherwise comes back as an HTML page with a 200 that the browser reports as
    a JavaScript syntax error on line 1.
    """

    def __init__(self, *, directory: Path, reserved: tuple[str, ...]) -> None:
        super().__init__(directory=directory, html=True)
        self.reserved = reserved

    async def get_response(self, path: str, scope: Scope) -> Response:
        if self.belongs_to_the_api(path):
            raise HTTPException(status_code=404)
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
        """Whether this path is one the server answers itself, and so must 404.

        `path` is what `StaticFiles.get_path` made of the URL: relative, and
        with `.` and `..` already normalised away -- so `/api/../api/nope` is
        `api/nope` here and cannot sneak past a first-segment check.
        """
        first = Path(path).parts[:1]
        return bool(first) and first[0] in self.reserved

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
    app.mount("/", SinglePageApp(directory=directory, reserved=RESERVED_PREFIXES), name="interface")


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
    )
    app.state.settings = settings
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
