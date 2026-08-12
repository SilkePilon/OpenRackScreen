# Core M3a — Server and Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A server that owns the rack's configuration in SQLite and pushes it to a paired daemon over a WebSocket, so the YAML file stops being the source of truth — with frames flowing back on demand, ready for the interface.

**Architecture:** FastAPI + SQLite in one process. The wire format is the `DaemonConfig` model the daemon already loads from YAML, assembled from database rows, so the daemon's apply path and its file path converge on one validated object. The daemon gains a reconnecting link client, an apply-snapshot path beside `load_config`, and a frame encoder that runs only when asked.

**Tech Stack:** Python 3.11+, `uv` workspace, FastAPI, uvicorn, SQLite (stdlib `sqlite3`), pydantic v2, argon2-cffi, cryptography, websockets, Pillow, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-12-core-m3-server-link-and-interface-design.md`
**Depends on:** M1 and M2, merged. `ors-schema` (`DaemonConfig` and friends), `ors-render`, `ors-daemon` (supervisor, snapshot store, screen worker, displays).

## Global Constraints

- **Research before implementing.** Spec §0 applies to every task: FastAPI/Starlette WebSocket lifecycle and disconnect handling, argon2id parameters, the current symmetric-encryption recommendation, SQLite `PRAGMA` settings for one writer with concurrent readers, and WebP encoding cost at 240×240. Where research contradicts this plan, the research wins — raise it, then implement.
- **TDD.** Failing test first, watch it fail for the expected reason, minimal implementation, watch it pass, commit.
- **No test may sleep to wait for time to pass**, start a subprocess, open a listening socket on a fixed port, or touch SPI. Clocks are injected on both ends, as in M2. Use `threading.Event`/`asyncio.Event` handshakes.
- **Verify by pytest exit code**, never by the tail of the output: run `uv run pytest -q; echo "exit=$?"` and report the number. An M2 task shipped a red commit because a pipe swallowed the status.
- Python `>=3.11`. Every public function annotated. `uv run ruff check --fix . && uv run ruff format .` first, then `uv run ruff check .` and `uv run ruff format --check .` must pass.
- **Nothing may be named `_stop`** — it shadows `threading.Thread._stop`, which `join()` calls, and every join then raises. The daemon uses `_stop_event` throughout; the link client follows.
- `ors-server` may import `ors-schema` and `ors-render`. It must **not** import `ors-daemon`, and no package may import `ors-server`.
- **The server going away is normal.** No failure of the server, the link, or the database may darken the rack.
- Secrets are encrypted at rest, write-only over the API, and never appear in a response or a log line.

---

### Task 1: Server package and app skeleton

**Files:**
- Create: `server/pyproject.toml`, `server/src/ors_server/__init__.py`, `server/src/ors_server/app.py`, `server/src/ors_server/__main__.py`
- Modify: root `pyproject.toml` (workspace member, dependency, testpaths)
- Test: `server/tests/test_app.py`

**Interfaces:**
- Consumes: nothing
- Produces: `create_app(settings: AppSettings) -> FastAPI`; `AppSettings(data_dir: Path, secret_key: str | None)`; `GET /api/health` → `{"status": "ok", "version": str}`

- [ ] **Step 1: Write the failing test**

`server/tests/test_app.py`:

```python
from fastapi.testclient import TestClient

from ors_server.app import AppSettings, create_app


def client(tmp_path) -> TestClient:
    return TestClient(create_app(AppSettings(data_dir=tmp_path)))


def test_health_reports_ok_and_a_version(tmp_path):
    response = client(tmp_path).get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


def test_an_unknown_api_route_is_a_404_not_an_index_page(tmp_path):
    # The SPA is mounted at the root later; /api must never fall through to it,
    # or a typo'd endpoint returns 200 and HTML and the browser cannot tell.
    assert client(tmp_path).get("/api/nope").status_code == 404


def test_the_data_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "data"
    create_app(AppSettings(data_dir=target))

    assert target.is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_app.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server'`

- [ ] **Step 3: Write minimal implementation**

`server/pyproject.toml`:

```toml
[project]
name = "ors-server"
version = "0.1.0"
description = "OpenRackScreen server: owns the rack's config and pushes it to daemons"
requires-python = ">=3.11"
dependencies = [
    "ors-schema",
    "ors-render",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.7",
    "argon2-cffi>=23.1",
    "cryptography>=43.0",
]

[project.scripts]
ors-server = "ors_server.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ors_server"]

[tool.uv.sources]
ors-schema = { workspace = true }
ors-render = { workspace = true }
```

`server/src/ors_server/__init__.py`:

```python
__version__ = "0.1.0"
```

`server/src/ors_server/app.py`:

```python
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

    app = FastAPI(title="OpenRackScreen", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings

    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api)
    return app
```

`server/src/ors_server/__main__.py`:

```python
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
        host=os.environ.get("ORS_HOST", "0.0.0.0"),  # noqa: S104 - a server is meant to listen
        port=int(os.environ.get("ORS_PORT", "8080")),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

Root `pyproject.toml`: add `"server"` to `[tool.uv.workspace] members`, `"ors-server"` to `[project] dependencies`, `ors-server = { workspace = true }` under `[tool.uv.sources]`, `"server/tests"` to `testpaths`, and `httpx` to the dev dependency group (`TestClient` needs it).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv sync --all-packages && uv run pytest -q; echo "exit=$?"`
Expected: PASS, exit 0, with the pre-existing suite intact.

- [ ] **Step 5: Commit**

```bash
git add server pyproject.toml uv.lock
git commit -m "chore(server): package skeleton and health endpoint"
```

---

### Task 2: Database — schema, version check, export-and-rebuild

**Files:**
- Create: `server/src/ors_server/db.py`
- Test: `server/tests/test_db.py`

**Interfaces:**
- Consumes: `AppSettings`
- Produces:
  - `SCHEMA_VERSION: int`
  - `Database(path: Path)` with `.connect() -> sqlite3.Connection`, `.initialise() -> Path | None` (returns the export path when it rebuilt), `.export() -> dict[str, list[dict]]`
  - Tables: `daemon`, `screen`, `template`, `integration`, `secret`, `setting`, `daemon_event`

- [ ] **Step 1: Write the failing test**

`server/tests/test_db.py`:

```python
import json
import sqlite3

from ors_server.db import SCHEMA_VERSION, Database

TABLES = {"daemon", "screen", "template", "integration", "secret", "setting", "daemon_event"}


def test_initialise_creates_every_table_and_records_the_version(tmp_path):
    database = Database(tmp_path / "ors.db")
    assert database.initialise() is None, "a fresh database has nothing to export"

    with database.connect() as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert TABLES <= names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_initialise_is_idempotent_and_keeps_data(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'Europe/Amsterdam')")

    assert database.initialise() is None
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM setting WHERE key='timezone'").fetchone()[0] == (
            "Europe/Amsterdam"
        )


def test_a_stale_schema_is_exported_then_rebuilt(tmp_path):
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'Europe/Amsterdam')")
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()

    assert export is not None and export.exists()
    dumped = json.loads(export.read_text())
    assert {"key": "timezone", "value": "Europe/Amsterdam"} in dumped["setting"]
    with Database(path).connect() as connection:
        assert connection.execute("SELECT count(*) FROM setting").fetchone()[0] == 0


def test_the_export_redacts_secrets(tmp_path):
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO secret (ciphertext) VALUES ('gAAAAAB-not-a-real-token')")
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()
    text = export.read_text()

    assert "gAAAAAB" not in text, "an export is a file on disk; it does not carry ciphertext"
    assert json.loads(text)["secret"] == [{"id": 1, "ciphertext": "<redacted>"}]


def test_foreign_keys_are_enforced(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()

    with database.connect() as connection:
        try:
            connection.execute(
                "INSERT INTO screen (daemon_id, position, name, display, template, params)"
                " VALUES (999, 1, 'X', '{}', 'ring-gauge', '{}')"
            )
        except sqlite3.IntegrityError:
            return
    raise AssertionError("a screen may not belong to a daemon that does not exist")


def test_rows_come_back_as_mappings(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'UTC')")
        row = connection.execute("SELECT key, value FROM setting").fetchone()

    assert row["key"] == "timezone", "callers read columns by name, not by index"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_db.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.db'`

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/db.py`:

```python
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
"""Bumped whenever the schema below changes.

There are no migrations: a bump exports the database and rebuilds it empty.
That is a deliberate trade for a single-file database holding a config you can
re-enter, and the export is what makes it survivable -- see `initialise`.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    token_hash     TEXT,
    paired_at      TEXT,
    version        TEXT,
    capabilities   TEXT NOT NULL DEFAULT '{}',
    last_seen      TEXT,
    status         TEXT NOT NULL DEFAULT 'unpaired',
    config_version INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    builtin       INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL DEFAULT 'general',
    scenes        TEXT NOT NULL,
    params_schema TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS screen (
    id             INTEGER PRIMARY KEY,
    daemon_id      INTEGER NOT NULL REFERENCES daemon(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    display        TEXT NOT NULL,
    rotation       INTEGER NOT NULL DEFAULT 0,
    hflip          INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    template       TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}',
    sleep_override TEXT
);

CREATE TABLE IF NOT EXISTS secret (
    id         INTEGER PRIMARY KEY,
    ciphertext TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration (
    id            INTEGER PRIMARY KEY,
    daemon_id     INTEGER NOT NULL REFERENCES daemon(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    name          TEXT NOT NULL,
    config        TEXT NOT NULL,
    secret_id     INTEGER REFERENCES secret(id) ON DELETE SET NULL,
    poll_interval REAL NOT NULL DEFAULT 5.0,
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_event (
    id        INTEGER PRIMARY KEY,
    daemon_id INTEGER REFERENCES daemon(id) ON DELETE CASCADE,
    at        TEXT NOT NULL,
    level     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""

_REDACTED = {"secret": {"ciphertext"}}


class Database:
    """The one SQLite file, opened only by this process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL so a reader is never blocked by the writer; foreign keys because
        # SQLite leaves them off by default and a screen whose daemon is gone is
        # a config the daemon cannot be given.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialise(self) -> Path | None:
        """Create or rebuild the schema. Returns the export path if it rebuilt."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]

        export: Path | None = None
        if not fresh and version != SCHEMA_VERSION:
            export = self._write_export()
            self.path.unlink()
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)

        with self.connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return export

    def export(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every table, with secrets redacted."""
        dumped: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                rows = []
                for row in connection.execute(f"SELECT * FROM {table}"):  # noqa: S608 - names from sqlite_master
                    record = dict(row)
                    for column in _REDACTED.get(table, ()):
                        record[column] = "<redacted>"
                    rows.append(record)
                dumped[table] = rows
        return dumped

    def _write_export(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"export-{stamp}.json")
        target.write_text(json.dumps(self.export(), indent=2, default=str))
        return target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_db.py -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): SQLite schema with export-then-rebuild on a version bump"
```

---

### Task 3: Secrets at rest

**Files:**
- Create: `server/src/ors_server/secrets.py`
- Test: `server/tests/test_secrets.py`

**Interfaces:**
- Consumes: `AppSettings.secret_key`, `Database`
- Produces: `SecretStore(database: Database, key: bytes)` with `.put(plaintext: str) -> int`, `.get(secret_id: int) -> str`, `.delete(secret_id: int) -> None`; `load_or_create_key(data_dir: Path, configured: str | None) -> bytes`

- [ ] **Step 1: Write the failing test**

`server/tests/test_secrets.py`:

```python
import stat

import pytest

from ors_server.db import Database
from ors_server.secrets import SecretStore, load_or_create_key


def store(tmp_path) -> SecretStore:
    database = Database(tmp_path / "ors.db")
    database.initialise()
    return SecretStore(database, load_or_create_key(tmp_path, None))


def test_a_secret_round_trips(tmp_path):
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")

    assert secrets.get(secret_id) == "hunter2"


def test_the_stored_form_is_not_the_plaintext(tmp_path):
    secrets = store(tmp_path)
    secrets.put("hunter2")

    blob = (tmp_path / "ors.db").read_bytes()
    assert b"hunter2" not in blob


def test_a_key_is_generated_once_and_reused(tmp_path):
    first = load_or_create_key(tmp_path, None)

    assert load_or_create_key(tmp_path, None) == first


def test_the_generated_key_file_is_not_world_readable(tmp_path):
    load_or_create_key(tmp_path, None)
    mode = (tmp_path / "secret.key").stat().st_mode

    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), "a key file readable by anyone is not a key"


def test_a_configured_key_wins_over_the_file(tmp_path):
    configured = load_or_create_key(tmp_path, "9" * 44)

    assert configured == b"9" * 44
    assert not (tmp_path / "secret.key").exists(), "nothing is written when a key is supplied"


def test_a_secret_encrypted_under_another_key_will_not_decrypt(tmp_path):
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")

    other = SecretStore(secrets.database, load_or_create_key(tmp_path / "other", None))
    with pytest.raises(Exception):
        other.get(secret_id)


def test_deleting_a_secret_removes_it(tmp_path):
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")
    secrets.delete(secret_id)

    with pytest.raises(KeyError):
        secrets.get(secret_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_secrets.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.secrets'`

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/secrets.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

from ors_server.db import Database

_KEY_FILE = "secret.key"


def load_or_create_key(data_dir: Path, configured: str | None) -> bytes:
    """The configured key if there is one, else a generated one kept at 0600."""
    if configured:
        return configured.encode()

    path = Path(data_dir) / _KEY_FILE
    if path.exists():
        return path.read_bytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Written before the mode is set would leave a readable window, so the
    # descriptor is opened with the mode already on it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


class SecretStore:
    """Credentials at rest. Nothing here ever returns to the browser."""

    def __init__(self, database: Database, key: bytes) -> None:
        self.database = database
        self._fernet = Fernet(key)

    def put(self, plaintext: str) -> int:
        token = self._fernet.encrypt(plaintext.encode()).decode()
        with self.database.connect() as connection:
            cursor = connection.execute("INSERT INTO secret (ciphertext) VALUES (?)", (token,))
            return int(cursor.lastrowid)

    def get(self, secret_id: int) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM secret WHERE id = ?", (secret_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"no secret {secret_id}")
        return self._fernet.decrypt(row["ciphertext"].encode()).decode()

    def delete(self, secret_id: int) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM secret WHERE id = ?", (secret_id,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests/test_secrets.py -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): secrets encrypted at rest"
```

---

### Task 4: Admin password and session auth

**Files:**
- Create: `server/src/ors_server/auth.py`, `server/src/ors_server/api/__init__.py`, `server/src/ors_server/api/auth.py`
- Modify: `server/src/ors_server/app.py`
- Test: `server/tests/test_auth.py`

**Interfaces:**
- Consumes: `Database`, `AppSettings`
- Produces:
  - `set_password(database, password: str) -> None`, `verify_password(database, password: str) -> bool`, `password_is_set(database) -> bool`
  - `require_session` — a FastAPI dependency raising 401 when the cookie is absent or invalid
  - `SESSION_COOKIE = "ors_session"`
  - Routes: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/setup`

- [ ] **Step 1: Write the failing test**

`server/tests/test_auth.py`:

```python
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ors_server.app import AppSettings, create_app
from ors_server.auth import require_session


def app_and_client(tmp_path) -> tuple[FastAPI, TestClient]:
    app = create_app(AppSettings(data_dir=tmp_path))

    @app.get("/api/guarded")
    def guarded(_: None = Depends(require_session)) -> dict[str, bool]:
        return {"ok": True}

    return app, TestClient(app)


def setup_password(client: TestClient, password: str = "correct horse") -> None:
    assert client.post("/api/auth/setup", json={"password": password}).status_code == 200


def test_a_fresh_server_reports_that_no_password_is_set(tmp_path):
    _, client = app_and_client(tmp_path)

    assert client.get("/api/auth/me").json() == {"authenticated": False, "password_set": False}


def test_setup_sets_the_password_once_and_then_refuses(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    second = client.post("/api/auth/setup", json={"password": "someone else's"})
    assert second.status_code == 409, "a second setup would be a password reset for anyone"


def test_login_grants_a_session_and_logout_takes_it_away(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/api/guarded").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/guarded").status_code == 401


def test_a_wrong_password_is_refused(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/guarded").status_code == 401


def test_the_session_cookie_is_http_only_and_same_site_strict(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)
    response = client.post("/api/auth/login", json={"password": "correct horse"})

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header, "a cookie readable by script is a cookie stealable by script"
    assert "samesite=strict" in header


def test_a_guarded_route_refuses_a_forged_cookie(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)
    client.cookies.set("ors_session", "not-a-real-token")

    assert client.get("/api/guarded").status_code == 401


def test_repeated_failures_are_rate_limited(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    codes = [client.post("/api/auth/login", json={"password": "wrong"}).status_code for _ in range(12)]

    assert 429 in codes, "an unlimited password endpoint is an offline attack with extra steps"


def test_the_password_is_not_stored_in_the_clear(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client, "correct horse")

    assert b"correct horse" not in (tmp_path / "ors.db").read_bytes()


def test_health_stays_open(tmp_path):
    _, client = app_and_client(tmp_path)
    setup_password(client)

    assert client.get("/api/health").status_code == 200, "a health check nobody can call is useless"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_auth.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.auth'`

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/auth.py`:

```python
from __future__ import annotations

import secrets as _secrets
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, HTTPException, Request

from ors_server.db import Database

SESSION_COOKIE = "ors_session"
_PASSWORD_KEY = "admin_password_hash"
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 60.0

_hasher = PasswordHasher()


def password_is_set(database: Database) -> bool:
    with database.connect() as connection:
        row = connection.execute("SELECT 1 FROM setting WHERE key = ?", (_PASSWORD_KEY,)).fetchone()
    return row is not None


def set_password(database: Database, password: str) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PASSWORD_KEY, _hasher.hash(password)),
        )


def verify_password(database: Database, password: str) -> bool:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value FROM setting WHERE key = ?", (_PASSWORD_KEY,)
        ).fetchone()
    if row is None:
        return False
    try:
        return _hasher.verify(row["value"], password)
    except VerifyMismatchError:
        return False


class Sessions:
    """In-memory sessions. A restart logs everyone out, which is acceptable here."""

    def __init__(self) -> None:
        self._tokens: set[str] = set()
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def issue(self) -> str:
        token = _secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return token is not None and token in self._tokens

    def revoke(self, token: str | None) -> None:
        self._tokens.discard(token or "")

    def too_many_attempts(self, client: str, now: float) -> bool:
        recent = [at for at in self._attempts[client] if now - at < _WINDOW_SECONDS]
        self._attempts[client] = recent
        return len(recent) >= _MAX_ATTEMPTS

    def record_attempt(self, client: str, now: float) -> None:
        self._attempts[client].append(now)


def require_session(request: Request, ors_session: str | None = Cookie(default=None)) -> None:
    """Every API route and both sockets sit behind this."""
    sessions: Sessions = request.app.state.sessions
    if not sessions.valid(ors_session):
        raise HTTPException(status_code=401, detail="not authenticated")


def now() -> float:
    return time.monotonic()
```

`server/src/ors_server/api/auth.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ors_server.auth import (
    SESSION_COOKIE,
    now,
    password_is_set,
    set_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class PasswordBody(BaseModel):
    password: str = Field(min_length=1)


@router.get("/me")
def me(request: Request, ors_session: str | None = Cookie(default=None)) -> dict[str, bool]:
    return {
        "authenticated": request.app.state.sessions.valid(ors_session),
        "password_set": password_is_set(request.app.state.database),
    }


@router.post("/setup")
def setup(request: Request, body: PasswordBody) -> dict[str, bool]:
    database = request.app.state.database
    if password_is_set(database):
        raise HTTPException(status_code=409, detail="a password is already set")
    set_password(database, body.password)
    return {"ok": True}


@router.post("/login")
def login(request: Request, response: Response, body: PasswordBody) -> dict[str, bool]:
    sessions = request.app.state.sessions
    client = request.client.host if request.client else "unknown"
    if sessions.too_many_attempts(client, now()):
        raise HTTPException(status_code=429, detail="too many attempts")

    if not verify_password(request.app.state.database, body.password):
        sessions.record_attempt(client, now())
        raise HTTPException(status_code=401, detail="wrong password")

    response.set_cookie(
        SESSION_COOKIE, sessions.issue(), httponly=True, samesite="strict", path="/"
    )
    return {"ok": True}


@router.post("/logout")
def logout(
    request: Request, response: Response, ors_session: str | None = Cookie(default=None)
) -> dict[str, bool]:
    request.app.state.sessions.revoke(ors_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
```

`server/src/ors_server/api/__init__.py` is empty.

In `create_app`, before including the router: initialise the database and hang the shared objects on `app.state` —

```python
    database = Database(settings.data_dir / "ors.db")
    export = database.initialise()
    if export is not None:
        log.warning("schema changed; exported and rebuilt", extra={"export": str(export)})
    app.state.database = database
    app.state.sessions = Sessions()
    app.state.secrets = SecretStore(database, load_or_create_key(settings.data_dir, settings.secret_key))
```

and `api.include_router(auth_router)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): admin password, sessions and rate-limited login"
```

---

### Task 5: Link protocol models

**Files:**
- Create: `packages/ors-schema/src/ors_schema/link.py`
- Modify: `packages/ors-schema/src/ors_schema/__init__.py`
- Test: `packages/ors-schema/tests/test_link.py`

**Interfaces:**
- Consumes: `DaemonConfig`
- Produces:
  - `PROTOCOL_VERSION: int`
  - Daemon → server: `Hello`, `Heartbeat`, `Ack`, `Nack`, `SourceStatus`, `Frame`, `LogLine`
  - Server → daemon: `ConfigPush`, `Command`, `FramesRequest`
  - `DaemonMessage` and `ServerMessage` — discriminated unions on `type`
  - `parse_daemon_message(raw: str) -> DaemonMessage`, `parse_server_message(raw: str) -> ServerMessage`

- [ ] **Step 1: Write the failing test**

`packages/ors-schema/tests/test_link.py`:

```python
import pytest
from pydantic import ValidationError

from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    PROTOCOL_VERSION,
    Ack,
    Command,
    ConfigPush,
    Frame,
    FramesRequest,
    Hello,
    Nack,
    parse_daemon_message,
    parse_server_message,
)

CONFIG = {
    "version": 1,
    "timezone": "UTC",
    "integrations": [
        {"name": "prom", "type": "prometheus", "url": "http://p:9090", "fields": {"cpu": {"query": "up"}}}
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "ring-gauge",
            "params": {},
        }
    ],
}


def test_hello_carries_what_the_server_needs_to_identify_a_daemon():
    hello = Hello(token="abc", hostname="pi-rack", daemon_version="0.1.0", capabilities={"spi": [0, 1]})

    assert hello.type == "hello"
    assert hello.protocol_version == PROTOCOL_VERSION


def test_a_config_push_carries_a_whole_validated_snapshot():
    push = ConfigPush(version=7, snapshot=DaemonConfig.model_validate(CONFIG))

    assert push.snapshot.screens[0].name == "CPU"
    assert push.version == 7


def test_a_push_whose_snapshot_is_invalid_is_rejected_at_the_edge():
    with pytest.raises(ValidationError):
        ConfigPush(version=1, snapshot={"screens": [{"rotation": 45}]})


def test_daemon_messages_are_discriminated_by_type():
    parsed = parse_daemon_message(Ack(config_version=7).model_dump_json())

    assert isinstance(parsed, Ack)
    assert parsed.config_version == 7


def test_server_messages_are_discriminated_by_type():
    parsed = parse_server_message(Command(command="identify").model_dump_json())

    assert isinstance(parsed, Command)
    assert parsed.command == "identify"


def test_an_unknown_message_type_is_rejected_rather_than_ignored():
    with pytest.raises(ValidationError):
        parse_daemon_message('{"type": "shutdown_everything"}')


def test_a_nack_carries_the_reason_a_snapshot_was_refused():
    nack = Nack(config_version=7, reason="screens.0.rotation: Input should be 0, 90, 180 or 270")

    assert "rotation" in nack.reason


def test_a_frames_request_names_the_screens_and_the_rate():
    request = FramesRequest(enabled=True, screen_ids=[1, 2], fps=2.0)

    assert request.screen_ids == [1, 2]
    assert FramesRequest(enabled=False).screen_ids == []


def test_a_frame_carries_bytes_and_a_sequence_number():
    frame = Frame(screen_id=1, seq=42, webp=b"RIFF....WEBP")

    # Round-tripped through JSON as base64, because the envelope is JSON and
    # bytes are not: a frame that silently became a str would decode to garbage.
    restored = Frame.model_validate_json(frame.model_dump_json())
    assert restored.webp == b"RIFF....WEBP"
    assert restored.seq == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-schema/tests/test_link.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_schema.link'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-schema/src/ors_schema/link.py`:

```python
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ors_schema.daemon import DaemonConfig

PROTOCOL_VERSION = 1
"""Bumped when a message shape changes incompatibly.

Carried in `hello` so a server meeting an older daemon can say so, rather than
failing on a field neither end can explain.
"""


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hello(_Message):
    type: Literal["hello"] = "hello"
    token: str
    hostname: str
    daemon_version: str
    protocol_version: int = PROTOCOL_VERSION
    capabilities: dict[str, Any] = Field(default_factory=dict)


class Heartbeat(_Message):
    type: Literal["heartbeat"] = "heartbeat"
    uptime_s: int = 0
    status: dict[str, Any] = Field(default_factory=dict)


class Ack(_Message):
    type: Literal["ack"] = "ack"
    config_version: int


class Nack(_Message):
    type: Literal["nack"] = "nack"
    config_version: int
    reason: str


class SourceStatus(_Message):
    type: Literal["source_status"] = "source_status"
    integration: str
    state: str
    reason: str | None = None
    latency_ms: float | None = None


class Frame(_Message):
    type: Literal["frame"] = "frame"
    screen_id: int
    seq: int
    webp: bytes


class LogLine(_Message):
    type: Literal["log"] = "log"
    level: str
    message: str


class ConfigPush(_Message):
    type: Literal["config"] = "config"
    version: int
    """The server's generation counter, and what an `Ack` refers to.

    Not `DaemonConfig.version`, which is the config *schema* version and a
    constant, and not the daemon's status `config_fingerprint`, which is a
    content hash. Three different questions; three different fields.
    """
    snapshot: DaemonConfig


class Command(_Message):
    type: Literal["command"] = "command"
    command: Literal["identify", "sleep", "wake", "reload"]
    screen_id: int | None = None


class FramesRequest(_Message):
    type: Literal["frames"] = "frames"
    enabled: bool
    screen_ids: list[int] = Field(default_factory=list)
    fps: float = 2.0


DaemonMessage = Annotated[
    Union[Hello, Heartbeat, Ack, Nack, SourceStatus, Frame, LogLine],
    Field(discriminator="type"),
]
ServerMessage = Annotated[Union[ConfigPush, Command, FramesRequest], Field(discriminator="type")]

_daemon_adapter: TypeAdapter[DaemonMessage] = TypeAdapter(DaemonMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_daemon_message(raw: str | bytes) -> DaemonMessage:
    return _daemon_adapter.validate_json(raw)


def parse_server_message(raw: str | bytes) -> ServerMessage:
    return _server_adapter.validate_json(raw)
```

Extend `ors_schema/__init__.py` with the new names in its imports and `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-schema/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-schema
git commit -m "feat(schema): link protocol models shared by both ends"
```

---

### Task 6: Snapshot assembly — database rows to a `DaemonConfig`

**Files:**
- Create: `server/src/ors_server/snapshot.py`
- Test: `server/tests/test_snapshot.py`

**Interfaces:**
- Consumes: `Database`, `SecretStore`, `ors_render.load_builtin_templates`
- Produces:
  - `build_snapshot(database, secrets, daemon_id: int) -> DaemonConfig`
  - `bump_config_version(database, daemon_id: int) -> int`
  - `seed_builtin_templates(database) -> None`

- [ ] **Step 1: Write the failing test**

`server/tests/test_snapshot.py`:

```python
import json

import pytest
from ors_schema.daemon import DaemonConfig

from ors_server.db import Database
from ors_server.secrets import SecretStore, load_or_create_key
from ors_server.snapshot import build_snapshot, bump_config_version, seed_builtin_templates


def fixtures(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    seed_builtin_templates(database)
    secrets = SecretStore(database, load_or_create_key(tmp_path, None))
    with database.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO daemon (name, status, created_at) VALUES ('pi-rack', 'paired', '2026-01-01')"
        )
        daemon_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO setting (key, value) VALUES ('timezone', 'Europe/Amsterdam')"
        )
        connection.execute(
            "INSERT INTO screen (daemon_id, position, name, display, rotation, hflip, template, params)"
            " VALUES (?, 1, 'CPU', ?, 270, 0, 'ring-gauge', ?)",
            (daemon_id, json.dumps({"backend": "virtual", "out_dir": "/tmp/p"}), json.dumps({"title": "CPU"})),
        )
    return database, secrets, daemon_id


def test_a_snapshot_is_a_valid_daemon_config(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert isinstance(snapshot, DaemonConfig)
    assert snapshot.timezone == "Europe/Amsterdam"
    assert [screen.name for screen in snapshot.screens] == ["CPU"]
    assert snapshot.screens[0].rotation == 270
    assert snapshot.screens[0].params["title"] == "CPU"


def test_only_this_daemons_screens_are_included(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with database.connect() as connection:
        other = int(
            connection.execute(
                "INSERT INTO daemon (name, status, created_at) VALUES ('other', 'paired', '2026-01-01')"
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO screen (daemon_id, position, name, display, template, params)"
            " VALUES (?, 1, 'THEIRS', ?, 'ring-gauge', '{}')",
            (other, json.dumps({"backend": "virtual", "out_dir": "/tmp/q"})),
        )

    assert [s.name for s in build_snapshot(database, secrets, daemon_id).screens] == ["CPU"]


def test_an_integrations_secret_is_decrypted_into_the_snapshot(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    secret_id = secrets.put("s3cret")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO integration (daemon_id, type, name, config, secret_id, poll_interval)"
            " VALUES (?, 'prometheus', 'prom', ?, ?, 5.0)",
            (
                daemon_id,
                json.dumps({"url": "http://p:9090", "fields": {"cpu": {"query": "up"}}}),
                secret_id,
            ),
        )

    integration = build_snapshot(database, secrets, daemon_id).integrations[0]

    assert integration.name == "prom"
    assert integration.url == "http://p:9090"


def test_builtin_templates_are_seeded_and_travel_in_the_snapshot(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert "ring-gauge" in snapshot.templates
    assert "system" in snapshot.templates, "the daemon needs the system scenes to show connecting"


def test_seeding_twice_does_not_duplicate(tmp_path):
    database, _, _ = fixtures(tmp_path)
    seed_builtin_templates(database)

    with database.connect() as connection:
        count = connection.execute("SELECT count(*) FROM template WHERE name='ring-gauge'").fetchone()[0]
    assert count == 1


def test_the_config_version_increases_and_is_per_daemon(tmp_path):
    database, _, daemon_id = fixtures(tmp_path)

    first = bump_config_version(database, daemon_id)
    second = bump_config_version(database, daemon_id)

    assert second == first + 1


def test_a_snapshot_for_an_unknown_daemon_is_an_error(tmp_path):
    database, secrets, _ = fixtures(tmp_path)

    with pytest.raises(KeyError):
        build_snapshot(database, secrets, 4242)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_snapshot.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.snapshot'`

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/snapshot.py`:

```python
from __future__ import annotations

import json
from typing import Any

from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig

from ors_server.db import Database
from ors_server.secrets import SecretStore

_DEFAULTS = {"timezone": "UTC"}


def seed_builtin_templates(database: Database) -> None:
    """Copy the render engine's built-ins into the database, once."""
    with database.connect() as connection:
        for name, template in load_builtin_templates().items():
            connection.execute(
                "INSERT INTO template (name, builtin, category, scenes, params_schema)"
                " VALUES (?, 1, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
                (
                    name,
                    template.category,
                    json.dumps([scene.model_dump(exclude_none=True) for scene in template.scenes]),
                    json.dumps(
                        {key: spec.model_dump() for key, spec in template.params_schema.items()}
                    ),
                ),
            )


def bump_config_version(database: Database, daemon_id: int) -> int:
    """Advance this daemon's generation counter and return the new value."""
    with database.connect() as connection:
        connection.execute(
            "UPDATE daemon SET config_version = config_version + 1 WHERE id = ?", (daemon_id,)
        )
        row = connection.execute(
            "SELECT config_version FROM daemon WHERE id = ?", (daemon_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"no daemon {daemon_id}")
    return int(row["config_version"])


def _setting(connection: Any, key: str) -> str:
    row = connection.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else _DEFAULTS[key]


def build_snapshot(database: Database, secrets: SecretStore, daemon_id: int) -> DaemonConfig:
    """Assemble what this daemon should be running, as the model it already loads.

    The wire format is `DaemonConfig` itself, so a snapshot goes through exactly
    the validation a hand-written YAML file does -- and a server that assembles
    something the daemon would refuse finds out here rather than on the rack.
    """
    with database.connect() as connection:
        if connection.execute("SELECT 1 FROM daemon WHERE id = ?", (daemon_id,)).fetchone() is None:
            raise KeyError(f"no daemon {daemon_id}")

        screens = [
            {
                "name": row["name"],
                "position": row["position"],
                "display": json.loads(row["display"]),
                "rotation": row["rotation"],
                "hflip": bool(row["hflip"]),
                "enabled": bool(row["enabled"]),
                "template": row["template"],
                "params": json.loads(row["params"]),
                "sleep_override": json.loads(row["sleep_override"]) if row["sleep_override"] else None,
            }
            for row in connection.execute(
                "SELECT * FROM screen WHERE daemon_id = ? ORDER BY position", (daemon_id,)
            )
        ]

        integrations = []
        for row in connection.execute(
            "SELECT * FROM integration WHERE daemon_id = ? AND enabled = 1", (daemon_id,)
        ):
            config = json.loads(row["config"])
            config |= {
                "name": row["name"],
                "type": row["type"],
                "poll_interval": row["poll_interval"],
            }
            if row["secret_id"] is not None:
                # The daemon needs the credential itself; the browser never does.
                config["password"] = secrets.get(row["secret_id"])
            integrations.append(config)

        templates = {
            row["name"]: {
                "name": row["name"],
                "category": row["category"],
                "builtin": bool(row["builtin"]),
                "scenes": json.loads(row["scenes"]),
                "params_schema": json.loads(row["params_schema"]),
            }
            for row in connection.execute("SELECT * FROM template")
        }

        night_row = connection.execute("SELECT value FROM setting WHERE key='night'").fetchone()
        payload: dict[str, Any] = {
            "version": 1,
            "timezone": _setting(connection, "timezone"),
            "screens": screens,
            "integrations": integrations,
            "templates": templates,
        }
        if night_row:
            payload["night"] = json.loads(night_row["value"])

    return DaemonConfig.model_validate(payload)
```

**Note for the implementer:** `PrometheusConfig` has no `password` field today, so the `secrets.get` line above will be rejected by `extra="forbid"` for a Prometheus integration. Prometheus needs no credential; the line exists for M4's qBittorrent. Either gate it on the integration type, or leave it out entirely and add it in M4 — decide, and say which in your report. Do not weaken `extra="forbid"` to make it fit.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): assemble a daemon snapshot from the database"
```

---

### Task 7: The link hub

**Files:**
- Create: `server/src/ors_server/link/__init__.py`, `server/src/ors_server/link/hub.py`
- Test: `server/tests/test_hub.py`

**Interfaces:**
- Consumes: link protocol models
- Produces:
  - `Hub()` with `register(daemon_id, sender) -> Connection`, `drop(connection)`, `is_online(daemon_id) -> bool`, `online_ids() -> set[int]`
  - `await push_config(daemon_id, ConfigPush) -> None`, `await send_command(daemon_id, Command) -> None`, `await request_frames(daemon_id, FramesRequest) -> None`
  - `record_ack(daemon_id, version)`, `acked_version(daemon_id) -> int | None`
  - `subscribe_frames(screen_id, queue)`, `unsubscribe_frames(screen_id, queue)`, `await relay_frame(Frame)`
  - `Sender = Callable[[str | bytes], Awaitable[None]]`

- [ ] **Step 1: Write the failing test**

`server/tests/test_hub.py`:

```python
import asyncio

import pytest
from ors_schema.link import Command, Frame

from ors_server.link.hub import Hub


class FakeSocket:
    def __init__(self, fails: bool = False) -> None:
        self.sent: list[str | bytes] = []
        self.fails = fails

    async def send(self, payload: str | bytes) -> None:
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_a_registered_daemon_is_online_and_receives_a_command():
    hub, socket = Hub(), FakeSocket()
    hub.register(1, socket.send)

    assert hub.is_online(1) is True
    await hub.send_command(1, Command(command="identify"))

    assert '"identify"' in socket.sent[0]


@pytest.mark.asyncio
async def test_sending_to_an_offline_daemon_is_not_an_error():
    # An edit made while the Pi is unplugged must save; the daemon picks it up
    # when it reconnects. Raising here would make the API 500 on a normal state.
    await Hub().send_command(99, Command(command="reload"))


@pytest.mark.asyncio
async def test_a_dropped_connection_stops_being_online():
    hub, socket = Hub(), FakeSocket()
    connection = hub.register(1, socket.send)
    hub.drop(connection)

    assert hub.is_online(1) is False


@pytest.mark.asyncio
async def test_a_second_connection_for_one_daemon_replaces_the_first():
    hub, first, second = Hub(), FakeSocket(), FakeSocket()
    hub.register(1, first.send)
    hub.register(1, second.send)

    await hub.send_command(1, Command(command="reload"))

    assert second.sent and not first.sent, "a reconnect must not leave a stale socket receiving"


@pytest.mark.asyncio
async def test_a_send_that_fails_takes_the_daemon_offline():
    hub = Hub()
    hub.register(1, FakeSocket(fails=True).send)

    await hub.send_command(1, Command(command="reload"))

    assert hub.is_online(1) is False


@pytest.mark.asyncio
async def test_acks_are_recorded_per_daemon():
    hub = Hub()
    assert hub.acked_version(1) is None

    hub.record_ack(1, 7)
    assert hub.acked_version(1) == 7


@pytest.mark.asyncio
async def test_a_frame_reaches_every_subscriber_of_that_screen_and_no_other():
    hub = Hub()
    watching: asyncio.Queue[Frame] = asyncio.Queue()
    other: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, watching)
    hub.subscribe_frames(2, other)

    await hub.relay_frame(Frame(screen_id=1, seq=1, webp=b"x"))

    assert (await asyncio.wait_for(watching.get(), 1)).seq == 1
    assert other.empty()


@pytest.mark.asyncio
async def test_unsubscribing_stops_delivery_and_reports_the_last_one_out():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue()
    hub.subscribe_frames(1, queue)

    assert hub.unsubscribe_frames(1, queue) is True, "the last subscriber leaving is what stops the daemon"
    await hub.relay_frame(Frame(screen_id=1, seq=1, webp=b"x"))

    assert queue.empty()


@pytest.mark.asyncio
async def test_a_full_subscriber_queue_drops_the_frame_rather_than_blocking():
    hub = Hub()
    queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=1)
    hub.subscribe_frames(1, queue)

    await hub.relay_frame(Frame(screen_id=1, seq=1, webp=b"x"))
    await asyncio.wait_for(hub.relay_frame(Frame(screen_id=1, seq=2, webp=b"y")), 1)

    assert queue.qsize() == 1, "a slow browser must not stall the daemon's socket"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_hub.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.link'`

- [ ] **Step 3: Write minimal implementation**

Add `pytest-asyncio>=0.24` to the root dev dependency group and `asyncio_mode = "auto"` under `[tool.pytest.ini_options]`.

`server/src/ors_server/link/hub.py`:

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ors_schema.link import Command, ConfigPush, Frame, FramesRequest

log = logging.getLogger(__name__)

Sender = Callable[[str | bytes], Awaitable[None]]


@dataclass
class Connection:
    daemon_id: int
    send: Sender
    subscriptions: set[int] = field(default_factory=set)


class Hub:
    """Who is connected, what they have acked, and who is watching which screen.

    Deliberately ignorant of the database: it moves messages and tracks
    liveness, and every decision about *what* to send is made by a caller that
    can read rows. That is what keeps the API testable without a socket and the
    socket testable without a database.
    """

    def __init__(self) -> None:
        self._connections: dict[int, Connection] = {}
        self._acked: dict[int, int] = {}
        self._watchers: dict[int, set[asyncio.Queue[Frame]]] = {}

    def register(self, daemon_id: int, send: Sender) -> Connection:
        # A reconnect arrives before the old socket's close is always observed,
        # so the newest connection wins outright rather than being refused.
        connection = Connection(daemon_id=daemon_id, send=send)
        self._connections[daemon_id] = connection
        return connection

    def drop(self, connection: Connection) -> None:
        current = self._connections.get(connection.daemon_id)
        if current is connection:
            del self._connections[connection.daemon_id]

    def is_online(self, daemon_id: int) -> bool:
        return daemon_id in self._connections

    def online_ids(self) -> set[int]:
        return set(self._connections)

    def record_ack(self, daemon_id: int, version: int) -> None:
        self._acked[daemon_id] = version

    def acked_version(self, daemon_id: int) -> int | None:
        return self._acked.get(daemon_id)

    async def push_config(self, daemon_id: int, push: ConfigPush) -> None:
        await self._send(daemon_id, push.model_dump_json())

    async def send_command(self, daemon_id: int, command: Command) -> None:
        await self._send(daemon_id, command.model_dump_json())

    async def request_frames(self, daemon_id: int, request: FramesRequest) -> None:
        await self._send(daemon_id, request.model_dump_json())

    def subscribe_frames(self, screen_id: int, queue: asyncio.Queue[Frame]) -> bool:
        """Returns True when this is the first watcher, which starts the daemon."""
        watchers = self._watchers.setdefault(screen_id, set())
        first = not watchers
        watchers.add(queue)
        return first

    def unsubscribe_frames(self, screen_id: int, queue: asyncio.Queue[Frame]) -> bool:
        """Returns True when the last watcher left, which stops the daemon."""
        watchers = self._watchers.get(screen_id, set())
        watchers.discard(queue)
        if watchers:
            return False
        self._watchers.pop(screen_id, None)
        return True

    def watched_screens(self) -> set[int]:
        return set(self._watchers)

    async def relay_frame(self, frame: Frame) -> None:
        for queue in list(self._watchers.get(frame.screen_id, ())):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # The newest frame wins; a browser that cannot keep up gets the
                # next one instead of holding the daemon's socket open.
                log.debug("dropped a frame for a slow watcher", extra={"screen": frame.screen_id})

    async def _send(self, daemon_id: int, payload: str) -> None:
        connection = self._connections.get(daemon_id)
        if connection is None:
            # Offline is a normal state, not an error: the edit is already
            # saved, and the snapshot is pushed again when it reconnects.
            return
        try:
            await connection.send(payload)
        except Exception as exc:
            log.info("daemon send failed; dropping", extra={"daemon": daemon_id, "error": str(exc)})
            self.drop(connection)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server pyproject.toml uv.lock
git commit -m "feat(server): the link hub — connections, acks and frame fan-out"
```

---

### Task 8: `/ws/daemon` — pairing and the daemon socket

**Files:**
- Create: `server/src/ors_server/link/ws_daemon.py`, `server/src/ors_server/pairing.py`
- Modify: `server/src/ors_server/app.py`
- Test: `server/tests/test_ws_daemon.py`

**Interfaces:**
- Consumes: `Hub`, `Database`, `build_snapshot`, `bump_config_version`
- Produces:
  - `mint_token(database, name: str) -> tuple[int, str]` — daemon id and the one-time token
  - `claim_token(database, token: str) -> int | None` — the daemon id, marking it spent
  - `WS /ws/daemon`

- [ ] **Step 1: Write the failing test**

`server/tests/test_ws_daemon.py`:

```python
import json

from fastapi.testclient import TestClient
from ors_schema.link import Ack, Hello

from ors_server.app import AppSettings, create_app
from ors_server.pairing import mint_token


def build(tmp_path) -> tuple[TestClient, int, str]:
    app = create_app(AppSettings(data_dir=tmp_path))
    client = TestClient(app)
    daemon_id, token = mint_token(app.state.database, "pi-rack")
    return client, daemon_id, token


def hello(token: str) -> str:
    return Hello(token=token, hostname="pi-rack", daemon_version="0.1.0").model_dump_json()


def test_a_valid_token_is_accepted_and_answered_with_a_snapshot(tmp_path):
    client, _, token = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        message = json.loads(socket.receive_text())

    assert message["type"] == "config"
    assert message["snapshot"]["timezone"]


def test_a_bad_token_is_refused_and_the_socket_closes(tmp_path):
    client, _, _ = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello("not-the-token"))
        try:
            socket.receive_text()
        except Exception:
            return
    raise AssertionError("an unpaired daemon must not be left connected")


def test_a_token_cannot_be_claimed_twice(tmp_path):
    client, _, token = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        try:
            socket.receive_text()
        except Exception:
            return
    raise AssertionError("a spent token is a second daemon claiming one identity")


def test_a_connected_daemon_is_online_and_its_ack_is_recorded(tmp_path):
    client, daemon_id, token = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        push = json.loads(socket.receive_text())
        socket.send_text(Ack(config_version=push["version"]).model_dump_json())

        assert client.app.state.hub.is_online(daemon_id)


def test_disconnecting_takes_the_daemon_offline(tmp_path):
    client, daemon_id, token = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

    assert client.app.state.hub.is_online(daemon_id) is False


def test_hello_marks_the_daemon_paired_and_records_its_version(tmp_path):
    client, daemon_id, token = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(hello(token))
        socket.receive_text()

    with client.app.state.database.connect() as connection:
        row = connection.execute("SELECT * FROM daemon WHERE id = ?", (daemon_id,)).fetchone()
    assert row["status"] == "paired"
    assert row["version"] == "0.1.0"


def test_a_message_before_hello_is_refused(tmp_path):
    client, _, _ = build(tmp_path)

    with client.websocket_connect("/ws/daemon") as socket:
        socket.send_text(Ack(config_version=1).model_dump_json())
        try:
            socket.receive_text()
        except Exception:
            return
    raise AssertionError("nothing is accepted from an unidentified socket")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_ws_daemon.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_server.pairing'`

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/pairing.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from ors_server.db import Database


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token(database: Database, name: str) -> tuple[int, str]:
    """Create a daemon record and a one-time token. Only the hash is stored."""
    token = secrets.token_urlsafe(24)
    with database.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO daemon (name, token_hash, status, created_at) VALUES (?, ?, 'unpaired', ?)",
            (name, _hash(token), datetime.now(UTC).isoformat()),
        )
    return int(cursor.lastrowid), token


def claim_token(database: Database, token: str) -> int | None:
    """Spend a token and return whose it was, or None. Constant-time compare."""
    candidate = _hash(token)
    with database.connect() as connection:
        for row in connection.execute(
            "SELECT id, token_hash FROM daemon WHERE token_hash IS NOT NULL"
        ):
            if hmac.compare_digest(row["token_hash"], candidate):
                connection.execute(
                    "UPDATE daemon SET token_hash = NULL, paired_at = ?, status = 'paired'"
                    " WHERE id = ?",
                    (datetime.now(UTC).isoformat(), row["id"]),
                )
                return int(row["id"])
    return None
```

`server/src/ors_server/link/ws_daemon.py`:

```python
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ors_schema.link import Ack, ConfigPush, Frame, Hello, Nack, SourceStatus, parse_daemon_message
from pydantic import ValidationError

from ors_server.pairing import claim_token
from ors_server.snapshot import build_snapshot, bump_config_version

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/daemon")
async def daemon_socket(socket: WebSocket) -> None:
    await socket.accept()
    state = socket.app.state
    connection = None
    daemon_id: int | None = None

    try:
        first = parse_daemon_message(await socket.receive_text())
        if not isinstance(first, Hello):
            await socket.close(code=4401, reason="hello first")
            return

        daemon_id = claim_token(state.database, first.token) or _already_paired(state, first)
        if daemon_id is None:
            await socket.close(code=4401, reason="unknown token")
            return

        _record_hello(state.database, daemon_id, first)
        connection = state.hub.register(daemon_id, socket.send_text)

        version = bump_config_version(state.database, daemon_id)
        snapshot = build_snapshot(state.database, state.secrets, daemon_id)
        await socket.send_text(ConfigPush(version=version, snapshot=snapshot).model_dump_json())

        while True:
            await _handle(state, daemon_id, parse_daemon_message(await socket.receive_text()))
    except WebSocketDisconnect:
        pass
    except ValidationError as exc:
        log.info("malformed message from a daemon", extra={"daemon": daemon_id, "error": str(exc)})
    finally:
        if connection is not None:
            state.hub.drop(connection)


def _already_paired(state, hello: Hello) -> int | None:
    """A reconnecting daemon presents the same token; its hash is gone by then."""
    with state.database.connect() as connection:
        row = connection.execute(
            "SELECT id FROM daemon WHERE name = ? AND status = 'paired'", (hello.hostname,)
        ).fetchone()
    return int(row["id"]) if row else None


def _record_hello(database, daemon_id: int, hello: Hello) -> None:
    import json

    with database.connect() as connection:
        connection.execute(
            "UPDATE daemon SET version = ?, capabilities = ?, last_seen = ?, status = 'paired'"
            " WHERE id = ?",
            (hello.daemon_version, json.dumps(hello.capabilities), datetime.now(UTC).isoformat(), daemon_id),
        )


async def _handle(state, daemon_id: int, message) -> None:
    if isinstance(message, Ack):
        state.hub.record_ack(daemon_id, message.config_version)
    elif isinstance(message, Nack):
        log.error(
            "daemon refused a snapshot",
            extra={"daemon": daemon_id, "version": message.config_version, "reason": message.reason},
        )
    elif isinstance(message, Frame):
        await state.hub.relay_frame(message)
    elif isinstance(message, SourceStatus):
        _touch(state.database, daemon_id)
    else:
        _touch(state.database, daemon_id)


def _touch(database, daemon_id: int) -> None:
    with database.connect() as connection:
        connection.execute(
            "UPDATE daemon SET last_seen = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), daemon_id),
        )
```

In `create_app`: `app.state.hub = Hub()`, `seed_builtin_templates(database)`, and `app.include_router(ws_daemon.router)` (outside the `/api` prefix).

**Note on `_already_paired`:** reconnection currently trusts the hostname, because the token's hash is deleted when it is spent. That is weaker than pairing and you should say so in your report. The intended fix is a persistent per-daemon key issued at pairing and presented on every connect — decide whether to build that here or to raise it for a follow-up task, and justify it.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): pairing and the daemon websocket"
```

---

### Task 9: The daemon's link client

**Files:**
- Create: `daemon/src/ors_daemon/link.py`
- Modify: `daemon/pyproject.toml` (add `websockets`)
- Test: `daemon/tests/test_link.py`

**Interfaces:**
- Consumes: `ors_schema.link`, `ors_daemon.config`
- Produces:
  - `LinkSettings(server_url: str, token: str, cache_path: Path)`
  - `LinkClient(settings, on_snapshot: Callable[[DaemonConfig, int], None], stop: threading.Event, clock, connect_factory=None)` with `.run()`, `.tick_once()`, `.heartbeat: float`, `.connected: bool`
  - `load_link_settings(path: Path) -> LinkSettings | None`
  - `write_link_settings(path: Path, settings: LinkSettings) -> None`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_link.py`:

```python
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from ors_schema.daemon import DaemonConfig
from ors_schema.link import Ack, ConfigPush, Hello, Nack, parse_daemon_message

from ors_daemon.clock import FakeClock
from ors_daemon.link import LinkClient, LinkSettings, load_link_settings, write_link_settings

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
CONFIG = {
    "version": 1,
    "timezone": "UTC",
    "integrations": [],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "text-only",
            "params": {"big": "hi"},
        }
    ],
}


class FakeSocket:
    """A scripted server: hands out messages, records what the client sends."""

    def __init__(self, inbound: list[str]) -> None:
        self.inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        if not self.inbound:
            raise ConnectionError("server went away")
        return self.inbound.pop(0)

    def close(self) -> None:
        self.closed = True


def make(tmp_path, inbound, applied=None):
    socket = FakeSocket(inbound)
    settings = LinkSettings(
        server_url="http://server:8080", token="tok", cache_path=tmp_path / "cache.json"
    )
    client = LinkClient(
        settings=settings,
        on_snapshot=applied if applied is not None else (lambda snapshot, version: None),
        stop=threading.Event(),
        clock=FakeClock(NOW),
        connect_factory=lambda url: socket,
    )
    return client, socket


def push(version: int = 7) -> str:
    return ConfigPush(version=version, snapshot=DaemonConfig.model_validate(CONFIG)).model_dump_json()


def test_the_client_says_hello_with_its_token(tmp_path):
    client, socket = make(tmp_path, [push()])
    client.tick_once()

    first = parse_daemon_message(socket.sent[0])
    assert isinstance(first, Hello)
    assert first.token == "tok"


def test_a_snapshot_is_applied_and_acked(tmp_path):
    applied: list[tuple[DaemonConfig, int]] = []
    client, socket = make(tmp_path, [push(7)], applied=lambda s, v: applied.append((s, v)))
    client.tick_once()

    assert applied and applied[0][1] == 7
    assert applied[0][0].screens[0].name == "CPU"
    assert any(isinstance(parse_daemon_message(m), Ack) for m in socket.sent)


def test_an_applied_snapshot_is_written_to_the_cache(tmp_path):
    client, _ = make(tmp_path, [push(7)])
    client.tick_once()

    cached = json.loads((tmp_path / "cache.json").read_text())
    assert cached["version"] == 7
    assert DaemonConfig.model_validate(cached["snapshot"]).screens[0].name == "CPU"


def test_a_snapshot_the_daemon_refuses_is_nacked_with_the_reason(tmp_path):
    broken = json.dumps(
        {"type": "config", "version": 8, "snapshot": {"version": 1, "screens": [{"nope": 1}]}}
    )

    def explode(snapshot, version):  # pragma: no cover - never reached
        raise AssertionError("an invalid snapshot must not reach the apply path")

    client, socket = make(tmp_path, [broken], applied=explode)
    client.tick_once()

    nacks = [m for m in socket.sent if parse_daemon_message(m).type == "nack"]
    assert nacks, "the server is told why, or it pushes the same broken config forever"
    assert isinstance(parse_daemon_message(nacks[0]), Nack)


def test_a_snapshot_that_the_apply_path_rejects_is_also_nacked(tmp_path):
    def explode(snapshot, version):
        raise ValueError("template 'nope' is not defined")

    client, socket = make(tmp_path, [push(9)], applied=explode)
    client.tick_once()

    nack = next(parse_daemon_message(m) for m in socket.sent if '"nack"' in m)
    assert "nope" in nack.reason


def test_a_server_that_goes_away_leaves_the_client_disconnected_not_dead(tmp_path):
    client, socket = make(tmp_path, [])
    client.tick_once()

    assert client.connected is False
    assert socket.closed is True


def test_the_cache_round_trips(tmp_path):
    path = tmp_path / "link.json"
    write_link_settings(path, LinkSettings(server_url="http://s", token="t", cache_path=path))

    loaded = load_link_settings(path)
    assert loaded is not None
    assert (loaded.server_url, loaded.token) == ("http://s", "t")


def test_no_link_settings_means_an_unpaired_daemon(tmp_path):
    assert load_link_settings(tmp_path / "absent.json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_link.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.link'`

- [ ] **Step 3: Write minimal implementation**

Add `websockets>=13.0` to `daemon/pyproject.toml` dependencies.

`daemon/src/ors_daemon/link.py`:

```python
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ors_schema.daemon import DaemonConfig
from ors_schema.link import Ack, ConfigPush, Hello, Nack, parse_server_message
from pydantic import ValidationError

from ors_daemon import __version__
from ors_daemon.clock import Clock

log = logging.getLogger(__name__)

SnapshotHandler = Callable[[DaemonConfig, int], None]


@dataclass(frozen=True)
class LinkSettings:
    server_url: str
    token: str
    cache_path: Path


def load_link_settings(path: Path) -> LinkSettings | None:
    """The link's own state, or None for a daemon nobody has paired."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return LinkSettings(
        server_url=raw["server_url"],
        token=raw["token"],
        cache_path=Path(raw.get("cache_path", str(Path(path).with_name("snapshot.json")))),
    )


def write_link_settings(path: Path, settings: LinkSettings) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "server_url": settings.server_url,
            "token": settings.token,
            "cache_path": str(settings.cache_path),
        }
    )
    # 0600 from the moment it exists: the token is what pairs this rack.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(payload)


class LinkClient(threading.Thread):
    """One socket to the server, reconnecting forever.

    The server going away is a normal state, not an error: the daemon keeps
    rendering from its cache throughout, and this thread's only job is to notice
    when the server is back and take whatever it has been given since.
    """

    def __init__(
        self,
        settings: LinkSettings,
        on_snapshot: SnapshotHandler,
        stop: threading.Event,
        clock: Clock,
        connect_factory: Callable[[str], Any] | None = None,
        backoff_cap: float = 30.0,
    ) -> None:
        super().__init__(name="link", daemon=True)
        self._settings = settings
        self._on_snapshot = on_snapshot
        self._stop_event = stop
        self._clock = clock
        self._connect = connect_factory or _default_connect
        self._backoff_cap = backoff_cap
        self._delay = 1.0
        self.connected = False
        self.heartbeat = 0.0

    def tick_once(self) -> None:
        """One connection attempt, run to its end. The unit the tests drive."""
        socket = None
        try:
            socket = self._connect(self._settings.server_url)
            socket.send(
                Hello(
                    token=self._settings.token,
                    hostname=os.uname().nodename,
                    daemon_version=__version__,
                ).model_dump_json()
            )
            self.connected = True
            self._delay = 1.0
            while not self._stop_event.is_set():
                self._receive(socket, socket.recv())
        except Exception as exc:
            log.info("link down", extra={"error": str(exc), "retry_in": self._delay})
        finally:
            self.connected = False
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    log.debug("closing a dead socket failed")

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.tick_once()
            self._stop_event.wait(self._delay)
            self._delay = min(self._backoff_cap, self._delay * 2)

    def _receive(self, socket: Any, raw: str) -> None:
        try:
            message = parse_server_message(raw)
        except ValidationError as exc:
            self._nack(socket, _version_of(raw), str(exc))
            return

        if not isinstance(message, ConfigPush):
            return

        try:
            self._on_snapshot(message.snapshot, message.version)
        except Exception as exc:
            self._nack(socket, message.version, str(exc))
            return

        self._write_cache(message)
        socket.send(Ack(config_version=message.version).model_dump_json())

    def _nack(self, socket: Any, version: int, reason: str) -> None:
        log.error("refused a snapshot", extra={"version": version, "reason": reason})
        socket.send(Nack(config_version=version, reason=reason[:500]).model_dump_json())

    def _write_cache(self, push: ConfigPush) -> None:
        path = self._settings.cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps({"version": push.version, "snapshot": push.snapshot.model_dump(mode="json")})
        )
        os.replace(temporary, path)


def _version_of(raw: str) -> int:
    try:
        return int(json.loads(raw).get("version", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _default_connect(url: str) -> Any:  # pragma: no cover - exercised on the rack
    from websockets.sync.client import connect

    return connect(url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/daemon")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add daemon uv.lock
git commit -m "feat(daemon): the link client"
```

---

### Task 10: Applying a snapshot in the daemon

**Files:**
- Modify: `daemon/src/ors_daemon/config.py`, `daemon/src/ors_daemon/supervisor.py`, `daemon/src/ors_daemon/__main__.py`
- Test: `daemon/tests/test_apply_snapshot.py`

**Interfaces:**
- Consumes: `LinkClient`, `resolve_screens`, `Supervisor`
- Produces:
  - `load_cached_snapshot(path: Path) -> tuple[DaemonConfig, int] | None`
  - `Supervisor.apply(config: DaemonConfig) -> None` — atomically swap the running config
  - `ors-daemon connect --server URL --token TOKEN` writing link settings

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_apply_snapshot.py`:

```python
import json
from pathlib import Path

from ors_schema.daemon import DaemonConfig

from ors_daemon.__main__ import main
from ors_daemon.config import load_cached_snapshot

CONFIG = {
    "version": 1,
    "timezone": "UTC",
    "night": {"enabled": False},
    "integrations": [],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "text-only",
            "params": {"big": "one"},
        }
    ],
}


def test_connect_writes_link_settings_a_run_can_find(tmp_path):
    link = tmp_path / "link.json"

    assert main(["connect", "--server", "http://s:8080", "--token", "tok", "--link", str(link)]) == 0

    written = json.loads(link.read_text())
    assert written["server_url"] == "http://s:8080"
    assert written["token"] == "tok"


def test_connect_refuses_to_overwrite_an_existing_pairing_without_force(tmp_path, capsys):
    link = tmp_path / "link.json"
    main(["connect", "--server", "http://s", "--token", "a", "--link", str(link)])

    assert main(["connect", "--server", "http://s", "--token", "b", "--link", str(link)]) == 1
    assert "already paired" in capsys.readouterr().err


def test_a_cached_snapshot_is_loaded_with_its_version(tmp_path):
    cache = tmp_path / "snapshot.json"
    cache.write_text(json.dumps({"version": 12, "snapshot": CONFIG}))

    loaded = load_cached_snapshot(cache)

    assert loaded is not None
    config, version = loaded
    assert version == 12
    assert config.screens[0].name == "CPU"


def test_a_corrupt_cache_is_no_cache_rather_than_a_crash(tmp_path):
    cache = tmp_path / "snapshot.json"
    cache.write_text("{ not json")

    assert load_cached_snapshot(cache) is None


def test_a_cache_the_schema_rejects_is_no_cache(tmp_path):
    cache = tmp_path / "snapshot.json"
    cache.write_text(json.dumps({"version": 1, "snapshot": {"screens": [{"rotation": 45}]}}))

    assert load_cached_snapshot(cache) is None
```

Plus, in `daemon/tests/test_supervisor.py`:

```python
def test_apply_swaps_the_running_config_and_restarts_the_screens(tmp_path: Path) -> None:
    """A pushed snapshot reaches the glass without restarting the process."""
    supervisor, _, displays = make(tmp_path, screens=1)
    supervisor.start()
    try:
        before = supervisor.workers[0]
        replacement = DaemonConfig.model_validate(
            {
                **config_dict(tmp_path, screens=1),
                "screens": [
                    {
                        **config_dict(tmp_path, screens=1)["screens"][0],
                        "name": "RENAMED",
                    }
                ],
            }
        )

        supervisor.apply(replacement)

        assert [worker.screen_name for worker in supervisor.workers] == ["RENAMED"]
        assert before.is_alive() is False, "the old worker is stopped, not orphaned"
    finally:
        supervisor.stop()


def test_apply_leaves_the_previous_config_running_when_the_new_one_is_unusable(tmp_path: Path) -> None:
    supervisor, _, _ = make(tmp_path, screens=1)
    supervisor.start()
    try:
        broken = DaemonConfig.model_validate(
            {**config_dict(tmp_path, screens=1), "screens": [
                {**config_dict(tmp_path, screens=1)["screens"][0], "template": "no-such-template"}
            ]}
        )

        with pytest.raises(ConfigError):
            supervisor.apply(broken)

        assert [w.screen_name for w in supervisor.workers] == ["S1"], "the rack keeps running"
    finally:
        supervisor.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_apply_snapshot.py -q; echo "exit=$?"`
Expected: FAIL — `ImportError: cannot import name 'load_cached_snapshot'`

- [ ] **Step 3: Write minimal implementation**

In `daemon/src/ors_daemon/config.py`:

```python
def load_cached_snapshot(path: Path) -> tuple[DaemonConfig, int] | None:
    """The last snapshot the server pushed, or None if there is nothing usable.

    A daemon boots from this when the server is unreachable, which is the whole
    reason a server outage does not darken the rack. Anything unreadable or
    unvalidatable is treated as absent rather than fatal: a corrupt cache must
    not be the thing that stops a rack from starting.
    """
    try:
        raw = json.loads(Path(path).read_text())
        return DaemonConfig.model_validate(raw["snapshot"]), int(raw["version"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError):
        log.warning("ignoring an unusable snapshot cache", extra={"path": str(path)})
        return None
```

In `Supervisor`, an `apply` that resolves the new config *before* touching anything running:

```python
    def apply(self, config: DaemonConfig) -> None:
        """Swap to a new config atomically. Raises without disturbing the rack.

        Resolution happens first and against the *new* config alone: a snapshot
        naming a template that does not exist, or a screen the schema refuses,
        must be discovered while the current one is still driving the panels.
        Only once the replacement is known-good is anything stopped.
        """
        replacement = resolve_screens(config)  # raises ConfigError; nothing has moved yet

        with self._shutdown_lock:
            for slot in list(self._slots):
                slot.panel.revoke()
                if slot.worker is not None:
                    slot.worker.request_stop()
                    slot.worker.join(timeout=_JOIN_TIMEOUT)
                self._shut_down_panel(slot.panel.backend, slot.screen.config.name)
            self._slots.clear()
            self._unavailable.clear()
            self._config = config
            self._screens = replacement

        for screen in self._screens:
            self._open_panel(screen)
        for slot in list(self._slots):
            self._start_worker(slot)
```

**Note for the implementer:** `ScreenWorker` has no `request_stop`; it waits on the supervisor's shared stop event, which cannot be set for a reconfigure without stopping everything. Add a per-worker stop that `run` also honours, or give each generation of workers its own `threading.Event` that the supervisor holds. Pick one, and say why — this is the crux of the task and the tests above pin the behaviour, not the mechanism.

In `__main__.py`, a `connect` subcommand that writes link settings, refuses to overwrite an existing pairing without `--force`, and a `run` that: loads link settings if present; boots from the cached snapshot when there is one, else the file; starts a `LinkClient` whose `on_snapshot` calls `supervisor.apply`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): apply a pushed snapshot without restarting"
```

---

### Task 11: Frames from the daemon

**Files:**
- Create: `daemon/src/ors_daemon/frames.py`
- Modify: `daemon/src/ors_daemon/screen.py`, `daemon/src/ors_daemon/supervisor.py`
- Test: `daemon/tests/test_frames.py`

**Interfaces:**
- Consumes: `ScreenWorker`, `Frame`
- Produces:
  - `FrameStream(fps: float, clock)` with `.enable(screen_ids: set[int])`, `.disable()`, `.offer(screen_id: int, image: Image.Image) -> Frame | None`
  - `ScreenWorker.on_frame: Callable[[Image.Image], None] | None`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_frames.py`:

```python
from datetime import UTC, datetime

from PIL import Image

from ors_daemon.clock import FakeClock
from ors_daemon.frames import FrameStream

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def panel() -> Image.Image:
    return Image.new("RGB", (240, 240), (0, 128, 255))


def test_nothing_is_encoded_while_disabled():
    stream = FrameStream(fps=2.0, clock=FakeClock(NOW))

    assert stream.offer(1, panel()) is None


def test_an_enabled_screen_produces_a_webp_frame():
    clock = FakeClock(NOW)
    stream = FrameStream(fps=2.0, clock=clock)
    stream.enable({1})

    frame = stream.offer(1, panel())

    assert frame is not None
    assert frame.screen_id == 1
    assert frame.webp[:4] == b"RIFF" and frame.webp[8:12] == b"WEBP"


def test_a_screen_nobody_asked_for_is_not_encoded():
    stream = FrameStream(fps=2.0, clock=FakeClock(NOW))
    stream.enable({1})

    assert stream.offer(2, panel()) is None


def test_the_rate_is_capped():
    clock = FakeClock(NOW)
    stream = FrameStream(fps=2.0, clock=clock)
    stream.enable({1})

    assert stream.offer(1, panel()) is not None
    assert stream.offer(1, panel()) is None, "two frames inside one interval is one frame"

    clock.advance(0.6)
    assert stream.offer(1, panel()) is not None


def test_sequence_numbers_increase_per_screen():
    clock = FakeClock(NOW)
    stream = FrameStream(fps=100.0, clock=clock)
    stream.enable({1, 2})

    first = stream.offer(1, panel())
    clock.advance(1.0)
    second = stream.offer(1, panel())
    other = stream.offer(2, panel())

    assert second.seq == first.seq + 1
    assert other.seq == first.seq, "each screen counts its own frames"


def test_disabling_stops_encoding_again():
    clock = FakeClock(NOW)
    stream = FrameStream(fps=100.0, clock=clock)
    stream.enable({1})
    stream.disable()

    assert stream.offer(1, panel()) is None


def test_a_frame_is_small_enough_to_stream():
    clock = FakeClock(NOW)
    stream = FrameStream(fps=2.0, clock=clock)
    stream.enable({1})

    frame = stream.offer(1, panel())

    assert len(frame.webp) < 20_000, "four panels at 2fps must not saturate a Pi's uplink"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_frames.py -q; echo "exit=$?"`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.frames'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/frames.py`:

```python
from __future__ import annotations

import io
import threading

from ors_schema.link import Frame
from PIL import Image

from ors_daemon.clock import Clock

_QUALITY = 60


class FrameStream:
    """Encodes panels for the browser, and only when the browser is looking.

    A Pi 3B+ is already rendering four panels and packing them to RGB565; a
    WebP encode per panel per frame on top of that is not free, so the default
    is off and the server has to ask. `offer` returning None is the normal case.
    """

    def __init__(self, fps: float, clock: Clock) -> None:
        self._fps = fps
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled: set[int] = set()
        self._last: dict[int, float] = {}
        self._seq: dict[int, int] = {}

    def enable(self, screen_ids: set[int], fps: float | None = None) -> None:
        with self._lock:
            self._enabled = set(screen_ids)
            if fps is not None:
                self._fps = fps

    def disable(self) -> None:
        with self._lock:
            self._enabled.clear()

    def offer(self, screen_id: int, image: Image.Image) -> Frame | None:
        """Encode this panel if anyone is watching it and the rate allows."""
        with self._lock:
            if screen_id not in self._enabled:
                return None
            now = self._clock().timestamp()
            interval = 1.0 / self._fps if self._fps > 0 else 0.0
            if now - self._last.get(screen_id, float("-inf")) < interval:
                return None
            self._last[screen_id] = now
            seq = self._seq.get(screen_id, 0)
            self._seq[screen_id] = seq + 1

        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=_QUALITY, method=0)
        return Frame(screen_id=screen_id, seq=seq, webp=buffer.getvalue())
```

`ScreenWorker` gains an optional `on_frame` callback, invoked with the finished image inside `_show` **after** a successful `show` — so a frame is only ever sent for something that actually reached the glass. The supervisor wires it to `FrameStream.offer` and hands the result to the link client.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): encode frames on demand"
```

---

### Task 12: `/ws/ui` — status and frame subscriptions

**Files:**
- Create: `server/src/ors_server/link/ws_ui.py`
- Modify: `server/src/ors_server/app.py`
- Test: `server/tests/test_ws_ui.py`

**Interfaces:**
- Consumes: `Hub`, `require_session`
- Produces: `WS /ws/ui` accepting `{"action": "subscribe"|"unsubscribe", "screen_id": int}` and emitting `{"type": "frame", ...}` and `{"type": "daemons", ...}`

- [ ] **Step 1: Write the failing test**

`server/tests/test_ws_ui.py`:

```python
import base64
import json

import pytest
from fastapi.testclient import TestClient
from ors_schema.link import Frame

from ors_server.app import AppSettings, create_app
from ors_server.pairing import mint_token


def logged_in(tmp_path) -> TestClient:
    app = create_app(AppSettings(data_dir=tmp_path))
    client = TestClient(app)
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    mint_token(app.state.database, "pi-rack")
    return client


def test_the_socket_refuses_an_unauthenticated_browser(tmp_path):
    app = create_app(AppSettings(data_dir=tmp_path))
    client = TestClient(app)
    client.post("/api/auth/setup", json={"password": "pw"})

    with pytest.raises(Exception):
        with client.websocket_connect("/ws/ui") as socket:
            socket.receive_text()


def test_subscribing_delivers_frames_for_that_screen(tmp_path):
    client = logged_in(tmp_path)

    with client.websocket_connect("/ws/ui") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "screen_id": 1}))
        client.portal.call(client.app.state.hub.relay_frame, Frame(screen_id=1, seq=3, webp=b"RIFF"))

        while True:
            message = json.loads(socket.receive_text())
            if message["type"] == "frame":
                break

    assert message["screen_id"] == 1
    assert base64.b64decode(message["webp"]) == b"RIFF"


def test_unsubscribing_stops_them(tmp_path):
    client = logged_in(tmp_path)

    with client.websocket_connect("/ws/ui") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "screen_id": 1}))
        socket.send_text(json.dumps({"action": "unsubscribe", "screen_id": 1}))

        assert client.app.state.hub.watched_screens() == set()


def test_a_disconnect_releases_the_subscription(tmp_path):
    client = logged_in(tmp_path)

    with client.websocket_connect("/ws/ui") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "screen_id": 1}))

    assert client.app.state.hub.watched_screens() == set(), (
        "a closed tab must stop the daemon encoding, or it encodes forever"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_ws_ui.py -q; echo "exit=$?"`
Expected: FAIL — no `/ws/ui` route.

- [ ] **Step 3: Write minimal implementation**

`server/src/ors_server/link/ws_ui.py` — accept the socket only when the session cookie is valid; keep one `asyncio.Queue` per connection; on `subscribe`, register with the hub and, if it is the first watcher, send that screen's daemon a `FramesRequest(enabled=True, screen_ids=[...])`; on `unsubscribe` or disconnect, deregister and, if it was the last, send `FramesRequest(enabled=False)`. Frames are emitted as JSON with `webp` base64-encoded, matching `Frame.model_dump_json()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest server/tests -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): the browser websocket and frame subscriptions"
```

---

### Task 13: The configuration API

**Files:**
- Create: `server/src/ors_server/api/daemons.py`, `screens.py`, `templates.py`, `integrations.py`, `settings.py`
- Modify: `server/src/ors_server/app.py`
- Test: `server/tests/test_api_daemons.py`, `server/tests/test_api_screens.py`

**Interfaces:**
- Consumes: `Database`, `Hub`, `build_snapshot`, `bump_config_version`, `require_session`
- Produces: the endpoints listed in the spec's §7.1, each requiring a session, and **every mutation pushing a fresh snapshot to the affected daemon**

- [ ] **Step 1: Write the failing test**

`server/tests/test_api_daemons.py`:

```python
import pytest
from fastapi.testclient import TestClient

from ors_server.app import AppSettings, create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    return client


def test_every_daemon_route_refuses_an_unauthenticated_caller(tmp_path):
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))

    assert anonymous.get("/api/daemons").status_code == 401
    assert anonymous.post("/api/daemons", json={"name": "x"}).status_code == 401


def test_creating_a_daemon_returns_its_token_exactly_once(client):
    created = client.post("/api/daemons", json={"name": "pi-rack"}).json()

    assert created["token"], "the token is shown once, at creation"
    listed = client.get("/api/daemons").json()
    assert "token" not in listed[0], "and never again, from any route"


def test_a_daemon_reports_offline_until_it_connects(client):
    client.post("/api/daemons", json={"name": "pi-rack"})

    assert client.get("/api/daemons").json()[0]["online"] is False


def test_deleting_a_daemon_takes_its_screens_with_it(client):
    daemon_id = client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]
    client.post(
        "/api/screens",
        json={
            "daemon_id": daemon_id,
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "ring-gauge",
            "params": {},
        },
    )

    client.delete(f"/api/daemons/{daemon_id}")

    assert client.get("/api/screens").json() == []


def test_rotating_a_key_returns_a_new_token_and_unpairs(client):
    daemon_id = client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]

    rotated = client.post(f"/api/daemons/{daemon_id}/rotate-key").json()

    assert rotated["token"]
    assert client.get("/api/daemons").json()[0]["status"] == "unpaired"
```

`server/tests/test_api_screens.py`:

```python
import pytest
from fastapi.testclient import TestClient

from ors_server.app import AppSettings, create_app

SCREEN = {
    "name": "CPU",
    "position": 1,
    "display": {"backend": "virtual", "out_dir": "/tmp/p"},
    "template": "ring-gauge",
    "params": {"title": "CPU"},
}


@pytest.fixture
def client_and_daemon(tmp_path) -> tuple[TestClient, int]:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    daemon_id = client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]
    return client, daemon_id


def version_of(client: TestClient, daemon_id: int) -> int:
    return next(d for d in client.get("/api/daemons").json() if d["id"] == daemon_id)["config_version"]


def test_creating_a_screen_bumps_the_daemons_config_version(client_and_daemon):
    client, daemon_id = client_and_daemon
    before = version_of(client, daemon_id)

    created = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id})

    assert created.status_code == 201
    assert version_of(client, daemon_id) > before, "an unchanged version means nothing is pushed"


def test_patching_a_screen_bumps_it_again(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id}).json()["id"]
    before = version_of(client, daemon_id)

    assert client.patch(f"/api/screens/{screen_id}", json={"rotation": 90}).status_code == 200
    assert version_of(client, daemon_id) > before


def test_an_edit_for_an_offline_daemon_still_saves(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id}).json()["id"]

    client.patch(f"/api/screens/{screen_id}", json={"rotation": 180})

    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 180


def test_a_rotation_the_schema_refuses_is_a_422_and_changes_nothing(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id}).json()["id"]
    before = version_of(client, daemon_id)

    assert client.patch(f"/api/screens/{screen_id}", json={"rotation": 45}).status_code == 422
    assert client.get(f"/api/screens/{screen_id}").json()["rotation"] == 0
    assert version_of(client, daemon_id) == before, "a refused edit must not look like a change"


def test_reorder_renumbers_positions(client_and_daemon):
    client, daemon_id = client_and_daemon
    first = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id}).json()["id"]
    second = client.post(
        "/api/screens", json={**SCREEN, "daemon_id": daemon_id, "name": "MEM", "position": 2}
    ).json()["id"]

    client.post("/api/screens/reorder", json={"ids": [second, first]})

    positions = {s["name"]: s["position"] for s in client.get("/api/screens").json()}
    assert positions == {"MEM": 1, "CPU": 2}


def test_preview_renders_a_png_without_a_daemon(client_and_daemon):
    client, daemon_id = client_and_daemon
    screen_id = client.post("/api/screens", json={**SCREEN, "daemon_id": daemon_id}).json()["id"]

    response = client.get(f"/api/screens/{screen_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_api_daemons.py -q; echo "exit=$?"`
Expected: FAIL — the routes do not exist.

- [ ] **Step 3: Write minimal implementation**

Each router follows one shape: validate the body with a pydantic model, write the row, then

```python
    version = bump_config_version(database, daemon_id)
    snapshot = build_snapshot(database, secrets, daemon_id)
    await hub.push_config(daemon_id, ConfigPush(version=version, snapshot=snapshot))
```

Assembling the snapshot *before* pushing means a change the daemon would refuse is caught by the server's own validation, in the request that made it, rather than as a nack seconds later.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q; echo "exit=$?"`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): the configuration API"
```

---

### Task 14: Docker image and compose files

**Files:**
- Create: `deploy/Dockerfile`, `deploy/compose.pi.yaml`, `deploy/compose.remote.yaml`, `server/README.md`
- Test: `server/tests/test_deploy.py`

**Interfaces:**
- Consumes: everything above
- Produces: an image serving the API on `:8080` with its database in a volume

- [ ] **Step 1: Write the failing test**

`server/tests/test_deploy.py` parses the compose files and asserts what actually matters and can be checked without Docker: the data volume is mounted so the database survives a container restart; `ORS_SECRET_KEY` is passed through rather than baked in; the published port matches the server's default; the Pi compose file does *not* try to run the daemon in a container; and the Dockerfile's final stage runs as a non-root user.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest server/tests/test_deploy.py -q; echo "exit=$?"`
Expected: FAIL — the files do not exist.

- [ ] **Step 3: Write minimal implementation**

A multi-stage `Dockerfile`: a Node stage that builds the SPA (a placeholder `web/` in this plan; M3b fills it), then a Python stage that installs the workspace and copies the built assets. `compose.pi.yaml` runs only the server, with a note that the daemon stays a host systemd unit and why. `compose.remote.yaml` is the same server with the daemon's host reaching it over the LAN.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q; echo "exit=$?"` and, on a machine with Docker, `docker build -f deploy/Dockerfile .`
Expected: PASS, exit 0; the image builds.

- [ ] **Step 5: Commit**

```bash
git add deploy server
git commit -m "feat(deploy): server image and compose files"
```

---

## Definition of done for M3a

- `uv run pytest` passes from a clean checkout with no hardware; ruff clean; CI green.
- A daemon paired with a minted token connects, receives a snapshot, applies it and acks.
- A screen edited through the API reaches a connected daemon and changes what it renders.
- Stopping the server leaves the daemon rendering; restarting it reconnects with no re-push when the versions match.
- A snapshot the daemon refuses is nacked with a reason, and the previous config keeps running.
- Frames flow only while `/ws/ui` has a subscriber.
- `docker build` produces an image that serves the API and persists its database.

## What M3b picks up

M3b (the interface) consumes: the API in §7.1, the `/ws/ui` socket, and the OpenAPI schema at `/api/openapi.json` for generated types. No server change should be needed to build it — if M3b finds one, that is a signal this API was wrong, and it should be raised rather than patched around.
