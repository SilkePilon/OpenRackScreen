from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ors_server.app import AppSettings, create_app


def main() -> int:
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
