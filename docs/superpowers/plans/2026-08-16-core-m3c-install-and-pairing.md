# M3c Install and Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenRackScreen installable from PyPI, give the daemon an `install`
command that takes a bare Raspberry Pi to a running service, and let a rack join
a server by being approved in the web interface instead of by pasting a token.

**Architecture:** Five distributions published at one lockstep version, with the
built interface shipped inside the `ors-server` wheel so a pip install serves
pages. `ors-daemon install` owns every mutation a Pi needs — user, groups,
directories, `config.txt`, a venv at `/opt/openrackscreen`, the unit — behind
injected filesystem roots so the tests never touch the machine running them.
Pairing inverts: the server announces over mDNS, an unpaired daemon files an
unauthenticated claim carrying a fingerprint and an ephemeral X25519 public key,
and an authenticated admin approves it in the interface after matching a
six-character code.

**Tech Stack:** Python 3.11+, uv workspace, hatchling, FastAPI, pydantic 2,
`zeroconf` (new), `cryptography` (new for the daemon, existing for the server),
React 19 + TypeScript 6 + Tailwind 4 + shadcn/Radix, Vitest 4, Playwright.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-16-core-m3c-install-and-pairing-design.md`. Where this plan and the spec disagree, **the spec wins** — say so in the review rather than implementing the plan's version.
- Distribution names are `ors-schema`, `ors-render`, `ors-daemon`, `ors-server`, `openrackscreen`. Import names are unchanged.
- All five distributions carry **one shared version**; every intra-project dependency pins it with `==`.
- Target version for this milestone: **`0.2.0`**.
- `requires-python = ">=3.11"` everywhere. Do not raise it.
- **No test may read or write outside `tmp_path`.** Install code is parameterised on `etc_root`, `boot_root`, `state_root`, `prefix`; subprocess calls go through an injected runner.
- Ruff: `line-length = 100`, `select = ["E", "F", "I", "UP", "B"]`. Run `uv run ruff check .` and `uv run ruff format --check .` before every commit.
- Python tests: `uv run pytest`. Per-test timeout is 10s and is not to be raised.
- Web: `pnpm test`, `pnpm typecheck`, `pnpm lint` (oxlint, `--max-warnings=0`), `pnpm build`. Not ESLint.
- The daemon must not gain a dependency that lacks a `manylinux` **aarch64** wheel. `linux/arm/v7` is already unsupported.
- Every new constant that a test could pin must be pinned by a test. A surviving mutant means "no test drove the state where this matters", not "this does not matter".
- Commit after every task. Never `git checkout --` with uncommitted work in the tree.

---

## File Structure

**New files**

| Path | Responsibility |
| --- | --- |
| `tools/version.py` | Rewrites the version and every intra-project pin across all five `pyproject.toml` files. |
| `tests/test_packaging.py` | Fails if versions or pins disagree, or if a wheel is missing the interface. |
| `packages/openrackscreen/pyproject.toml` | The squat-blocker meta-package. No modules. |
| `daemon/src/ors_daemon/install.py` | Every machine mutation: user, groups, directories, venv, unit, enable. Pure functions over injected roots. |
| `daemon/src/ors_daemon/boot_config.py` | The `config.txt` reader/editor. Separate from `install.py` because it is the one step with an SD-reader failure mode and deserves its own test file. |
| `daemon/src/ors_daemon/identity.py` | The install identity: secret, fingerprint, short code. |
| `daemon/src/ors_daemon/discovery.py` | mDNS browsing, `zeroconf` behind one seam so tests stub one class. |
| `daemon/src/ors_daemon/join.py` | The claim client: file a claim, poll it, decrypt the key. |
| `daemon/tests/test_install.py`, `test_boot_config.py`, `test_identity.py`, `test_discovery.py`, `test_join.py` | One per module above. |
| `server/src/ors_server/claims.py` | The pending-claim store: create, list, approve, deny, expire, suppress. |
| `server/src/ors_server/api/claims.py` | The three routes. |
| `server/src/ors_server/announce.py` | mDNS service registration. |
| `server/src/ors_server/limiter.py` | The rolling-window limiter, extracted from `Sessions`. |
| `server/tests/test_claims.py`, `test_api_claims.py`, `test_announce.py`, `test_limiter.py` | One per module above. |
| `web/src/routes/daemons/PendingClaims.tsx` | The "Waiting to join" list. |
| `web/src/routes/daemons/ApproveClaimDialog.tsx` | Approve, with the short code and what it grants. |
| `web/src/routes/daemons/DenyClaimDialog.tsx` | Deny, with what suppression means. |
| `deploy/compose.image.yaml` | The `image:` override, so a checkout builds itself. |
| `.github/workflows/release.yml` | Tag → build five distributions → Trusted Publishing. |

**Modified files**

| Path | Change |
| --- | --- |
| `pyproject.toml` | `testpaths` gains `tests`; workspace members gain `packages/openrackscreen`. |
| `daemon/pyproject.toml`, `server/pyproject.toml`, `packages/*/pyproject.toml` | Version `0.2.0`, `==` cross-pins, new dependencies. |
| `daemon/src/ors_daemon/__main__.py` | `--config` optional; `install`/`uninstall` subcommands; the join flow. |
| `daemon/examples/openrackscreen.service` | `ExecStart` moves to `/opt/openrackscreen/bin/ors-daemon` and loses `--config`. |
| `server/src/ors_server/__main__.py` | `web_dir` and `data_dir` defaults; the `install` subcommand. |
| `server/src/ors_server/app.py` | Mounts the claims router; starts and stops the announcer. |
| `server/src/ors_server/api/auth.py` | Uses the extracted limiter. |
| `server/src/ors_server/db.py` | The `claim` table. |
| `deploy/Dockerfile`, `deploy/compose.pi.yaml`, `deploy/compose.remote.yaml` | Explicit `ORS_WEB_DIR`/`ORS_DATA_DIR`; `image:` removed. |
| `web/src/api/queries.ts`, `web/src/routes/daemons/DaemonsPage.tsx` | Claim queries and the new section. |
| `README.md`, `daemon/README.md`, `server/README.md` | Rewritten install and first-run sections. |

---

## Task 1: Lockstep versions and the test that enforces them

**Files:**
- Create: `tools/version.py`, `tests/test_packaging.py`
- Modify: `pyproject.toml` (testpaths), `daemon/pyproject.toml`, `server/pyproject.toml`, `packages/ors-schema/pyproject.toml`, `packages/ors-render/pyproject.toml`

**Interfaces:**
- Produces: `tools.version.PACKAGES: dict[str, Path]`, `tools.version.rewrite(version: str) -> list[Path]`, `tools.version.read_versions() -> dict[str, str]`.

- [ ] **Step 1: Add a root test directory to `testpaths`**

In `pyproject.toml`, under `[tool.pytest.ini_options]`:

```toml
testpaths = [
    "tests",
    "packages/ors-schema/tests",
    "packages/ors-render/tests",
    "daemon/tests",
    "server/tests",
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_packaging.py`:

```python
"""The five distributions, and the one number they all have to agree on.

`ors-daemon` declares `Requires-Dist: ors-schema` today with no version at all,
which lets pip pair a new daemon with an old schema. The link protocol between
them is the one place in this project where a mismatch is silent: a daemon that
half-understands a snapshot draws a rack rather than refusing one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PACKAGES = {
    "ors-schema": ROOT / "packages/ors-schema/pyproject.toml",
    "ors-render": ROOT / "packages/ors-render/pyproject.toml",
    "ors-daemon": ROOT / "daemon/pyproject.toml",
    "ors-server": ROOT / "server/pyproject.toml",
    "openrackscreen": ROOT / "packages/openrackscreen/pyproject.toml",
}
"""Every distribution this repository publishes. A package added here and
nowhere else fails the first test below, which is the point: the failure is
`FileNotFoundError` naming the file somebody forgot to write."""


def _document(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def test_every_published_package_has_a_pyproject():
    for name, path in PACKAGES.items():
        assert path.is_file(), f"{name} is published but {path} does not exist"


def test_all_five_versions_agree():
    versions = {name: _document(path)["project"]["version"] for name, path in PACKAGES.items()}
    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_intra_project_dependencies_pin_the_shared_version(name: str):
    # Not `>=`: the failure this guards is a *newer* schema under an older
    # daemon as much as the reverse, and only `==` refuses both.
    document = _document(PACKAGES[name])
    version = document["project"]["version"]
    for requirement in document["project"].get("dependencies", []):
        for other in PACKAGES:
            if requirement.startswith(other):
                assert requirement == f"{other}=={version}", (
                    f"{name} requires {requirement!r}, expected {other}=={version}"
                )
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_packaging.py -v`
Expected: `test_every_published_package_has_a_pyproject` FAILS —
`openrackscreen is published but .../packages/openrackscreen/pyproject.toml does not exist`.
The other two fail on unpinned bare `"ors-schema"` requirements.

- [ ] **Step 4: Write `tools/version.py`**

```python
"""Set one version across every published distribution, pins included.

A script rather than `hatch-vcs`, because the cross-pins have to move with the
version and a dynamic version cannot be written into another package's
`dependencies` at build time. The script is the convenience; the test in
`tests/test_packaging.py` is the mechanism, and it is what fails if somebody
edits one file by hand.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PACKAGES = {
    "ors-schema": ROOT / "packages/ors-schema/pyproject.toml",
    "ors-render": ROOT / "packages/ors-render/pyproject.toml",
    "ors-daemon": ROOT / "daemon/pyproject.toml",
    "ors-server": ROOT / "server/pyproject.toml",
    "openrackscreen": ROOT / "packages/openrackscreen/pyproject.toml",
}

_VERSION = re.compile(r'^version = "[^"]*"$', re.MULTILINE)


def _pin(name: str) -> re.Pattern[str]:
    # Anchored on the quote so `ors-schema` cannot match `ors-schema-extra`,
    # and tolerant of an existing pin so the script is re-runnable.
    return re.compile(rf'"{re.escape(name)}(==[^"]*)?"')


def rewrite(version: str) -> list[Path]:
    """Set `version` everywhere and return the files that changed."""
    changed = []
    for path in PACKAGES.values():
        text = path.read_text()
        updated = _VERSION.sub(f'version = "{version}"', text, count=1)
        for name in PACKAGES:
            updated = _pin(name).sub(f'"{name}=={version}"', updated)
        if updated != text:
            path.write_text(updated)
            changed.append(path)
    return changed


def read_versions() -> dict[str, str]:
    import tomllib

    return {
        name: tomllib.loads(path.read_text())["project"]["version"]
        for name, path in PACKAGES.items()
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python tools/version.py <version>", file=sys.stderr)
        return 2
    for path in rewrite(argv[0]):
        print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

Note the `_pin` regex substitutes the *version line first*, then pins. A
package's own `version = "0.2.0"` line is not quoted as a bare name, so the pin
pass cannot corrupt it.

- [ ] **Step 5: Create the meta-package so the first test can pass**

Create `packages/openrackscreen/pyproject.toml`:

```toml
[project]
name = "openrackscreen"
version = "0.2.0"
description = "OpenRackScreen: configurable monitoring displays for server racks"
readme = "README.md"
requires-python = ">=3.11"
# Deliberately empty of modules. This distribution exists so the project's own
# name on PyPI belongs to the project, and so that `pip install openrackscreen`
# gets somebody the two things they actually wanted rather than a 404.
dependencies = ["ors-daemon==0.2.0", "ors-server==0.2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
# No package to include; hatchling needs telling that on purpose.
bypass-selection = true

[tool.uv.sources]
ors-daemon = { workspace = true }
ors-server = { workspace = true }
```

Create `packages/openrackscreen/README.md`:

```markdown
# OpenRackScreen

Configurable monitoring displays for server racks.

This distribution installs both halves of the project:

- `ors-server` — owns the configuration and serves the web interface.
- `ors-daemon` — runs on the Raspberry Pi and drives the panels.

Most people want one or the other. See
<https://github.com/SilkePilon/OpenRackScreen>.
```

- [ ] **Step 6: Add it to the workspace**

In the root `pyproject.toml`, the `packages/*` glob already covers it. Add the
source so the workspace resolves:

```toml
[tool.uv.sources]
ors-schema = { workspace = true }
ors-render = { workspace = true }
ors-daemon = { workspace = true }
ors-server = { workspace = true }
```

(unchanged — `openrackscreen` is a member, not a dependency of the root).

- [ ] **Step 7: Run the rewrite**

Run: `uv run python tools/version.py 0.2.0`
Expected: prints five `updated ...` lines.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_packaging.py -v && uv run pytest`
Expected: packaging tests PASS; the full suite still passes (2177 passed, 1 skipped, plus the new ones).

- [ ] **Step 9: Re-lock and commit**

```bash
uv lock
uv run ruff check . && uv run ruff format --check .
git add pyproject.toml uv.lock tools/ tests/ packages/ daemon/pyproject.toml server/pyproject.toml
git commit -m "build: one version across five distributions, and the test that keeps it"
```

---

## Task 2: The interface ships inside the server wheel

**Files:**
- Modify: `server/src/ors_server/__main__.py`, `server/pyproject.toml`, `.gitignore`
- Test: `server/tests/test_main.py`

**Interfaces:**
- Produces: `ors_server.__main__.packaged_web_dir() -> Path`, and `web_dir` resolution order `ORS_WEB_DIR` → packaged.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_main.py`:

```python
def test_the_web_directory_defaults_to_the_one_inside_the_wheel(monkeypatch):
    """Not `/app/web`, which exists only in the container.

    A pip-installed server whose `web_dir` pointed at a container path would
    serve `/api/*`, pass its health check, and answer 404 for every page --
    the exact shape of the failure a stale published image already produced
    on this project once, and one nobody diagnoses quickly, because the
    server looks healthy from every angle except a browser.
    """
    from ors_server.__main__ import packaged_web_dir, resolve_web_dir

    monkeypatch.delenv("ORS_WEB_DIR", raising=False)
    assert resolve_web_dir() == packaged_web_dir()
    # And it is inside the installed package, not beside the repository.
    assert packaged_web_dir().name == "web"
    assert packaged_web_dir().parent.name == "ors_server"


def test_the_environment_still_wins_over_the_packaged_directory(monkeypatch, tmp_path):
    """The container sets it explicitly, and must keep winning.

    Pinned because the packaged default is the *new* behaviour: a resolution
    order that consulted the wheel first would make `ORS_WEB_DIR` dead in
    every deployment that sets it, and the Dockerfile is one of those.
    """
    from ors_server.__main__ import resolve_web_dir

    monkeypatch.setenv("ORS_WEB_DIR", str(tmp_path))
    assert resolve_web_dir() == tmp_path
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest server/tests/test_main.py -k web_directory -v`
Expected: FAIL — `ImportError: cannot import name 'packaged_web_dir'`.

- [ ] **Step 3: Implement**

In `server/src/ors_server/__main__.py`, add above `main`:

```python
def packaged_web_dir() -> Path:
    """The built interface, as shipped inside the wheel.

    `Path(__file__).parent` and not `importlib.resources`: the directory is
    handed to starlette's `StaticFiles`, which wants a real path on a real
    filesystem, and this project is never installed from a zipimport.
    """
    return Path(__file__).resolve().parent / "web"


def resolve_web_dir() -> Path:
    """Where the built interface is. The environment first, the wheel second.

    In that order because the container sets `ORS_WEB_DIR` deliberately, and a
    resolution that preferred the packaged copy would make that setting dead
    everywhere it is used. A checkout serving its own build sets it to
    `web/dist`; `create_app` warns once and serves the API alone when the
    directory holds no build, which stays the ordinary developer state.
    """
    from_environment = os.environ.get("ORS_WEB_DIR")
    return Path(from_environment) if from_environment else packaged_web_dir()
```

Replace the `web_dir=` line inside `main` with `web_dir=resolve_web_dir(),` and
delete the old `os.environ.get("ORS_WEB_DIR", "/app/web")` expression along with
the paragraph of its comment that describes `/app/web` as the default (keep the
paragraph about the venv layout, moved to `packaged_web_dir`).

- [ ] **Step 4: Make hatchling ship the directory**

In `server/pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/ors_server"]
# `artifacts`, not just `packages`. Hatchling honours .gitignore when selecting
# files, and `src/ors_server/web/` is gitignored precisely because it is a build
# output that would go stale the day it were committed. Without this line the
# wheel builds cleanly, installs cleanly, and serves no pages.
artifacts = ["src/ors_server/web/**"]
```

Add to `.gitignore`:

```gitignore
# Built by `pnpm build` and copied in at release time. Committing it would ship
# a stale interface from whichever checkout last ran the build.
server/src/ors_server/web/
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest server/tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 6: Prove the Docker path still works**

```bash
docker build -f deploy/Dockerfile -t ors-m3c-check .
docker run --rm -e ORS_DATA_DIR=/tmp/d ors-m3c-check ors-server --help
```

Expected: exits 0. If the Dockerfile does not already set `ORS_WEB_DIR`, add
`ENV ORS_WEB_DIR=/app/web` to it in this step and say so in the commit.

- [ ] **Step 7: Commit**

```bash
git add server/ .gitignore deploy/Dockerfile
git commit -m "feat(server): the interface, inside the wheel that serves it"
```

---

## Task 3: The data directory a non-root user can write

**Files:**
- Modify: `server/src/ors_server/__main__.py`, `deploy/Dockerfile`, `deploy/compose.pi.yaml`, `deploy/compose.remote.yaml`
- Test: `server/tests/test_main.py`, `server/tests/test_deploy.py`

**Interfaces:**
- Produces: `ors_server.__main__.resolve_data_dir() -> Path`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_main.py`:

```python
def test_the_data_directory_defaults_somewhere_a_normal_user_can_write(monkeypatch, tmp_path):
    """`/var/lib/openrackscreen` needs root, and `uv tool install` is not root.

    The whole point of publishing to PyPI is that `uv tool install ors-server
    && ors-server` works. A default that raises PermissionError on the first
    boot for anybody who has not read the environment table makes that false.
    """
    from ors_server.__main__ import resolve_data_dir

    monkeypatch.delenv("ORS_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert resolve_data_dir() == tmp_path / "openrackscreen"


def test_the_data_directory_falls_back_to_local_state_without_xdg(monkeypatch, tmp_path):
    from ors_server.__main__ import resolve_data_dir

    monkeypatch.delenv("ORS_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_data_dir() == tmp_path / ".local" / "state" / "openrackscreen"


def test_the_environment_still_wins_for_the_data_directory(monkeypatch, tmp_path):
    """The container and the systemd unit both set it, and both must keep winning."""
    from ors_server.__main__ import resolve_data_dir

    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path / "explicit"))
    assert resolve_data_dir() == tmp_path / "explicit"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest server/tests/test_main.py -k data_directory -v`
Expected: FAIL — `cannot import name 'resolve_data_dir'`.

- [ ] **Step 3: Implement**

```python
def resolve_data_dir() -> Path:
    """The database, the secret key and the stored credentials.

    `~/.local/state/openrackscreen` and not `/var/lib/openrackscreen`, which
    needs root: the point of publishing to an index is that `uv tool install
    ors-server && ors-server` works, and a default only root can write makes
    the first boot a PermissionError for everyone who has not read the
    environment table. Both deployments that *should* use `/var/lib` -- the
    container and the generated unit -- set `ORS_DATA_DIR` explicitly, which
    keeps that path visible where it is chosen rather than implicit here.
    """
    from_environment = os.environ.get("ORS_DATA_DIR")
    if from_environment:
        return Path(from_environment)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "openrackscreen"
```

Replace the `data_dir=` line in `main` with `data_dir=resolve_data_dir(),`.

- [ ] **Step 4: Set it explicitly where it matters**

`deploy/Dockerfile`: add `ENV ORS_DATA_DIR=/var/lib/openrackscreen`.

Both compose files: confirm the `environment:` block names `ORS_DATA_DIR`, and
add it if it does not. `server/tests/test_deploy.py` reads these as YAML
documents; extend it:

```python
def test_both_compose_files_set_the_data_directory_explicitly():
    """The code default moved to the user's state directory in M3c.

    A compose file that relied on the old `/var/lib` default now puts the
    database somewhere inside the container's root home, and the named volume
    it mounts is a directory nothing writes to -- so every restart is a fresh
    server with no password set, and the volume looks healthy and empty.
    """
    for path in (COMPOSE_PI, COMPOSE_REMOTE):
        document = yaml.safe_load(path.read_text())
        service = document["services"]["server"]
        assert service["environment"]["ORS_DATA_DIR"] == "/var/lib/openrackscreen"
```

Use whatever the existing constants in `test_deploy.py` are called for the two
paths and the service key; read the file first rather than assuming `server`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest server/tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/ deploy/
git commit -m "feat(server): a data directory the user running it can write"
```

---

## Task 4: A checkout that builds itself

**Files:**
- Create: `deploy/compose.image.yaml`
- Modify: `deploy/compose.pi.yaml`, `deploy/compose.remote.yaml`
- Test: `server/tests/test_deploy.py`

**Interfaces:**
- Produces: nothing importable. Behavioural: `docker compose -f deploy/compose.pi.yaml up -d` builds the working tree.

- [ ] **Step 1: Write the failing test**

```python
def test_no_compose_file_names_both_an_image_and_a_build():
    """Compose prefers `image:`, so naming both never builds your checkout.

    Measured, not theorised: on this machine a 45-hour-old copy of
    `ghcr.io/silkepilon/openrackscreen:latest` was started instead of the
    working tree, went `healthy`, answered `/api/health`, and returned 404 for
    every page -- because it predated the interface being in the image. The
    README had to grow a paragraph telling people to pass `--build`. This test
    is that paragraph, enforced.
    """
    for path in (COMPOSE_PI, COMPOSE_REMOTE):
        document = yaml.safe_load(path.read_text())
        for name, service in document["services"].items():
            assert not ("image" in service and "build" in service), (
                f"{path.name}:{name} names both; compose will silently use the image"
            )


def test_the_image_override_pins_the_published_tag():
    """Pulling a published image stays possible, and stays a thing you ask for."""
    document = yaml.safe_load(COMPOSE_IMAGE.read_text())
    service = document["services"]["server"]
    assert service["image"] == "ghcr.io/silkepilon/openrackscreen:latest"
    assert "build" not in service
```

Add `COMPOSE_IMAGE = DEPLOY / "compose.image.yaml"` beside the existing path
constants.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest server/tests/test_deploy.py -k compose -v`
Expected: FAIL on both — the two files name both keys, and `compose.image.yaml` does not exist.

- [ ] **Step 3: Remove `image:` from both compose files**

Delete the `image: ghcr.io/silkepilon/openrackscreen:latest` line from
`deploy/compose.pi.yaml` and `deploy/compose.remote.yaml`. Leave `build:`.

- [ ] **Step 4: Create the override**

`deploy/compose.image.yaml`:

```yaml
# Run the published image instead of building this checkout.
#
#   docker compose -f deploy/compose.pi.yaml -f deploy/compose.image.yaml up -d
#
# Separate from the two files it overrides, and not merged into them, because
# compose prefers `image:` over `build:` when a service names both -- so a file
# carrying both never builds your working tree, and the symptom is a container
# that starts, goes healthy, answers /api/health and 404s every page.
services:
  server:
    image: ghcr.io/silkepilon/openrackscreen:latest
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest server/tests/test_deploy.py -v`
Expected: PASS.

- [ ] **Step 6: Verify by running it**

```bash
docker compose -f deploy/compose.pi.yaml up -d
curl -fsS localhost:8080/ | head -c 200
docker compose -f deploy/compose.pi.yaml down
```

Expected: HTML, not a 404. This is the check that the whole task exists for; do
not skip it.

- [ ] **Step 7: Commit**

```bash
git add deploy/ server/tests/test_deploy.py
git commit -m "fix(deploy): a compose file that builds the checkout it ships with"
```

---

## Task 5: The release workflow

**Files:**
- Create: `.github/workflows/release.yml`
- Modify: `tests/test_packaging.py`

**Interfaces:**
- Consumes: `tools/version.py` (Task 1), the `artifacts` key (Task 2).
- Produces: nothing importable.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_packaging.py`:

```python
def test_the_release_workflow_refuses_a_wheel_without_the_interface():
    """The gate that stops §2.3's failure reaching an index.

    A published `ors-server` wheel with no `ors_server/web/index.html` in it
    installs cleanly and serves no pages. That is not recoverable by yanking:
    somebody has already installed it. The workflow has to check before it
    uploads, and this asserts the check is there rather than trusting a
    reviewer to notice its removal.
    """
    import yaml

    document = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    build = document["jobs"]["build"]
    steps = [step.get("name", step.get("uses", "")) for step in build["steps"]]
    check = next(i for i, name in enumerate(steps) if "no interface" in name)
    upload = next(i for i, name in enumerate(steps) if "upload-artifact" in name)
    # Ordering asserted on the job graph and the step list, not on a substring
    # search of the file. A text search for "pypi.org" passes whatever the
    # workflow does -- `pypa/gh-action-pypi-publish` does not contain that
    # string -- which is a test that reads like a gate and is not one.
    assert check < upload, "the interface check must run before the artifact is uploaded"
    assert document["jobs"]["publish"]["needs"] == "build" or "build" in document["jobs"][
        "publish"
    ]["needs"], "publish must not run unless build succeeded"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_packaging.py -k release -v`
Expected: FAIL — `FileNotFoundError: .../release.yml`.

- [ ] **Step 3: Write the workflow**

`.github/workflows/release.yml`:

```yaml
name: release

# A tag, not a branch push: publishing is irreversible per version, and a
# workflow that fired on every merge would burn version numbers on commits
# nobody meant to ship.
on:
  push:
    tags: ["v*"]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - uses: astral-sh/setup-uv@v7
        with:
          enable-cache: true

      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v5
        with:
          node-version: 24
          cache: pnpm
          cache-dependency-path: web/pnpm-lock.yaml

      # The interface is built first and copied into the server package, so the
      # wheel built below contains it. Everything after this depends on it
      # having happened.
      - name: Build the interface
        working-directory: web
        run: |
          pnpm install --frozen-lockfile
          pnpm build

      - name: Put the interface inside the server package
        run: |
          rm -rf server/src/ors_server/web
          cp -r web/dist server/src/ors_server/web

      - name: Check the tag matches the version in the tree
        run: |
          uv run python - <<'PY'
          import sys
          sys.path.insert(0, "tools")
          import os
          from version import read_versions
          tag = os.environ["GITHUB_REF_NAME"].removeprefix("v")
          versions = set(read_versions().values())
          if versions != {tag}:
              raise SystemExit(f"tag {tag!r} does not match {versions}")
          PY

      - name: Build every distribution
        run: uv build --all-packages --out-dir dist

      # Before anything is uploaded. A wheel without the interface installs
      # cleanly and serves no pages, and yanking does not un-install it.
      - name: Refuse a server wheel with no interface in it
        run: |
          uv run python - <<'PY'
          import pathlib, sys, zipfile
          wheels = list(pathlib.Path("dist").glob("ors_server-*.whl"))
          if len(wheels) != 1:
              raise SystemExit(f"expected one ors_server wheel, found {wheels}")
          names = zipfile.ZipFile(wheels[0]).namelist()
          if "ors_server/web/index.html" not in names:
              raise SystemExit("the server wheel has no interface in it")
          print(f"{wheels[0].name} carries the interface")
          PY

      # Spec §9. `[tool.uv.sources]` is uv-only and should never reach a wheel,
      # but "should" is how a `file://` requirement gets published and fails to
      # install for everyone who is not in this workspace.
      - name: Refuse a wheel whose dependencies name a local path
        run: |
          uv run python - <<'PY'
          import email.parser, pathlib, zipfile
          for wheel in pathlib.Path("dist").glob("*.whl"):
              archive = zipfile.ZipFile(wheel)
              [metadata] = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
              parsed = email.parser.BytesParser().parsebytes(archive.read(metadata))
              for requirement in parsed.get_all("Requires-Dist") or []:
                  if "file://" in requirement or "@ /" in requirement:
                      raise SystemExit(f"{wheel.name} requires a local path: {requirement}")
          print("no wheel names a local path")
          PY

      - uses: actions/upload-artifact@v4
        with:
          name: distributions
          path: dist/

  publish:
    needs: build
    runs-on: ubuntu-latest
    environment: release
    # Trusted Publishing: PyPI verifies this workflow's OIDC identity, so no API
    # token is stored in this repository and none can be stolen from it.
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: distributions
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_packaging.py -v`
Expected: PASS.

- [ ] **Step 5: Lint the workflow**

Run: `uv run python -c "import yaml,pathlib;yaml.safe_load(pathlib.Path('.github/workflows/release.yml').read_text());print('parses')"`
Expected: `parses`.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/release.yml tests/test_packaging.py
git commit -m "ci: a release that cannot publish a server wheel with no interface in it"
```

**Note for the reviewer:** Trusted Publishing must be configured once on PyPI
for each of the five projects, by a human, in the project settings. That is not
a code step and cannot be automated from here. The first tag will fail to
publish until it is done, and that is the correct order — the workflow's
identity has to exist before PyPI can be told to trust it.

---

## Task 6: The install identity

**Files:**
- Create: `daemon/src/ors_daemon/identity.py`, `daemon/tests/test_identity.py`

**Interfaces:**
- Produces:
  - `ors_daemon.identity.Identity` — frozen dataclass, fields `secret: bytes`, `fingerprint: str`, `short_code: str`.
  - `ors_daemon.identity.load_or_create(path: Path) -> Identity`
  - `ors_daemon.identity.IDENTITY_BYTES: int`, `SHORT_CODE_CHARS: int`

**Correction to the spec, made during plan self-review.** The spec's §6.3 step 4
has the daemon authenticate each poll with "an HMAC over the claim id under its
identity secret". **The server cannot verify that**: it stores only
`sha256(secret)`, never the secret, so it has nothing to compute the HMAC with.

The poll needs no signature at all. The claim id becomes a
`secrets.token_urlsafe(32)` **bearer capability** — generated by the server,
returned only in the 202 to the daemon that filed the claim, never listed in the
interface. Confidentiality of the handed-over key does not rest on it in any
case: the key is sealed to the claim's X25519 public key, so a ciphertext
collected by anyone else is useless. Guessing a claim id therefore buys an
attacker a `{"status": "approved"}` and an undecryptable blob.

Update the spec's §6.3 step 4 when this task is implemented, and do not
implement `sign()`.

- [ ] **Step 1: Write the failing test**

Create `daemon/tests/test_identity.py`:

```python
"""The secret this rack is, and the six characters a human compares.

The claim endpoint is unauthenticated by necessity -- a daemon that has not
been approved holds no credential -- so the only thing standing between an
admin's click and a stranger's rack is that the code on the screen matches the
code on the Pi.
"""

from __future__ import annotations

import json
import stat

import pytest

from ors_daemon.identity import IDENTITY_BYTES, SHORT_CODE_CHARS, load_or_create


def test_the_two_constants_are_the_numbers_they_are_meant_to_be():
    """Literal, because every other assertion in this file reads the constant.

    `len(secret) == IDENTITY_BYTES` is satisfied by any value of
    `IDENTITY_BYTES`, including 4 -- the constant is on both sides. 32 is the
    input to a SHA-256; 6 base32 characters is 30 bits, which is far more than
    the number of racks anyone approves and few enough to read off a terminal
    without losing your place.
    """
    assert IDENTITY_BYTES == 32
    assert SHORT_CODE_CHARS == 6


def test_a_fresh_identity_is_random_and_persisted(tmp_path):
    path = tmp_path / "identity.json"
    first = load_or_create(path)
    assert len(first.secret) == IDENTITY_BYTES
    # Read back, not regenerated: the fingerprint is what the server stores, and
    # a rack whose identity changed on restart would file a new claim -- with a
    # different short code -- every time it rebooted.
    assert load_or_create(path) == first


def test_two_racks_are_not_the_same_rack(tmp_path):
    """An identity fixture where both sides coincide would hide a constant.

    `secrets.token_bytes` replaced by a fixed value passes any test that only
    ever creates one identity.
    """
    left = load_or_create(tmp_path / "a.json")
    right = load_or_create(tmp_path / "b.json")
    assert left.secret != right.secret
    assert left.fingerprint != right.fingerprint
    assert left.short_code != right.short_code


def test_the_file_is_private(tmp_path):
    """0600. It is the whole of this rack's claim to its own identity."""
    path = tmp_path / "identity.json"
    load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_secret_is_never_in_the_fingerprint(tmp_path):
    identity = load_or_create(tmp_path / "identity.json")
    assert identity.secret.hex() not in identity.fingerprint
    assert len(identity.fingerprint) == 64  # sha256, hex


def test_the_short_code_is_readable_and_short(tmp_path):
    identity = load_or_create(tmp_path / "identity.json")
    assert len(identity.short_code) == SHORT_CODE_CHARS
    # Base32 without padding: no lowercase, no 0/1/8, nothing a person reading
    # it off a terminal can confuse with something else.
    assert set(identity.short_code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_the_short_code_follows_the_fingerprint(tmp_path):
    """Derived, not stored. A code stored beside the fingerprint could drift
    from it, and then the thing a human matched would not be the thing the
    server keyed on."""
    identity = load_or_create(tmp_path / "identity.json")
    reloaded = load_or_create(tmp_path / "identity.json")
    assert reloaded.short_code == identity.short_code


def test_a_corrupt_identity_file_is_an_error_not_a_new_rack(tmp_path):
    """Regenerating silently would mint a second identity for one rack, and the
    pending claim an admin is looking at would stop being this daemon's."""
    path = tmp_path / "identity.json"
    path.write_text("not json")
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)


def test_an_identity_missing_its_secret_is_an_error(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": 1}))
    with pytest.raises(ValueError, match="identity"):
        load_or_create(path)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest daemon/tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.identity'`.

- [ ] **Step 3: Implement**

Create `daemon/src/ors_daemon/identity.py`:

```python
"""What this installation of the daemon is, independently of any server.

Generated once by `ors-daemon install` and kept for the life of the machine.
It survives re-pairing and outlives any single server: a rack that is denied,
re-approved, or pointed at a different server is still the same rack, and the
six characters a human compares must not move under them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

IDENTITY_BYTES = 32
"""The secret's length. 32 because it is the input to an HMAC-SHA256, and a key
shorter than the hash's block adds nothing while a longer one is folded."""

SHORT_CODE_CHARS = 6
"""How much of the fingerprint a person is asked to compare.

Six base32 characters is 30 bits: about a billion, which is far more than the
number of racks anyone will ever approve, and few enough to read off a terminal
without losing your place. It is a check against *confusion*, not against a
determined collision -- the fingerprint is what the server keys on, and this is
what the admin looks at."""

_VERSION = 1


@dataclass(frozen=True)
class Identity:
    """A rack's own name for itself.

    `secret` never leaves the Pi. `fingerprint` is what the server stores, and
    `short_code` is what the interface shows.
    """

    secret: bytes
    fingerprint: str
    short_code: str


def _derive(secret: bytes) -> Identity:
    digest = hashlib.sha256(secret).digest()
    # Base32 and not hex: hex is 4 bits a character, so six characters would be
    # 24 bits, and it contains no letters past F -- which makes a short code
    # look like a number and reads worse aloud.
    code = base64.b32encode(digest).decode("ascii")[:SHORT_CODE_CHARS]
    return Identity(secret=secret, fingerprint=digest.hex(), short_code=code)


def load_or_create(path: Path) -> Identity:
    """Read this machine's identity, generating one the first time.

    A file that exists but does not parse raises rather than being replaced.
    Regenerating silently would give one rack a second identity, and the
    pending claim an admin is looking at would quietly stop being this
    daemon's -- so they would be approving something that no longer exists,
    and this rack would file a third claim behind it.
    """
    if path.exists():
        try:
            document = json.loads(path.read_text())
            raw = document["secret"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(
                f"{path} is not a readable identity: {error}. "
                "Delete it to mint a new one, which costs a re-approval."
            ) from error
        return _derive(base64.b64decode(raw))

    secret = secrets.token_bytes(IDENTITY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened with the mode already on it: writing first and chmod-ing after
    # leaves a window where the file is world-readable, and this file is the
    # entire proof that this rack is this rack.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"version": _VERSION, "secret": base64.b64encode(secret).decode()}, handle)
    return _derive(secret)
```

Drop the `hmac` import along with `sign`; the module needs only `base64`,
`hashlib`, `json`, `os`, `secrets`, `dataclasses` and `pathlib`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest daemon/tests/test_identity.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add daemon/src/ors_daemon/identity.py daemon/tests/test_identity.py
git commit -m "feat(daemon): the secret a rack is, and the six characters a person checks"
```

---

## Task 7: The `config.txt` editor

**Files:**
- Create: `daemon/src/ors_daemon/boot_config.py`, `daemon/tests/test_boot_config.py`

**Interfaces:**
- Produces:
  - `ors_daemon.boot_config.SPI_LINES: tuple[str, ...]` — `("dtparam=spi=on", "dtoverlay=spi1-2cs")`
  - `ors_daemon.boot_config.CANDIDATES: tuple[str, ...]` — relative paths, in order.
  - `ors_daemon.boot_config.find_config(boot_root: Path) -> Path | None`
  - `ors_daemon.boot_config.EnableResult` — frozen dataclass: `path: Path | None`, `added: tuple[str, ...]`, `backup: Path | None`, `diff: str`.
  - `ors_daemon.boot_config.enable_spi(boot_root: Path, now: str) -> EnableResult`

- [ ] **Step 1: Write the failing test**

Create `daemon/tests/test_boot_config.py`:

```python
"""The one step in `install` whose failure mode is an SD reader.

Everything else this command does can be undone from a shell on the machine.
A `config.txt` that does not parse is a Pi that does not boot, and the only
recovery is another computer and the card in your hand.
"""

from __future__ import annotations

from pathlib import Path

from ors_daemon.boot_config import SPI_LINES, enable_spi, find_config

NOW = "2026-08-16T12:00:00"
"""Passed in rather than read from a clock, so the backup's name is a fact the
test states rather than one it has to discover."""


def _boot(tmp_path: Path, relative: str, text: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_it_prefers_the_bookworm_path(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "# firmware\n")
    _boot(tmp_path, "config.txt", "# legacy\n")
    assert find_config(tmp_path) == tmp_path / "firmware/config.txt"


def test_it_falls_back_to_the_pre_bookworm_path(tmp_path):
    _boot(tmp_path, "config.txt", "# legacy\n")
    assert find_config(tmp_path) == tmp_path / "config.txt"


def test_a_machine_that_is_not_a_pi_is_not_an_error(tmp_path):
    """Installing the daemon on an x86 box to drive virtual panels is a real
    thing this project supports -- `render` and the virtual display exist for
    it -- and it must not fail at a file only a Pi has."""
    result = enable_spi(tmp_path, NOW)
    assert result.path is None
    assert result.added == ()
    assert result.backup is None


def test_it_adds_both_lines_to_a_config_that_has_neither(tmp_path):
    path = _boot(tmp_path, "firmware/config.txt", "# a comment\ndtparam=audio=on\n")
    result = enable_spi(tmp_path, NOW)

    assert result.added == SPI_LINES
    text = path.read_text()
    for line in SPI_LINES:
        assert f"\n{line}\n" in text
    # Nothing that was there before is gone. The whole file is rewritten, so
    # this is the assertion that catches a rewrite that drops what it did not
    # recognise.
    assert "dtparam=audio=on" in text
    assert "# a comment" in text


def test_it_backs_the_file_up_before_touching_it(tmp_path):
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    result = enable_spi(tmp_path, NOW)

    assert result.backup == path.parent / f"config.txt.ors-{NOW}"
    assert result.backup.read_text() == "# original\n"


def test_a_second_run_changes_nothing_and_takes_no_backup(tmp_path):
    """Idempotent, because `install` is documented as safe to re-run and is the
    upgrade path. A backup per run would fill /boot with copies."""
    _boot(tmp_path, "firmware/config.txt", "# a comment\n")
    enable_spi(tmp_path, NOW)
    after_first = (tmp_path / "firmware/config.txt").read_text()

    second = enable_spi(tmp_path, "2026-08-16T13:00:00")

    assert second.added == ()
    assert second.backup is None
    assert (tmp_path / "firmware/config.txt").read_text() == after_first
    assert not (tmp_path / "firmware/config.txt.ors-2026-08-16T13:00:00").exists()


def test_an_existing_line_that_differs_is_left_exactly_as_written(tmp_path):
    """Somebody who tuned their overlay meant it. Overwriting a deliberate
    `spi1-3cs` with our `spi1-2cs` silently removes a chip select they wired."""
    _boot(tmp_path, "firmware/config.txt", "dtoverlay=spi1-3cs\n")
    result = enable_spi(tmp_path, NOW)

    text = (tmp_path / "firmware/config.txt").read_text()
    assert "dtoverlay=spi1-3cs" in text
    assert "dtoverlay=spi1-2cs" not in text
    assert result.added == ("dtparam=spi=on",)


def test_a_commented_out_line_does_not_count_as_present(tmp_path):
    """`#dtparam=spi=on` is the state a Pi ships in. Treating it as enabled is
    how every panel comes up unavailable with a config.txt that looks right."""
    _boot(tmp_path, "firmware/config.txt", "#dtparam=spi=on\n")
    result = enable_spi(tmp_path, NOW)
    assert "dtparam=spi=on" in result.added


def test_the_diff_names_only_what_changed(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "dtparam=audio=on\n")
    result = enable_spi(tmp_path, NOW)
    assert "+dtparam=spi=on" in result.diff
    assert "+dtoverlay=spi1-2cs" in result.diff
    assert "-dtparam=audio=on" not in result.diff


def test_a_file_with_no_trailing_newline_does_not_lose_its_last_line(tmp_path):
    """`dtparam=audio=ondtparam=spi=on` is one broken line, and the Pi that
    boots from it has no audio and no SPI."""
    _boot(tmp_path, "firmware/config.txt", "dtparam=audio=on")
    enable_spi(tmp_path, NOW)
    lines = (tmp_path / "firmware/config.txt").read_text().splitlines()
    assert "dtparam=audio=on" in lines
    assert "dtparam=spi=on" in lines
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest daemon/tests/test_boot_config.py -v`
Expected: FAIL — `No module named 'ors_daemon.boot_config'`.

- [ ] **Step 3: Implement**

Create `daemon/src/ors_daemon/boot_config.py`:

```python
"""Turning both SPI buses on, in the one file where getting it wrong costs a
card reader.

The single most common reason every panel comes up `unavailable` is that SPI1
was never enabled, and it is invisible from software: `/dev/spidev0.*` exists,
the daemon opens what it can, and the two panels on the auxiliary block are
simply not there. Nothing reports why.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

SPI_LINES = ("dtparam=spi=on", "dtoverlay=spi1-2cs")
"""Both buses, in the order `daemon/README.md` documents them.

`dtparam=spi=on` is SPI0: CE0 on GPIO8 and CE1 on GPIO7, panels 1 and 2.
`dtoverlay=spi1-2cs` is the auxiliary block: CE0 on GPIO18 and CE1 on GPIO17,
panels 3 and 4. A rack with four panels needs both; a rack with two needs the
first, and the second costs it nothing."""

CANDIDATES = ("firmware/config.txt", "config.txt")
"""Bookworm moved it. In this order, so a machine that has both -- an upgrade
that left the old file behind -- gets the one the firmware actually reads."""


@dataclass(frozen=True)
class EnableResult:
    """What `enable_spi` did, in enough detail to print.

    `path is None` means the machine has no `config.txt` at all, which is not
    an error: installing the daemon on an x86 box to drive virtual panels is
    supported, and `render` exists for exactly that.
    """

    path: Path | None
    added: tuple[str, ...]
    backup: Path | None
    diff: str


def find_config(boot_root: Path) -> Path | None:
    for relative in CANDIDATES:
        candidate = boot_root / relative
        if candidate.is_file():
            return candidate
    return None


def _already_has(lines: list[str], setting: str) -> bool:
    # `dtoverlay=spi1-2cs` counts as present when any `dtoverlay=spi1-` line is
    # there: somebody running `spi1-3cs` wired a third chip select, and
    # appending ours would fight theirs. A commented-out line is not present --
    # `#dtparam=spi=on` is the state a Pi ships in, and reading it as enabled is
    # how a config.txt that looks right produces four unavailable panels.
    key = setting.split("=")[0]
    prefix = "spi1-" if setting.startswith("dtoverlay=spi1-") else None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != key:
            continue
        if prefix is not None and value.strip().startswith(prefix):
            return True
        if prefix is None and stripped == setting:
            return True
    return False


def enable_spi(boot_root: Path, now: str) -> EnableResult:
    """Add the SPI lines this project needs, once, behind a backup.

    `now` is passed in rather than read from a clock so the backup's name is
    decided by the caller and asserted by the tests without freezing time.
    """
    path = find_config(boot_root)
    if path is None:
        return EnableResult(path=None, added=(), backup=None, diff="")

    original = path.read_text()
    lines = original.splitlines()
    missing = tuple(line for line in SPI_LINES if not _already_has(lines, line))
    if not missing:
        # No backup on a no-op: `install` is safe to re-run and is the upgrade
        # path, so a copy per run would fill /boot with them.
        return EnableResult(path=path, added=(), backup=None, diff="")

    updated = lines + ["", "# Added by `ors-daemon install`.", *missing]
    # `splitlines` dropped whatever the file ended with; this puts a newline
    # back deliberately. Without it a file with no trailing newline gets
    # `dtparam=audio=ondtparam=spi=on`, which is one broken line and a Pi with
    # neither setting.
    text = "\n".join(updated) + "\n"

    backup = path.with_name(f"{path.name}.ors-{now}")
    shutil.copy2(path, backup)
    path.write_text(text)

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(), text.splitlines(), fromfile=str(path), tofile=str(path), lineterm=""
        )
    )
    return EnableResult(path=path, added=missing, backup=backup, diff=diff)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest daemon/tests/test_boot_config.py -v`
Expected: 11 passed.

- [ ] **Step 5: Mutation-check the two constants**

```bash
sed -i 's/^CANDIDATES = ("firmware\/config.txt", "config.txt")/CANDIDATES = ("config.txt", "firmware\/config.txt")/' daemon/src/ors_daemon/boot_config.py
uv run pytest daemon/tests/test_boot_config.py -q
git checkout -- daemon/src/ors_daemon/boot_config.py
```

Expected: at least one named test FAILS. If it does not, the ordering is
unpinned — add a test before continuing.

- [ ] **Step 6: Commit**

```bash
git add daemon/src/ors_daemon/boot_config.py daemon/tests/test_boot_config.py
git commit -m "feat(daemon): both SPI buses, behind a backup and an idempotent edit"
```

---

## Task 8: `ors-daemon install` — users, directories, venv, unit

**Files:**
- Create: `daemon/src/ors_daemon/install.py`, `daemon/tests/test_install.py`
- Modify: `daemon/examples/openrackscreen.service`

**Interfaces:**
- Consumes: `boot_config.enable_spi` (Task 7), `identity.load_or_create` (Task 6).
- Produces:
  - `ors_daemon.install.Roots` — frozen dataclass: `etc: Path`, `boot: Path`, `state: Path`, `prefix: Path`, `systemd: Path`.
  - `ors_daemon.install.Runner` — protocol with `run(argv: list[str]) -> int`.
  - `ors_daemon.install.SERVICE_NAME: str` = `"openrackscreen"`
  - `ors_daemon.install.SERVICE_USER: str` = `"openrackscreen"`
  - `ors_daemon.install.unit_text(exec_start: str) -> str`
  - `ors_daemon.install.install(roots, runner, *, version, enable_spi_step=True, use_current_interpreter=False, now) -> InstallReport`
  - `ors_daemon.install.uninstall(roots, runner, *, purge=False) -> UninstallReport`

- [ ] **Step 1: Write the failing test**

Create `daemon/tests/test_install.py`. This is the largest test file in the
milestone; write it in full.

```python
"""Everything `install` changes on a machine, against roots that are not it.

No test in this file may touch /etc, /boot, /var or systemd. Every path is
under `tmp_path` and every subprocess goes through `FakeRunner`, which records
argv and returns whatever the test says. A test that shelled out for real would
pass on the author's laptop and reconfigure a reviewer's.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ors_daemon.install import (
    SERVICE_NAME,
    SERVICE_USER,
    Roots,
    install,
    uninstall,
    unit_text,
)

NOW = "2026-08-16T12:00:00"


@dataclass
class FakeRunner:
    """Records what would have been run, and answers what the test decides.

    `codes` maps the first argument -- `useradd`, `systemctl`, `uv` -- to the
    exit code to return, defaulting to 0. Keyed on the program and not on the
    whole argv because a test that cared about the arguments asserts on
    `calls`, and one that only wants a failure should not have to spell the
    successful command out to get it.
    """

    codes: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return self.codes.get(Path(argv[0]).name, 0)

    def programs(self) -> list[str]:
        return [Path(call[0]).name for call in self.calls]

    def argv_for(self, program: str) -> list[list[str]]:
        return [call for call in self.calls if Path(call[0]).name == program]


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    for name in ("etc", "boot", "state", "opt", "systemd"):
        (tmp_path / name).mkdir()
    return Roots(
        etc=tmp_path / "etc",
        boot=tmp_path / "boot",
        state=tmp_path / "state",
        prefix=tmp_path / "opt" / "openrackscreen",
        systemd=tmp_path / "systemd",
    )


def _install(roots: Roots, runner: FakeRunner, **kwargs):
    kwargs.setdefault("version", "0.2.0")
    kwargs.setdefault("now", NOW)
    return install(roots, runner, **kwargs)


# --- directories -----------------------------------------------------------


def test_it_creates_the_directories_the_daemon_needs(roots):
    _install(roots, FakeRunner())
    assert (roots.etc / "openrackscreen").is_dir()
    assert (roots.state / "openrackscreen").is_dir()


def test_the_state_directory_is_private(roots):
    """0700. It holds the pairing and the install identity: the right to
    reconfigure this rack and to draw on its panels."""
    _install(roots, FakeRunner())
    mode = stat.S_IMODE((roots.state / "openrackscreen").stat().st_mode)
    assert mode == 0o700


def test_running_it_twice_changes_nothing_the_second_time(roots):
    """`install` is documented as safe to re-run and is the upgrade path."""
    first = _install(roots, FakeRunner())
    second = _install(roots, FakeRunner())
    assert first.unit_path.read_text() == second.unit_path.read_text()
    assert second.created_user is False


# --- the user --------------------------------------------------------------


def test_it_creates_a_system_user_with_no_login(roots):
    runner = FakeRunner()
    _install(roots, runner)
    [argv] = runner.argv_for("useradd")
    assert SERVICE_USER in argv
    assert "--system" in argv
    # A daemon account that can be logged into is an account that can be logged
    # into. There is nothing for a person to do as this user.
    assert "/usr/sbin/nologin" in argv or "/sbin/nologin" in argv


def test_an_existing_user_is_not_recreated(roots):
    """`useradd` on an existing user exits 9, and treating that as a failure
    would make the second `install` -- the upgrade path -- fail."""
    runner = FakeRunner(codes={"useradd": 9})
    report = _install(roots, runner)
    assert report.created_user is False
    assert report.failed is False


def test_it_joins_the_groups_that_reach_the_panels(roots):
    """On Raspberry Pi OS /dev/spidev* is group `spi` and /dev/gpiochip* is
    group `gpio`. Without both, every screen comes up unavailable."""
    runner = FakeRunner()
    _install(roots, runner)
    joined = " ".join(" ".join(argv) for argv in runner.argv_for("usermod"))
    assert "spi" in joined
    assert "gpio" in joined


def test_a_missing_group_is_reported_and_not_created(roots):
    """The groups come with the udev rules that make them mean anything.
    Inventing a group with no rules behind it produces a rack that comes up
    with four unavailable screens and a configuration that looks correct."""
    runner = FakeRunner(codes={"usermod": 6})
    report = _install(roots, runner)
    assert "gpio" in report.warnings_text() or "spi" in report.warnings_text()
    assert "groupadd" not in runner.programs()


# --- the venv --------------------------------------------------------------


def test_it_installs_itself_into_a_predictable_prefix(roots):
    """`sudo uv tool install ors-daemon` lands in *root's* data directory,
    which User=openrackscreen cannot read -- and the rack then comes up dead
    with a permission error on the interpreter, which appears in no daemon log
    because the daemon never starts."""
    runner = FakeRunner()
    _install(roots, runner)
    [venv] = runner.argv_for("uv")[:1]
    assert "venv" in venv
    assert str(roots.prefix) in " ".join(venv)
    installed = " ".join(" ".join(argv) for argv in runner.argv_for("uv")[1:])
    assert "ors-daemon[hardware]==0.2.0" in installed


def test_the_unit_points_at_the_prefix(roots):
    report = _install(roots, FakeRunner())
    assert f"ExecStart={roots.prefix}/bin/ors-daemon run" in report.unit_path.read_text()


def test_the_current_interpreter_can_be_used_instead(roots):
    runner = FakeRunner()
    interpreter = roots.etc / "readable" / "bin" / "ors-daemon"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)

    report = _install(roots, runner, use_current_interpreter=True, executable=interpreter)

    assert "uv" not in runner.programs()
    assert f"ExecStart={interpreter} run" in report.unit_path.read_text()


def test_an_unreadable_interpreter_is_refused_before_the_unit_is_written(roots):
    """Refusing after writing the unit leaves a machine that is configured to
    fail at every boot, and `systemctl status` blames the executable rather
    than the install that chose it."""
    runner = FakeRunner()
    interpreter = roots.etc / "private" / "bin" / "ors-daemon"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o700)
    interpreter.parent.chmod(0o700)

    report = _install(roots, runner, use_current_interpreter=True, executable=interpreter)

    assert report.failed is True
    assert "0700" in report.warnings_text() or "readable" in report.warnings_text()
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


# --- the unit --------------------------------------------------------------


def test_the_unit_keeps_every_line_that_is_load_bearing(roots):
    """Each of these was learned in front of a rack, and each has a comment in
    `daemon/examples/openrackscreen.service` saying what it costs to drop."""
    report = _install(roots, FakeRunner())
    text = report.unit_path.read_text()
    assert "StartLimitIntervalSec=0" in text
    assert "SupplementaryGroups=spi gpio" in text
    assert "TimeoutStopSec=30" in text
    assert "RuntimeDirectory=openrackscreen" in text
    assert "StateDirectory=openrackscreen" in text
    assert f"User={SERVICE_USER}" in text
    # Deliberately absent: it hides /dev/spidev* and /dev/gpiochip*, and every
    # screen comes up unavailable with a permission error nobody connects to it.
    assert "PrivateDevices=yes" not in text


def test_the_unit_does_not_pass_a_config_file(roots):
    """M3c made --config optional: a paired rack's configuration comes from the
    server, and a unit naming a file would make every rack need one."""
    report = _install(roots, FakeRunner())
    assert "--config" not in report.unit_path.read_text()


def test_the_generated_unit_and_the_example_do_not_drift(roots):
    """Two copies of this file is the seam this project has been bitten by.

    The example is what a person reads and edits by hand; the generated one is
    what actually runs. Every setting in the example that is not a path must be
    in what `install` writes.
    """
    report = _install(roots, FakeRunner())
    generated = report.unit_path.read_text()
    example = (
        Path(__file__).resolve().parents[1] / "examples" / "openrackscreen.service"
    ).read_text()

    def settings(text: str) -> set[str]:
        return {
            line.strip()
            for line in text.splitlines()
            if "=" in line
            and not line.strip().startswith("#")
            # Paths differ by construction: the example names /opt and the test
            # names a tmp_path.
            and not line.strip().startswith(("ExecStart=", "Documentation="))
        }

    assert settings(example) <= settings(generated)


def test_it_enables_and_starts_the_service(roots):
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("daemon-reload" in call for call in systemctl)
    assert any("enable" in call and "--now" in call for call in systemctl)


# --- SPI -------------------------------------------------------------------


def test_it_enables_spi_by_default(roots):
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    report = _install(roots, FakeRunner())
    text = (roots.boot / "firmware" / "config.txt").read_text()
    assert "dtparam=spi=on" in text
    assert report.reboot_needed is True


def test_spi_can_be_skipped(roots):
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    report = _install(roots, FakeRunner(), enable_spi_step=False)
    assert "dtparam=spi=on" not in (roots.boot / "firmware" / "config.txt").read_text()
    assert report.reboot_needed is False


def test_no_reboot_is_claimed_when_spi_was_already_on(roots):
    """A reboot people are told to take and do not need teaches them to ignore
    the next one."""
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text(
        "dtparam=spi=on\ndtoverlay=spi1-2cs\n"
    )
    report = _install(roots, FakeRunner())
    assert report.reboot_needed is False


# --- the identity ----------------------------------------------------------


def test_it_mints_the_install_identity(roots):
    """So the short code exists to be printed at the end of this command, which
    is where a person reads it before approving in the interface."""
    report = _install(roots, FakeRunner())
    assert (roots.state / "openrackscreen" / "identity.json").is_file()
    assert len(report.short_code) == 6


def test_a_second_install_keeps_the_same_short_code(roots):
    """Otherwise every upgrade files a new claim under a new code, and the
    pending entry an admin was looking at becomes a stranger."""
    first = _install(roots, FakeRunner())
    second = _install(roots, FakeRunner())
    assert second.short_code == first.short_code


# --- uninstall -------------------------------------------------------------


def test_uninstall_stops_disables_and_removes_the_unit(roots):
    _install(roots, FakeRunner())
    runner = FakeRunner()
    uninstall(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("stop" in call for call in systemctl)
    assert any("disable" in call for call in systemctl)
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


def test_uninstall_leaves_the_pairing_alone(roots):
    """The state directory holds the pairing and the identity. Removing it
    costs a re-approval in the interface, and a command called `uninstall`
    should not silently cost that."""
    _install(roots, FakeRunner())
    uninstall(roots, FakeRunner())
    assert (roots.state / "openrackscreen" / "identity.json").is_file()


def test_purge_removes_it_and_says_so(roots):
    _install(roots, FakeRunner())
    report = uninstall(roots, FakeRunner(), purge=True)
    assert not (roots.state / "openrackscreen").exists()
    assert "re-approv" in report.warnings_text().lower()


def test_uninstall_never_reverts_config_txt(roots):
    """Disabling SPI is not obviously desirable, and the backup is on disk with
    a name that says where it came from."""
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    _install(roots, FakeRunner())
    uninstall(roots, FakeRunner(), purge=True)
    assert "dtparam=spi=on" in (roots.boot / "firmware" / "config.txt").read_text()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest daemon/tests/test_install.py -v`
Expected: FAIL — `No module named 'ors_daemon.install'`.

- [ ] **Step 3: Implement `install.py`**

Create `daemon/src/ors_daemon/install.py`. Write it to satisfy the interface
block above. The unit template is the existing
`daemon/examples/openrackscreen.service` with `ExecStart` parameterised and
`--config` removed; copy the file's comments into the template verbatim rather
than paraphrasing them — they are the record of what each line cost to learn.

Key decisions the tests pin, restated so the implementer does not have to infer
them from assertions:

- `useradd` exit code **9** means "user exists" and is success, not failure.
- `usermod` exit code **6** means "group does not exist"; warn, do not create.
- The venv step is two calls: `uv venv <prefix>`, then
  `uv pip install --python <prefix>/bin/python "ors-daemon[hardware]==<version>"`.
- `--use-current-interpreter` checks `os.access(executable, os.X_OK)` **as the
  service user would** — in practice: the file and every parent directory must
  be `o+rx`. Check and refuse **before** writing the unit.
- `InstallReport` carries `unit_path: Path`, `created_user: bool`,
  `short_code: str`, `reboot_needed: bool`, `failed: bool`, `warnings: list[str]`,
  and `warnings_text() -> str` joining them.
- `UninstallReport` carries `warnings: list[str]` and `warnings_text()`.

- [ ] **Step 4: Update the example unit**

In `daemon/examples/openrackscreen.service`, change `ExecStart` to:

```ini
ExecStart=/opt/openrackscreen/bin/ors-daemon run \
          --status /run/openrackscreen/status.json
```

and add above it:

```ini
# /opt/openrackscreen/bin, not .venv/bin: `ors-daemon install` builds this venv
# with `uv venv /opt/openrackscreen`, which puts binaries in bin/. The nested
# .venv this line used to name was a `uv sync` in a checkout, which is not how
# anyone installs this any more.
#
# No --config. A paired rack's configuration comes from the server; the flag is
# optional since M3c and a unit naming a file would make every rack need one.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest daemon/tests/test_install.py -v`
Expected: 24 passed.

- [ ] **Step 6: Prove nothing escaped the sandbox**

```bash
uv run pytest daemon/tests/test_install.py -q
test -f /etc/systemd/system/openrackscreen.service && echo "LEAKED" || echo "clean"
id openrackscreen 2>/dev/null && echo "LEAKED" || echo "clean"
```

Expected: `clean` twice.

- [ ] **Step 7: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add daemon/
git commit -m "feat(daemon): install, against roots that are not this machine"
```

---

## Task 9: Wiring `install` and `uninstall` into the CLI

**Files:**
- Modify: `daemon/src/ors_daemon/__main__.py`
- Test: `daemon/tests/test_main_install.py` (create)

**Interfaces:**
- Consumes: `install.install`, `install.uninstall`, `install.Roots`.
- Produces: `ors-daemon install [--no-spi] [--prefix PATH] [--use-current-interpreter] [--upgrade]`, `ors-daemon uninstall [--purge]`. Both return shell exit codes.

- [ ] **Step 1: Write the failing test**

Create `daemon/tests/test_main_install.py`:

```python
"""The two subcommands that change a machine, and the one thing they check first."""

from __future__ import annotations

import pytest

from ors_daemon.__main__ import main


def test_install_without_root_refuses_and_changes_nothing(monkeypatch, capsys, tmp_path):
    """Exit 2, the conventional "you typed it wrong" code, with no partial
    state: a half-done install is worse than none, because the next thing it
    would have done is the thing that reports what went wrong."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    code = main(["install", "--prefix", str(tmp_path / "opt")])
    assert code == 2
    captured = capsys.readouterr()
    assert "root" in (captured.err + captured.out).lower()
    assert not (tmp_path / "opt").exists()


def test_uninstall_without_root_refuses(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert main(["uninstall"]) == 2


def test_install_help_names_every_flag(capsys):
    with pytest.raises(SystemExit):
        main(["install", "--help"])
    text = capsys.readouterr().out
    for flag in ("--no-spi", "--prefix", "--use-current-interpreter", "--upgrade"):
        assert flag in text


def test_uninstall_help_says_what_purge_costs(capsys):
    with pytest.raises(SystemExit):
        main(["uninstall", "--help"])
    text = capsys.readouterr().out
    assert "--purge" in text
    assert "approv" in text.lower()
```

Note `main(["install", "--help"])` raises `SystemExit` from argparse; `main`
catches it for parse *errors* but `--help` exits before dispatch. If the
existing `main` swallows it, assert on the return code instead — read
`daemon/src/ors_daemon/__main__.py:192-199` and match what is there.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest daemon/tests/test_main_install.py -v`
Expected: FAIL — argparse rejects the unknown command `install`.

- [ ] **Step 3: Add the subparsers**

In `_parser()`, after the `identify` block and **before** the `--config` loop:

```python
    install_command = subparsers.add_parser(
        "install", help="set this machine up to run the daemon as a service"
    )
    install_command.add_argument(
        "--no-spi",
        action="store_true",
        help="do not touch /boot/firmware/config.txt. Both SPI buses are enabled by "
        "default, because SPI1 being off is the most common reason every panel comes "
        "up unavailable and nothing reports it.",
    )
    install_command.add_argument(
        "--prefix",
        type=Path,
        default=Path("/opt/openrackscreen"),
        help="where to build the venv the service runs from (default: %(default)s)",
    )
    install_command.add_argument(
        "--use-current-interpreter",
        action="store_true",
        help="point the unit at the ors-daemon already running instead of building a "
        "venv. Refused if the service user could not execute it.",
    )
    install_command.add_argument(
        "--upgrade",
        action="store_true",
        help="reinstall into the prefix at this version and restart the service",
    )

    uninstall_command = subparsers.add_parser(
        "uninstall", help="stop the service and remove the unit and the venv"
    )
    uninstall_command.add_argument(
        "--purge",
        action="store_true",
        help="also delete /var/lib/openrackscreen, which holds the pairing and this "
        "rack's identity. That costs a re-approval in the interface.",
    )
```

Extend the `--config` exclusion loop:

```python
    for name, sub in subparsers.choices.items():
        if name not in ("connect", "install", "uninstall"):
            sub.add_argument("--config", type=Path, help="path to rack.yaml")
```

(`required=True` is dropped here; Task 10 owns that change and its tests. If
Task 10 has not run yet, leave `required=True` and add the two new names to the
exclusion only.)

- [ ] **Step 4: Dispatch**

In `main`, beside the existing `connect` and `run` branches:

```python
    if args.command in ("install", "uninstall"):
        # Checked here rather than inside `install.install`, which is a pure
        # function over injected roots and must stay callable from a test that
        # is not root -- which is every test in this suite.
        if os.geteuid() != 0:
            print(f"ors-daemon {args.command} has to run as root.", file=sys.stderr)
            return _USAGE_EXIT
        return _install(args) if args.command == "install" else _uninstall(args)
```

Write `_install` and `_uninstall` to build `Roots(etc=Path("/etc"),
boot=Path("/boot"), state=Path("/var/lib"), prefix=args.prefix,
systemd=Path("/etc/systemd/system"))`, a real `subprocess` runner, and print the
report — the short code, the SPI diff, and whether a reboot is owed.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest daemon/tests/ -v`
Expected: PASS, and the existing daemon suite is unchanged.

- [ ] **Step 6: Commit**

```bash
git add daemon/
git commit -m "feat(daemon): install and uninstall, with root checked before anything moves"
```

---

## Task 10: `--config` stops being mandatory

**Files:**
- Modify: `daemon/src/ors_daemon/__main__.py`
- Test: `daemon/tests/test_main.py` (or wherever `_boot` is currently tested — grep for `_boot` first)

**Interfaces:**
- Produces: `run` accepts no `--config`; `_boot` returns a config or `None` and `_run` branches on it.

- [ ] **Step 1: Write the failing test**

```python
def test_a_rack_with_no_pairing_and_no_config_enters_the_join_flow(tmp_path, monkeypatch):
    """This is the state a freshly installed rack is in, and it is not an
    error. Before M3c `--config` was required, so a rack that had never been
    paired could not be started at all -- which is the state `install` leaves
    the machine in."""
    joined = []
    monkeypatch.setattr("ors_daemon.__main__.join_a_server", lambda *a, **k: joined.append(True))
    code = main(["run", "--link", str(tmp_path / "link.json"), "--status", str(tmp_path / "s.json")])
    assert joined == [True]
    assert code == 0


def test_a_rack_with_no_way_to_get_a_configuration_says_so(tmp_path, capsys):
    """Discovery off and no --server is the one case with no answer. A message
    naming every way to fix it, not a traceback: the audience is somebody over
    SSH, and a traceback tells them about this program rather than about their
    machine."""
    code = main([
        "run",
        "--link", str(tmp_path / "link.json"),
        "--status", str(tmp_path / "s.json"),
        "--no-discovery",
    ])
    assert code != 0
    message = capsys.readouterr().err
    assert "--config" in message
    assert "--server" in message


def test_a_paired_rack_with_a_cached_snapshot_needs_no_config_file(tmp_path):
    """The behaviour that was already true and that the required flag hid: a
    paired rack that has been pushed to never read the YAML it was forced to
    supply."""
    # Build a link.json and a snapshot.json in tmp_path using the same helpers
    # the existing link tests use, then:
    code = main(["run", "--link", str(tmp_path / "link.json"), "--status", str(tmp_path / "s.json")])
    assert code == 0


def test_a_config_file_still_works_exactly_as_before(tmp_path):
    """Standalone racks are the daemon README's leading use case and M1/M2's
    whole story. Nothing about them changes."""
    code = main(["validate", "--config", str(EXAMPLE_CONFIG)])
    assert code == 0
```

Read `daemon/tests/` first and reuse the existing fixtures for link files and
snapshots rather than inventing new ones.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest daemon/tests/ -k config -v`
Expected: FAIL — argparse still requires `--config`.

- [ ] **Step 3: Implement**

- Drop `required=True` from the `--config` loop (Task 9 Step 3 already made the
  exclusion list correct).
- Add `--no-discovery` and `--server URL` to the `run` subparser.
- Rewrite `_boot`'s contract to the five rows of the spec's §5 table. Every
  branch gets a comment naming which row it is.
- The "no way to get a configuration" branch prints to **stderr** and returns
  non-zero.

- [ ] **Step 4: Add the supervisor-on-zero-screens test**

In `daemon/tests/test_hardware.py` (or wherever `Supervisor` is exercised):

```python
def test_a_supervisor_can_start_with_no_screens_and_gain_them(...):
    """The one genuinely new state in M3c: a paired rack that has not been
    pushed to yet holds no screens at all, and the panels appear when the
    server's first snapshot lands. `Supervisor` holds `_screens` as a list that
    `apply` replaces wholesale, so an empty one is *structurally* fine -- and
    "structurally fine" is an assumption until something starts one.
    """
    supervisor = Supervisor(screens=[], ...)
    supervisor.start()
    assert supervisor.status()["screens"] == []
    supervisor.apply(config_with_two_screens)
    assert len(supervisor.status()["screens"]) == 2
```

Match the real constructor and status shape; read the file first.

- [ ] **Step 5: Run everything**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add daemon/
git commit -m "feat(daemon): a rack whose configuration is the server's needs no file"
```

---

## Task 11: The rolling-window limiter, extracted

**Files:**
- Create: `server/src/ors_server/limiter.py`, `server/tests/test_limiter.py`
- Modify: `server/src/ors_server/auth.py`, `server/src/ors_server/api/auth.py`

**Interfaces:**
- Produces: `ors_server.limiter.Limiter(max_attempts: int, window_seconds: float)` with `too_many(key: str, now: float) -> bool`, `record(key: str, now: float) -> None`, `clear(key: str) -> None`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_limiter.py`:

```python
"""One rolling window, used by two endpoints with different budgets.

Extracted rather than reused in place: `Sessions.too_many_attempts` is about
password guesses and carries that window. The claim endpoint is unauthenticated
and its limit exists to stop a queue anybody can fill, which is a different
number for a different reason -- and sharing one counter would mean a rack
filing claims could lock an admin out of logging in.
"""

from __future__ import annotations

from ors_server.limiter import Limiter


def test_it_permits_up_to_the_limit():
    limiter = Limiter(max_attempts=3, window_seconds=60)
    for second in range(3):
        assert limiter.too_many("10.0.0.1", second) is False
        limiter.record("10.0.0.1", second)
    assert limiter.too_many("10.0.0.1", 3) is True


def test_the_window_rolls():
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    assert limiter.too_many("10.0.0.1", 59) is True
    assert limiter.too_many("10.0.0.1", 61) is False


def test_one_client_does_not_limit_another():
    """Otherwise a single noisy rack locks every admin out of the interface."""
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    assert limiter.too_many("10.0.0.2", 0) is False


def test_clearing_forgets_a_client():
    limiter = Limiter(max_attempts=1, window_seconds=60)
    limiter.record("10.0.0.1", 0)
    limiter.clear("10.0.0.1")
    assert limiter.too_many("10.0.0.1", 0) is False


def test_expired_attempts_are_not_kept_for_ever():
    """A dict keyed on address with an unbounded list per key is a memory leak
    an unauthenticated endpoint can drive."""
    limiter = Limiter(max_attempts=100, window_seconds=10)
    for second in range(50):
        limiter.record("10.0.0.1", second)
    limiter.too_many("10.0.0.1", 1000)
    assert limiter.size() == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest server/tests/test_limiter.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement, then re-point `Sessions`**

Write `Limiter`, then change `Sessions.too_many_attempts` / `record_attempt` /
`clear_attempts` to delegate to a `Limiter` instance. **Keep the three method
names**: `api/auth.py` calls them and its tests pin them; this task is an
extraction, not a rename.

- [ ] **Step 4: Run the whole server suite**

Run: `uv run pytest server/ -v`
Expected: PASS, including every existing `test_auth.py` case unchanged.

- [ ] **Step 5: Commit**

```bash
git add server/
git commit -m "refactor(server): one rolling window, for two endpoints that need different ones"
```

---

## Task 12: The claim store

**Files:**
- Create: `server/src/ors_server/claims.py`, `server/tests/test_claims.py`
- Modify: `server/src/ors_server/db.py`

**Interfaces:**
- Produces:
  - `ors_server.claims.MAX_PENDING: int = 32`, `CLAIM_LIFETIME_S: float = 1800`, `DENY_SUPPRESSION_S: float = 86400`
  - `ors_server.claims.Claim` — frozen dataclass: `id: str`, `hostname: str`, `address: str`, `fingerprint: str`, `short_code: str`, `version: str`, `public_key: str`, `first_seen: float`.
  - `file_claim(database, *, hostname, address, fingerprint, short_code, version, public_key, now) -> Claim | None` — `None` when suppressed.
  - `list_pending(database, now) -> list[Claim]`
  - `get_claim(database, claim_id, now) -> Claim | None`
  - `approve(database, claim_id, now) -> tuple[int, str] | None` — `(daemon_id, key)`, once.
  - `deny(database, claim_id, now) -> bool`
  - `count_pending(database, now) -> int`

- [ ] **Step 1: Add the table**

In `server/src/ors_server/db.py`, beside the existing schema:

```sql
CREATE TABLE IF NOT EXISTS claim (
    id           TEXT PRIMARY KEY,
    hostname     TEXT NOT NULL,
    address      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL UNIQUE,
    short_code   TEXT NOT NULL,
    version      TEXT NOT NULL,
    public_key   TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    -- Set when approved. The key is handed over exactly once and this column
    -- is cleared in the same transaction, so a second poll gets nothing --
    -- the same rule `pairing.claim_token` already follows for tokens.
    granted_key  TEXT,
    daemon_id    INTEGER REFERENCES daemon(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS denied_fingerprint (
    fingerprint  TEXT PRIMARY KEY,
    denied_at    REAL NOT NULL
);
```

`fingerprint` is `UNIQUE` on purpose: a rack that files a second claim while one
is pending replaces it rather than queueing twice, so a restarting daemon cannot
fill the queue by itself.

- [ ] **Step 2: Write the failing test**

Create `server/tests/test_claims.py` covering, one test each:

- A filed claim appears in `list_pending`.
- The same fingerprint filed twice yields **one** pending claim, and the newer
  `address`/`version` win — a rack that moved or upgraded is the same rack.
- `MAX_PENDING` is enforced; the 33rd claim is refused and the existing 32 are
  untouched. *(A cap that evicted the oldest would let a flood hide a real rack,
  which is the attack the cap exists for.)*
- A claim older than `CLAIM_LIFETIME_S` is absent from `list_pending` and from
  `get_claim`.
- Expiry frees a slot: 32 pending, advance past the lifetime, a new claim is
  accepted.
- `approve` returns `(daemon_id, key)` and creates a `daemon` row.
- `approve` twice returns `None` the second time and the key is gone from the
  row. *(The one-time handover.)*
- `deny` removes the claim and records the fingerprint.
- A denied fingerprint filing again within `DENY_SUPPRESSION_S` gets `None`.
- After `DENY_SUPPRESSION_S` it is accepted again.
- The three constants are pinned by value:
  `assert (MAX_PENDING, CLAIM_LIFETIME_S, DENY_SUPPRESSION_S) == (32, 1800.0, 86400.0)`
  with a comment saying each is a decision, not an accident.

Use the existing `Database` fixture from `server/tests/`; read `test_auth.py`
for the pattern.

- [ ] **Step 3: Run it, watch it fail, implement, run again**

Run: `uv run pytest server/tests/test_claims.py -v`

- [ ] **Step 4: Commit**

```bash
git add server/
git commit -m "feat(server): pending claims, with a cap a flood cannot hide a rack behind"
```

---

## Task 13: The claim routes

**Files:**
- Create: `server/src/ors_server/api/claims.py`, `server/tests/test_api_claims.py`
- Modify: `server/src/ors_server/app.py`, `server/src/ors_server/api/__init__.py`
- Modify: `server/pyproject.toml` (no new dependency — `cryptography` is already there)

**Interfaces:**
- Consumes: `claims.*` (Task 12), `limiter.Limiter` (Task 11).
- Produces:
  - `POST /api/racks/claims` → 202 `{claim_id}`; 429 when limited or the queue is full; 403 when suppressed.
  - `GET /api/racks/claims/{claim_id}` → `{status}` where status is `pending` / `denied` / `approved`; on `approved`, also `{ephemeral_public_key, nonce, ciphertext}`. The claim id **is** the credential: see Task 6's spec correction.
  - `GET /api/claims` → the pending list. **Session-authenticated.**
  - `POST /api/claims/{claim_id}/approve` → `{id, name}`. Session-authenticated.
  - `POST /api/claims/{claim_id}/deny` → `{ok: true}`. Session-authenticated.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_api_claims.py`. Cover, one test each:

- Filing a claim answers 202 with an id, **without a session cookie**. *(It must:
  a daemon that has not been approved holds no credential.)*
- The recorded address is the connection's, **not** a field in the body: file a
  claim whose body carries `"address": "10.9.9.9"` and assert the stored address
  is the test client's. *(A field the claimant fills in is a field the claimant
  chooses.)*
- The 33rd claim answers 429.
- The rate limiter refuses a burst from one address before the store is touched.
- `GET /api/claims` without a session answers 401.
- `GET /api/claims` with a session lists what was filed.
- Approving without a session answers 401.
- Approving with a session creates the rack.
- Polling a claim id that was never issued answers 404, and answers it the same
  way whether the id is malformed or merely unknown. *(An endpoint that
  distinguished the two would confirm ids to somebody enumerating them.)*
- The claim id is not in the body of `GET /api/claims`. *(It is the credential;
  putting it in the admin's list would publish every pending rack's capability
  to anything that could read one page.)*
- Polling an approved claim returns the ciphertext once; the second poll returns
  `approved` with **no** ciphertext.
- The daemon key round-trips: decrypt in the test with the private key the test
  generated and assert it authenticates via `pairing.authenticate_key`. *(This is
  the end-to-end assertion — everything else could pass with an encryption that
  produces garbage.)*
- Denying suppresses: deny, re-file, expect 403.

- [ ] **Step 2: Run it and watch it fail**

- [ ] **Step 3: Implement**

The key handover, written out because it is the one piece of new cryptography:

```python
def _seal(daemon_key: str, daemon_public_key_b64: str) -> dict[str, str]:
    """Encrypt the rack's key to the ephemeral key it filed its claim with.

    Without this the key crosses the LAN in cleartext over plain HTTP, exactly
    as the pasted token it replaces does. "No worse than the thing we are
    replacing" is not a good enough bar for a protocol being designed from
    scratch, and `cryptography` is already a dependency of this package.

    X25519 rather than sealing to a long-lived key: the claim's key pair lives
    for one claim, so a secret recovered from a Pi later does not decrypt a
    handover that was recorded earlier.
    """
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(daemon_public_key_b64))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(peer)
    # HKDF and not the raw shared secret: the output of X25519 is not uniform,
    # and AESGCM wants a key that is.
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ors-claim-v1").derive(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, daemon_key.encode(), None)
    return {
        "ephemeral_public_key": base64.b64encode(
            ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
```

The claim id is generated with `secrets.token_urlsafe(32)` and looked up with
`hmac.compare_digest` against the stored value rather than by SQL equality where
that is practical — a byte-at-a-time comparison on a bearer credential leaks its
prefix to anyone who can time the endpoint.

See Task 6 for why there is no signature header: the server holds only
`sha256(secret)` and can verify nothing computed under the secret itself. The
claim id is the credential, and confidentiality rests on the X25519 seal rather
than on the id staying secret.

- [ ] **Step 4: Run the tests, then the whole suite**

- [ ] **Step 5: Commit**

```bash
git add server/ docs/
git commit -m "feat(server): a claim anyone may file and only one daemon may collect"
```

---

## Task 14: mDNS — announce and discover

**Files:**
- Create: `server/src/ors_server/announce.py`, `server/tests/test_announce.py`, `daemon/src/ors_daemon/discovery.py`, `daemon/tests/test_discovery.py`
- Modify: `server/pyproject.toml`, `daemon/pyproject.toml` (add `zeroconf>=0.132`), `server/src/ors_server/app.py`

**Interfaces:**
- Produces:
  - `ors_server.announce.SERVICE_TYPE: str = "_openrackscreen._tcp.local."`
  - `ors_server.announce.Announcer(port: int, version: str, zeroconf_factory=...)` with `start()`, `stop()`.
  - `ors_daemon.discovery.discover(timeout: float, browser_factory=...) -> list[Found]` where `Found` has `host: str`, `port: int`, `version: str`, and `url` property.

- [ ] **Step 1: Write the failing tests**

Both sides stub `zeroconf` at one seam — a factory parameter — so no test opens
a socket or sends a multicast packet. Cover:

- The announced service type is exactly `_openrackscreen._tcp.local.`, pinned by
  value. *(A typo here is a rack that never finds anything, with no error
  anywhere.)*
- The TXT record carries the version and the scheme.
- `stop()` unregisters. *(A server that restarted without unregistering leaves a
  stale record and racks dial a port nothing is on.)*
- Discovery with one server returns one entry with the right URL.
- Discovery with two servers returns **both**, sorted deterministically. *(The
  daemon pairs with none of them; `--server` settles it. A discovery that picked
  one would pick a different one on each boot.)*
- Discovery with none returns `[]` after the timeout and does not raise.
- A TXT record missing its version does not crash discovery; the entry carries
  `""`. *(Another implementation on the network is not this project's problem to
  crash on.)*

- [ ] **Step 2: Run, fail, implement, run**

- [ ] **Step 3: Start and stop the announcer with the app**

In `server/src/ors_server/app.py`, register `Announcer.start` on startup and
`stop` on shutdown, guarded by `ORS_ANNOUNCE=0` to switch it off. Add a test that
the app starts and stops cleanly with announcing disabled, since every existing
`TestClient` test will now construct one.

- [ ] **Step 4: Check the dependency actually installs on the target**

```bash
uv add --package ors-daemon "zeroconf>=0.132"
uv run python -c "import zeroconf; print(zeroconf.__version__)"
```

Confirm from the lock that the resolved version publishes a pure-Python wheel or
an aarch64 one. `zeroconf` ships optional C speedups; if the resolved version has
**no** aarch64 wheel, pin below it and say so in the commit.

- [ ] **Step 5: Commit**

```bash
git add server/ daemon/ pyproject.toml uv.lock
git commit -m "feat: a server that says where it is, and a rack that listens for one"
```

---

## Task 15: The daemon joins

**Files:**
- Create: `daemon/src/ors_daemon/join.py`, `daemon/tests/test_join.py`
- Modify: `daemon/src/ors_daemon/__main__.py`, `daemon/pyproject.toml` (add `cryptography>=43.0`)

**Interfaces:**
- Consumes: `identity.load_or_create`, `discovery.discover`, the routes from Task 13.
- Produces: `ors_daemon.join.join_a_server(*, identity, servers, link_path, sleeper, http) -> bool`.

- [ ] **Step 1: Write the failing test**

Cover:

- A claim is filed carrying the fingerprint, short code, hostname, version and a
  **fresh** public key. *(Assert two calls produce two different public keys —
  a reused key is the thing X25519-per-claim exists to avoid.)*
- On `approved`, the key decrypts and is written to the link file, and the
  function returns `True`.
- On `denied`, it stops and returns `False` rather than re-filing immediately.
  *(A denied rack that reappears every five seconds trains people to click
  Approve.)*
- A 429 backs off and retries; assert the sleeper was called with increasing
  values.
- An expired claim (404 on poll) files a **new** claim rather than waiting for
  ever.
- Two discovered servers: it files with **neither** and says so.
- Ciphertext that does not decrypt is an error, not a written link file. *(A
  corrupt key silently saved is a rack that dials for ever with a credential
  nothing accepts.)*

- [ ] **Step 2: Run, fail, implement, run**

- [ ] **Step 3: Wire it into `run`'s join branch from Task 10**

- [ ] **Step 4: Commit**

```bash
git add daemon/ uv.lock
git commit -m "feat(daemon): file a claim, wait to be let in, and keep the key that arrives"
```

---

## Task 16: "Waiting to join" in the interface

**Files:**
- Create: `web/src/routes/daemons/PendingClaims.tsx`, `web/src/routes/daemons/ApproveClaimDialog.tsx`, `web/src/routes/daemons/DenyClaimDialog.tsx`, `web/tests/claims.test.tsx`
- Modify: `web/src/api/queries.ts`, `web/src/routes/daemons/DaemonsPage.tsx`, `web/src/api/schema.d.ts` (regenerate)

**Interfaces:**
- Consumes: `GET /api/claims`, `POST /api/claims/{id}/approve`, `POST /api/claims/{id}/deny`.
- Produces: `claimsKey`, `useClaims()`, `useApproveClaim()`, `useDenyClaim()` in `queries.ts`, following the existing `useMutate` shape.

- [ ] **Step 1: Regenerate the API types**

```bash
uv run ors-server &   # with ORS_DATA_DIR set to a tmp dir
cd web && pnpm generate:types
```

- [ ] **Step 2: Write the failing test**

`web/tests/claims.test.tsx`, following `web/tests/daemons.test.tsx`'s fixtures
and `renderApp` harness. Cover:

- The section is absent when there are no pending claims. *(An empty "Waiting to
  join" heading is a landmark that leads a screen reader nowhere — the same rule
  `RackCanvas` follows for an empty rack.)*
- Each entry shows hostname, address, short code and version.
- The approve dialog shows the short code and says it must match the Pi.
- Approving calls the route and the entry leaves the list.
- Denying says suppression lasts 24 hours before it happens.
- A claim arriving over `/ws/ui` appears without a reload.
- Two claims with **different** hostnames and **different** codes render
  distinctly. *(An identity fixture where both coincide would hide a component
  keying on the wrong field — this suite has been bitten by that twice.)*

- [ ] **Step 3: Run, fail, implement, run**

Run: `pnpm test && pnpm typecheck && pnpm lint && pnpm build`

- [ ] **Step 4: Commit**

```bash
git add web/
git commit -m "feat(web): the racks asking to join, and the code you check before saying yes"
```

---

## Task 17: End to end, by claim

**Files:**
- Modify: `web/e2e/rack.spec.ts`, `web/e2e/fixture.ts`, `web/e2e/virtual_rack.py`

- [ ] **Step 1: Extend the fixture**

Give `virtual_rack.py` a mode that files a claim against the server under test
instead of consuming a token, using the real `join.py` path.

- [ ] **Step 2: Write the failing spec**

A new spec between the existing 1 and 2: the rack appears under "Waiting to
join", the short code shown matches the one the daemon printed, approving it
makes it a rack, and the existing spec 3 onwards still passes against a rack
that joined this way.

- [ ] **Step 3: Run**

Run: `cd web && pnpm exec playwright test`
Expected: 8 passed.

- [ ] **Step 4: Commit**

```bash
git add web/
git commit -m "test(e2e): a rack that joins by being approved, not by being told a token"
```

---

## Task 18: The documentation this milestone exists for

**Files:**
- Modify: `README.md`, `daemon/README.md`, `server/README.md`

- [ ] **Step 1: Rewrite `README.md`'s install sections**

`## Run it for real` becomes three paths in this order: **Install** (`uv tool
install ors-server`), **Docker**, **From source**. `## First run` loses the
token paste and gains: install the daemon, run `sudo ors-daemon install`, open
the interface, approve the rack whose code matches.

- [ ] **Step 2: Rewrite `daemon/README.md`'s `## Install`**

`uv tool install "ors-daemon[hardware]"`, then `sudo ors-daemon install`. The
manual `config.txt` section stays but becomes "what `install` does for you, and
what to check if it did not".

- [ ] **Step 3: Update `server/README.md`'s environment table**

`ORS_DATA_DIR` and `ORS_WEB_DIR` defaults both changed; the table is wrong until
this step.

- [ ] **Step 4: Verify every command from a fresh clone**

```bash
git clone https://github.com/SilkePilon/OpenRackScreen /tmp/ors-readme-check
cd /tmp/ors-readme-check
# Run every fenced command in the three READMEs that does not need a Pi.
```

Anything that does not work as written is a documentation bug **and possibly a
code bug**; fix whichever it turns out to be. This step found two real errors
last milestone, one of them a compose file that never built the checkout.

- [ ] **Step 5: Commit**

```bash
git add README.md daemon/README.md server/README.md
git commit -m "docs: an install that starts at an index and ends at an approved rack"
```

---

## Task 19: `ors-server install`

Spec §3's third path. **Orderable any time after Task 8**, which is where the
generic install machinery is written; it is last here only because nothing else
depends on it.

**Files:**
- Create: `server/src/ors_server/install.py`, `server/tests/test_install.py`
- Modify: `server/src/ors_server/__main__.py`

**Interfaces:**
- Consumes: nothing from `ors_daemon.install` — the two do not share code. See
  Step 1.
- Produces: `ors-server install [--prefix PATH] [--port N]`, `ors-server uninstall [--purge]`, and `ors_server.install.unit_text(exec_start: str, data_dir: Path, port: int) -> str`.

- [ ] **Step 1: Decide the duplication deliberately, and write the decision down**

`ors_daemon.install` and `ors_server.install` do the same five things — user,
directories, venv, unit, enable — and it is tempting to lift them into a shared
module. **Do not.** `ors-daemon` and `ors-server` are separately installable
distributions; a shared module would have to live in `ors-schema` or a sixth
package, which would mean the server depending on a library whose reason for
existing is the daemon's systemd unit. The two units differ in every line that
matters: the daemon needs `SupplementaryGroups=spi gpio`, must not have
`PrivateDevices=yes`, and needs `TimeoutStopSec=30` so four panels can be slept;
the server needs none of those and does need `ORS_DATA_DIR`.

Put this reasoning in the module docstring. A later reader who does not find it
will do the merge.

- [ ] **Step 2: Write the failing test**

`server/tests/test_install.py`, structured exactly like
`daemon/tests/test_install.py` — same `Roots`, same `FakeRunner`, same rule that
nothing outside `tmp_path` is touched. Cover:

- The data directory is created and is **0700**. *(It holds the secret key and
  every stored integration credential.)*
- The unit sets `ORS_DATA_DIR` explicitly. *(Task 3 moved the code default to
  the user's state directory; a unit relying on the default would put the
  database in a system user's home and every restart would be a fresh server
  with no password set.)*
- The unit does **not** carry `SupplementaryGroups`, `TimeoutStopSec=30`, or
  anything about SPI. *(The assertion that the two units did not get merged.)*
- `PrivateDevices=yes` **is** present. *(The opposite of the daemon's rule, and
  the reason sharing the template would be wrong: the server touches no
  hardware.)*
- The port reaches both the unit and the healthcheck line.
- A second run changes nothing.
- `uninstall` leaves the data directory; `--purge` removes it and warns that the
  admin password, the secret key and every stored credential go with it.

- [ ] **Step 3: Run, fail, implement, run**

Run: `uv run pytest server/tests/test_install.py -v`

- [ ] **Step 4: Add the subcommands**

`ors_server.__main__` currently has no subparsers at all — `main()` reads the
environment and calls `uvicorn.run`. Adding `install` means adding an
`ArgumentParser` where there was none, so **bare `ors-server` with no arguments
must keep running the server**: that is what the Dockerfile's `CMD` does, what
`server/README.md` documents, and what every deployment already invokes. Add a
test for exactly that before writing the parser.

- [ ] **Step 5: Commit**

```bash
git add server/
git commit -m "feat(server): install, for a server that survives a reboot without Docker"
```

---

## Definition of done

All six gates, run from the repository root:

```bash
uv run pytest                                    # all pass
uv run ruff check . && uv run ruff format --check .
cd web && pnpm test && pnpm typecheck && pnpm lint && pnpm build
cd web && pnpm exec playwright test              # 8 passed
docker compose -f deploy/compose.pi.yaml up -d && curl -fsS localhost:8080/ | head -c 100
uv build --all-packages --out-dir /tmp/ors-dist && \
  python -c "import zipfile,glob;print('ors_server/web/index.html' in zipfile.ZipFile(glob.glob('/tmp/ors-dist/ors_server-*.whl')[0]).namelist())"
```

The last must print `True`.

## What is still not verified after this plan

- **arm64 has never been built.** Neither the image nor the wheels.
- **No real Pi.** `install` is tested against injected roots; nothing has run it
  on hardware, created a real user, or rebooted with an edited `config.txt`.
- **Trusted Publishing** must be configured by a human on PyPI for all five
  projects before the first tag can publish.
- **mDNS on a real network.** Every discovery test stubs `zeroconf`.

## What M4 picks up

Unchanged: the Jellyfin, \*arr, qBittorrent and Grafana integrations; the visual
template editor; the workflow builder; `frames_dropped`; whether
`sleep`/`wake`/`reload` become real commands; and the arm64 image.
