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
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]

PACKAGES = {
    "ors-schema": ROOT / "packages/ors-schema/pyproject.toml",
    "ors-render": ROOT / "packages/ors-render/pyproject.toml",
    "ors-daemon": ROOT / "daemon/pyproject.toml",
    "ors-server": ROOT / "server/pyproject.toml",
    "openrackscreen": ROOT / "packages/openrackscreen/pyproject.toml",
}

VERSION_MODULES = {
    "ors-schema": ROOT / "packages/ors-schema/src/ors_schema/__init__.py",
    "ors-render": ROOT / "packages/ors-render/src/ors_render/__init__.py",
    "ors-daemon": ROOT / "daemon/src/ors_daemon/__init__.py",
    "ors-server": ROOT / "server/src/ors_server/__init__.py",
}
"""Every `__version__` constant that has to move in lockstep with the
distribution it lives in. `openrackscreen` has no module of its own (its
`pyproject.toml` says so: "Deliberately empty of modules"), so it has no entry
here. These are user-visible independently of the wheel's own metadata: the
server hands its `__version__` back from `/api/health`, and the daemon sends
its `__version__` as `daemon_version` in the link's `Hello` -- the server's
only record of what the daemon on the other end thinks it is running."""

_VERSION = re.compile(r'^version = "[^"]*"$', re.MULTILINE)
_NAME = re.compile(r'^name = "[^"]*"$', re.MULTILINE)
_DUNDER_VERSION = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)


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
        # The pin pass below must not touch the package's own `name = "..."`
        # line: every published name here is also a key in `PACKAGES`, so
        # without this guard `name = "ors-schema"` becomes
        # `name = "ors-schema==0.2.0"`, an invalid project name. Protect the
        # line by pinning every line except the ones that match `_NAME`.
        lines = updated.split("\n")
        for i, line in enumerate(lines):
            if _NAME.fullmatch(line):
                continue
            for name in PACKAGES:
                line = _pin(name).sub(f'"{name}=={version}"', line)
            lines[i] = line
        updated = "\n".join(lines)
        if updated != text:
            path.write_text(updated)
            changed.append(path)
    for path in VERSION_MODULES.values():
        text = path.read_text()
        updated = _DUNDER_VERSION.sub(f'__version__ = "{version}"', text, count=1)
        if updated != text:
            path.write_text(updated)
            changed.append(path)
    return changed


def read_versions() -> dict[str, str]:
    return {
        name: tomllib.loads(path.read_text())["project"]["version"]
        for name, path in PACKAGES.items()
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python tools/version.py <version>", file=sys.stderr)
        return 2
    try:
        Version(argv[0])
    except InvalidVersion as exc:
        print(f"invalid version {argv[0]!r}: {exc}", file=sys.stderr)
        return 2
    for path in rewrite(argv[0]):
        print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
