"""The five distributions, and the one number they all have to agree on.

`ors-daemon` declares `Requires-Dist: ors-schema` today with no version at all,
which lets pip pair a new daemon with an old schema. The link protocol between
them is the one place in this project where a mismatch is silent: a daemon that
half-understands a snapshot draws a rack rather than refusing one.

`PACKAGES` and `VERSION_MODULES` are imported from `tools.version` rather than
redeclared here: a second, hand-copied dict is exactly the kind of thing that
silently stops covering a package the day somebody edits one copy and not the
other. There is exactly one list of what this repository publishes, and
`tools/version.py` is where the rewriting already has to know it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from tools.version import PACKAGES, VERSION_MODULES

ROOT = Path(__file__).resolve().parents[1]

TARGET_VERSION = "0.2.0"
"""The version this milestone ships. Not just "all five agree with each
other" -- they also have to agree with the number the release is actually
tagged, or a `uv run python tools/version.py 0.3.0` that nobody re-ran against
this constant would pass every other test in this file."""

TARGET_REQUIRES_PYTHON = ">=3.11"
"""The one Python constraint stated for the whole project. Mutating a single
distribution's `requires-python` (the meta-package to `>=3.12`, say) is
otherwise invisible: nothing installs it locally to notice, and nothing else
in this file reads that key."""

DEPENDENCY_EDGES = {
    "ors-schema": set(),
    "ors-render": {"ors-schema"},
    "ors-daemon": {"ors-schema", "ors-render"},
    "ors-server": {"ors-schema", "ors-render"},
    "openrackscreen": {"ors-daemon", "ors-server"},
}
"""The intra-project `dependencies` edges every distribution is expected to
declare. Pinning is only half the contract: a wheel that dropped
`Requires-Dist: ors-schema` entirely, rather than merely unpinning it, is
worse than an unpinned one, and nothing that only checks *pins on requirements
that exist* would ever notice a requirement that no longer does."""

_DUNDER_VERSION = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)


def _document(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _requirements(document: dict) -> list[str]:
    """Every requirement string a distribution can ship: its unconditional
    `dependencies`, plus every extra's list under `optional-dependencies`. An
    intra-project name added to an extra (`daemon`'s `hardware`, today all
    third-party) would ship unpinned if only `dependencies` were read here --
    the rewrite script's line-based pin pass does not make that distinction,
    so the test that checks its work should not either."""
    project = document["project"]
    requirements = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirements.extend(extra_requirements)
    return requirements


def _pins_exact_version(raw_requirement: str, name: str, version: str) -> bool | None:
    """Whether `raw_requirement` is a requirement on `name`, and if so, whether
    it pins that requirement to exactly `version`. `None` if the requirement is
    on some other package, so a caller can tell "not applicable" apart from
    "applicable and wrong".

    Parsed with `packaging.requirements.Requirement` rather than
    `raw_requirement.startswith(name)`: a raw prefix match accepts
    `"ors-schema-extra==1.0"` as a requirement *on* `ors-schema` (it is not --
    it is a different package that happens to share a prefix), and separately
    rejects `"ors-schema[extra]==0.2.0"` as *not* pinning it (it does -- the
    extras marker sits between the name and the exact string a prefix match
    goes looking for). Both are wrong in opposite directions, and this project
    has never needed either: today's fixtures don't exercise them, which is
    exactly why `test_pins_exact_version_matches_by_name_not_by_prefix` below
    constructs them rather than reading them out of a real `pyproject.toml`.
    """
    requirement = Requirement(raw_requirement)
    if requirement.name != name:
        return None
    return requirement.specifier == SpecifierSet(f"=={version}")


def test_packages_are_the_five_published_distributions():
    # `PACKAGES` is imported from `tools.version` rather than redeclared here
    # (see the module docstring), which closes the divergence that let the two
    # copies drift. It opens a narrower one: because every test in this file
    # iterates whatever `tools.version.PACKAGES` happens to contain, a name
    # quietly dropped from *that* single dict would shrink every test below
    # along with it rather than fail one. This is the one check anchored to a
    # name list that lives outside `tools/version.py` entirely.
    assert set(PACKAGES) == {
        "ors-schema",
        "ors-render",
        "ors-daemon",
        "ors-server",
        "openrackscreen",
    }


def test_version_modules_are_the_four_with_a_dunder_version():
    assert set(VERSION_MODULES) == {"ors-schema", "ors-render", "ors-daemon", "ors-server"}


def test_every_published_package_has_a_pyproject():
    for name, path in PACKAGES.items():
        assert path.is_file(), f"{name} is published but {path} does not exist"


def test_all_five_versions_agree():
    versions = {name: _document(path)["project"]["version"] for name, path in PACKAGES.items()}
    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"


def test_all_five_versions_equal_the_target_version():
    versions = {name: _document(path)["project"]["version"] for name, path in PACKAGES.items()}
    assert set(versions.values()) == {TARGET_VERSION}, f"versions: {versions}"


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_requires_python_matches_the_target_constraint(name: str):
    document = _document(PACKAGES[name])
    assert document["project"]["requires-python"] == TARGET_REQUIRES_PYTHON


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_intra_project_dependencies_pin_the_shared_version(name: str):
    # Not `>=`: the failure this guards is a *newer* schema under an older
    # daemon as much as the reverse, and only `==` refuses both.
    document = _document(PACKAGES[name])
    version = document["project"]["version"]
    for raw in _requirements(document):
        for other in PACKAGES:
            pinned = _pins_exact_version(raw, other, version)
            if pinned is not None:
                assert pinned, f"{name} requires {raw!r}, expected {other}=={version}"


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_intra_project_dependency_edges_match_expected(name: str):
    document = _document(PACKAGES[name])
    dependencies = document["project"].get("dependencies", [])
    edges = {Requirement(raw).name for raw in dependencies} & set(PACKAGES)
    assert edges == DEPENDENCY_EDGES[name], (
        f"{name} depends on {sorted(edges)}, expected {sorted(DEPENDENCY_EDGES[name])}"
    )


def test_pins_exact_version_matches_by_name_not_by_prefix():
    # The regression this guards: the pin check used to read
    # `raw_requirement.startswith(name)`. `"ors-schema-extra==1.0"` starts with
    # `"ors-schema"`, so a raw prefix match wrongly treats an unrelated package
    # as a requirement *on* `ors-schema` -- this asserts it is correctly
    # treated as unrelated (`None`, not applicable) instead.
    assert _pins_exact_version("ors-schema-extra==1.0", "ors-schema", "0.2.0") is None

    # `"ors-schema[extra]==0.2.0"` also starts with `"ors-schema"`, but the old
    # check then demanded the exact string `"ors-schema==0.2.0"`, which a
    # requirement carrying an extras marker never equals -- wrongly rejecting a
    # requirement that pins the right thing correctly. This asserts it is
    # correctly recognised as a requirement on `ors-schema`, pinned right.
    assert _pins_exact_version("ors-schema[extra]==0.2.0", "ors-schema", "0.2.0") is True

    # And the ordinary case, unpinned: recognised, and reported as wrong.
    assert _pins_exact_version("ors-schema", "ors-schema", "0.2.0") is False


@pytest.mark.parametrize("name", sorted(VERSION_MODULES))
def test_module_version_matches_its_distributions_version(name: str):
    # The `/api/health` response and the daemon's `Hello.daemon_version` both
    # come from this constant, not from the wheel's own metadata -- a
    # distribution can be 0.2.0 on PyPI while announcing 0.1.0 over the wire.
    version = _document(PACKAGES[name])["project"]["version"]
    module_path = VERSION_MODULES[name]
    match = _DUNDER_VERSION.search(module_path.read_text())
    assert match, f'{module_path} has no `__version__ = "..."` assignment'
    assert match.group(1) == version, (
        f"{module_path} __version__ is {match.group(1)!r}, expected {version!r}"
    )


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
    assert (
        document["jobs"]["publish"]["needs"] == "build"
        or "build" in document["jobs"]["publish"]["needs"]
    ), "publish must not run unless build succeeded"
