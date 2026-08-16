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


def _release_workflow() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


def _find_step(steps: list[dict], predicate, description: str) -> dict:
    """`next(...)` with no default raises a bare `StopIteration` on a missing
    step -- indistinguishable, in a test report, from any other bug in the
    generator. Renaming the step this is looking for should fail with a
    message that says what's missing, not a stack trace with no assertion in
    it."""
    step = next((s for s in steps if predicate(s)), None)
    assert step is not None, f"no step found: {description}"
    return step


def test_the_release_workflow_refuses_a_wheel_without_the_interface():
    """The gate that stops §2.3's failure reaching an index.

    A published `ors-server` wheel with no `ors_server/web/index.html` in it
    installs cleanly and serves no pages. That is not recoverable by yanking:
    somebody has already installed it. This asserts the check's *behaviour*
    -- what it actually runs, and that nothing can silence it -- rather than
    a step name and a position, either of which survives a mutation that
    guts the check (e.g. `continue-on-error: true` with the body replaced by
    `echo skipping`) while leaving a name-and-order-only test green.
    """
    document = _release_workflow()
    steps = document["jobs"]["build"]["steps"]

    check = _find_step(steps, lambda s: "no interface" in s.get("name", ""), "interface gate")
    upload = _find_step(
        steps,
        lambda s: s.get("uses", "").startswith("actions/upload-artifact"),
        "upload-artifact step",
    )

    assert "continue-on-error" not in check, (
        "the interface gate must not be allowed to fail silently"
    )
    assert "if" not in check, "the interface gate must not be conditionally skipped"
    assert "ors_server/web/index.html" in check["run"], (
        "the interface gate must actually inspect the wheel for the interface"
    )

    # Ordering asserted on the step list itself, not a text search of the
    # file. A text search for "pypi.org" passes whatever the workflow does --
    # `pypa/gh-action-pypi-publish` does not contain that string -- which is
    # a test that reads like a gate and is not one.
    assert steps.index(check) < steps.index(upload), (
        "the interface check must run before the artifact is uploaded"
    )

    needs = document["jobs"]["publish"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    # `"build" in needs` alone is a substring match when `needs` is a bare
    # string: it also accepts a job named `buildx`. Normalise to a list
    # first, so membership means membership.
    assert "build" in needs, "publish must not run unless build succeeded"


def test_the_release_workflow_refuses_a_wheel_with_a_local_path_dependency():
    """Spec §9: `[tool.uv.sources]` is uv-only and should never reach a
    published `Requires-Dist`, but "should" is how a `file://` requirement
    gets published and fails to install for everyone outside this workspace.
    Mirrors the interface gate's test above: asserts the check inspects
    wheel metadata, and that nothing can silence it -- not merely that a step
    with a plausible name exists somewhere in the file.
    """
    document = _release_workflow()
    steps = document["jobs"]["build"]["steps"]
    check = _find_step(steps, lambda s: "local path" in s.get("name", ""), "local-path gate")

    assert "continue-on-error" not in check, (
        "the local-path gate must not be allowed to fail silently"
    )
    assert "if" not in check, "the local-path gate must not be conditionally skipped"
    assert "Requires-Dist" in check["run"], (
        "the local-path gate must actually inspect wheel metadata for Requires-Dist"
    )


def test_the_release_workflow_uploads_and_downloads_the_same_artifact_name():
    """A rename on either side of the `build` / `publish` boundary makes
    `download-artifact` fetch nothing. `publish` then runs against an empty
    `dist/`, and `pypa/gh-action-pypi-publish` over zero files is a workflow
    that reports green having published nothing.
    """
    document = _release_workflow()
    upload = _find_step(
        document["jobs"]["build"]["steps"],
        lambda s: s.get("uses", "").startswith("actions/upload-artifact"),
        "upload-artifact step",
    )
    download = _find_step(
        document["jobs"]["publish"]["steps"],
        lambda s: s.get("uses", "").startswith("actions/download-artifact"),
        "download-artifact step",
    )
    assert upload["with"]["name"] == download["with"]["name"], (
        "build must upload the exact artifact name publish downloads"
    )


def test_the_release_workflow_only_triggers_on_version_tags():
    """YAML 1.1's bareword booleans mean `on:` parses under `yaml.safe_load`
    to the boolean key `True`, not the string `"on"` -- a trap for anyone
    reading this trigger back out with `document["on"]`, which raises
    `KeyError` and checks nothing.

    Firing on a branch push instead of a tag push would let CI attempt to
    publish on every commit merged to a branch, and publishing is
    irreversible per version.
    """
    document = _release_workflow()
    trigger = document[True]
    assert isinstance(trigger, dict) and trigger.get("push", {}).get("tags") == ["v*"], (
        "release must trigger only on pushes of version tags (on: push: tags: ['v*'])"
    )


def test_the_release_workflow_checks_the_tag_matches_the_version_before_publishing():
    """`tools/version.read_versions` is the one place this repository already
    knows what version every package believes it is. Publishing a tag that
    disagrees with the tree ships a version number the tag on GitHub and the
    version on PyPI will forever disagree about.
    """
    document = _release_workflow()
    check = _find_step(
        document["jobs"]["build"]["steps"],
        lambda s: "tag matches the version" in s.get("name", ""),
        "tag-matches-version gate",
    )
    assert "read_versions" in check["run"], (
        "the tag/version gate must actually compare against tools.version.read_versions"
    )


def test_the_release_meta_package_publishes_last():
    """`pypa/gh-action-pypi-publish` runs one `twine upload` per step here,
    sequentially, aborting on the first failure. `openrackscreen` pins
    `ors-daemon` and `ors-server`; if it published before either, a failure
    partway through would leave a meta-package on PyPI pinning a dependency
    that never made it there -- a version that can never resolve and, once
    published, can never be re-uploaded. Every package with dependents must
    publish after all of them.
    """
    document = _release_workflow()
    publish_steps = [
        step
        for step in document["jobs"]["publish"]["steps"]
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish")
    ]
    assert publish_steps, "publish must actually publish something"

    order = [step["with"]["packages-dir"].strip("/").rsplit("/", 1)[-1] for step in publish_steps]
    assert order[-1] == "openrackscreen", "the meta-package must publish last"
    assert order.index("ors-schema") < order.index("ors-render"), (
        "ors-render depends on ors-schema and must publish after it"
    )
    for dependent in ("ors-daemon", "ors-server"):
        assert order.index("ors-render") < order.index(dependent), (
            f"{dependent} depends on ors-render and must publish after it"
        )
        assert order.index(dependent) < order.index("openrackscreen"), (
            f"openrackscreen depends on {dependent} and must publish after it"
        )
