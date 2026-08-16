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
_NAME = re.compile(r'^name = "[^"]*"$', re.MULTILINE)


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
