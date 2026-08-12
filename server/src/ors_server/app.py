from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, FastAPI

from ors_server import __version__


@dataclass(frozen=True)
class AppSettings:
    """Everything the app needs from its environment, passed rather than read.

    A settings object rather than module-level `os.environ` reads because the
    tests build a dozen apps against a dozen temp directories, and a global
    would make them share one.
    """

    data_dir: Path
    secret_key: str | None = None


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

    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api)
    return app
