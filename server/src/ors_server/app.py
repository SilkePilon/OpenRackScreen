from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ors_server import __version__
from ors_server.api.auth import router as auth_router
from ors_server.auth import Sessions, require_session
from ors_server.db import Database
from ors_server.secrets import SecretStore, load_or_create_key

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
    app.state.database = database
    app.state.sessions = Sessions()
    app.state.secrets = SecretStore(
        database, load_or_create_key(settings.data_dir, settings.secret_key)
    )

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

    app.include_router(public)
    app.include_router(api)
    return app
