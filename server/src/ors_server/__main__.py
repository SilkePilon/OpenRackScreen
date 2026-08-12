from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ors_server.app import AppSettings, create_app
from ors_server.logging import setup_logging


def main() -> int:
    # First, before anything that can log: `create_app` reports a rebuilt
    # schema, and until this runs the root logger has no handler and every
    # record the server writes is discarded where nobody can see it.
    setup_logging(os.environ.get("ORS_LOG_LEVEL", "INFO"))
    settings = AppSettings(
        data_dir=Path(os.environ.get("ORS_DATA_DIR", "/var/lib/openrackscreen")),
        secret_key=os.environ.get("ORS_SECRET_KEY"),
    )
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("ORS_HOST", "0.0.0.0"),
        port=int(os.environ.get("ORS_PORT", "8080")),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
