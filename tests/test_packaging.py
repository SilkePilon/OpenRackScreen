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
