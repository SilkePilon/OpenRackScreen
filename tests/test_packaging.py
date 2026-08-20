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
import subprocess
import sys
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
        # The release workflow's "Group distributions by package" step derives
        # its target directory from this `PACKAGES` key, but the *actual*
        # built filename comes from `project.name` -- `uv build` names the
        # wheel after the latter, not the former. If the two ever disagree,
        # the grouping glob (built on the key) stops matching the file (built
        # on the name), and that distribution is left ungrouped. This is now
        # load-bearing for publishing and was previously unasserted.
        assert _document(path)["project"]["name"] == name, (
            f"{path} declares project.name != {name!r}; the release workflow's "
            "grouping step assumes these agree"
        )


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


def _heredoc_script(run: str) -> str:
    """Pull the Python source out of a `uv run python - <<'PY' ... PY` step's
    `run:` block. Tests that want to check *behaviour* -- what the grouping
    step actually does to a `dist/` directory, not just what its YAML looks
    like -- execute this, rather than re-implementing the step's logic
    in the test. A second implementation is exactly the kind of thing that
    silently stops matching the first the day only one of them is edited."""
    match = re.search(r"<<'PY'\n(.*)\nPY\n?$", run, re.DOTALL)
    assert match, "run step is not a `uv run python - <<'PY' ... PY` heredoc"
    return match.group(1)


def _built_filenames(version: str = TARGET_VERSION) -> list[str]:
    """The sdist/wheel filenames `uv build --all-packages` actually produces
    for every published package, at `version` -- underscored, per PEP 503
    normalisation, regardless of the hyphenated `PACKAGES` key."""
    filenames = []
    for name in PACKAGES:
        underscored = name.replace("-", "_")
        filenames.append(f"{underscored}-{version}.tar.gz")
        filenames.append(f"{underscored}-{version}-py3-none-any.whl")
    return filenames


def _run_grouping_script(tmp_path: Path, filenames: list[str]) -> subprocess.CompletedProcess[str]:
    """Actually execute the release workflow's "Group distributions by
    package" step against a synthetic `dist/` populated with `filenames`
    (empty files -- the step never reads content, only names), in a
    directory carrying its own copy of `tools/version.py` so the script's own
    `sys.path.insert(0, "tools")` resolves exactly as it does in CI.
    """
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "version.py").write_text((ROOT / "tools/version.py").read_text())
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    for filename in filenames:
        (dist_dir / filename).write_bytes(b"")

    document = _release_workflow()
    group = _find_step(
        document["jobs"]["build"]["steps"],
        lambda s: s.get("name") == "Group distributions by package",
        "distribution-grouping step",
    )
    script = _heredoc_script(group["run"])
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_the_release_workflow_groups_every_built_distribution(tmp_path: Path):
    """The happy path, run for real: every one of the five packages' sdist
    and wheel lands in its own `dist/<name>/` (hyphenated, matching the
    `PACKAGES` key -- not `dist/<name_with_underscores>/`, which is what the
    built *filenames* use), and nothing is left at `dist/`'s top level.
    Executing the actual script, rather than asserting on the YAML around it,
    is what catches a grouping step whose target-directory naming quietly
    drifts from the `PACKAGES` key it is supposed to match -- keeping this
    step's name, its `PACKAGES` import, and every publish step's
    `packages-dir` all unchanged, but a directory the grouping step wrote to
    dist/ors_schema/ (underscored) while every publish step still reads
    dist/ors-schema/ (hyphenated) -- would fail loudly here instead of
    silently on a real tag.
    """
    result = _run_grouping_script(tmp_path, _built_filenames())
    assert result.returncode == 0, result.stderr

    dist_dir = tmp_path / "dist"
    for name in PACKAGES:
        underscored = name.replace("-", "_")
        grouped = sorted(p.name for p in (dist_dir / name).iterdir())
        expected = sorted(
            [
                f"{underscored}-{TARGET_VERSION}.tar.gz",
                f"{underscored}-{TARGET_VERSION}-py3-none-any.whl",
            ]
        )
        assert grouped == expected, f"{name}: grouped into {grouped}, expected {expected}"

    leftover = sorted(p.name for p in dist_dir.iterdir() if p.suffix in {".whl", ".gz"})
    assert leftover == [], f"left ungrouped at dist/'s top level: {leftover}"


def test_the_release_workflow_grouping_step_refuses_a_stray_distribution(tmp_path: Path):
    """A built distribution matching no `PACKAGES` key -- a sixth workspace
    member, or a `project.name` that drifted from its `PACKAGES` key -- must
    not be silently left at `dist/`'s top level. Left there, `upload-artifact`
    still uploads it and no `publish` step ever names it: a green workflow
    that quietly never published it.
    """
    filenames = [*_built_filenames(), "ors_widget-0.1.0-py3-none-any.whl"]
    result = _run_grouping_script(tmp_path, filenames)
    assert result.returncode != 0, "a stray distribution must fail the grouping step"
    assert "ors_widget" in result.stderr, result.stderr


def test_the_release_workflow_grouping_step_refuses_a_missing_distribution(tmp_path: Path):
    """A package whose files all fail to match -- a build that silently
    produced zero (or only one) of its sdist/wheel pair -- must not pass
    through to an empty (or half-empty) `dist/<name>/` and a `publish` step
    that runs, and reports success, over nothing.
    """
    filenames = [f for f in _built_filenames() if not f.startswith("ors_schema-")]
    result = _run_grouping_script(tmp_path, filenames)
    assert result.returncode != 0, "a package missing its distributions must fail the grouping step"
    assert "ors-schema" in result.stderr, result.stderr


PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
"""The action every publish step uses. Named once: a `startswith` against a
hand-retyped string in eight places is how a test quietly stops finding the
steps it is supposed to be checking."""

RELEASE_ENVIRONMENTS = {
    "ors-schema": "release-ors-schema",
    "ors-render": "release-ors-render",
    "ors-daemon": "release-ors-daemon",
    "ors-server": "release-ors-server",
    "openrackscreen": "release-openrackscreen",
}
"""The GitHub environment each package publishes from, spelled out rather than
derived from `PACKAGES`. These five strings exist twice outside this
repository -- as environments on the repository itself, and inside the trusted
publisher PyPI has registered for each project -- and neither copy moves when
this file is edited. A derived `f"release-{name}"` would follow a renamed
`PACKAGES` key straight past the one thing that must not change silently.
`test_every_publish_job_has_its_own_trusted_publishing_identity` still checks
this dict covers exactly `PACKAGES`, so a sixth package cannot arrive without
an environment."""


def _publish_jobs(document: dict) -> dict[str, dict]:
    """Every job that uploads to PyPI, keyed by job id.

    The five uploads used to be five steps of one `publish` job, and every
    test here reached them through `document["jobs"]["publish"]["steps"]`.
    They are five jobs now -- PyPI keys a pending trusted publisher on the
    environment name, so five packages need five environments and an
    environment is a property of a job, not of a step. Found by what they
    *do* rather than by what they are called, so that a sixth publish job
    named anything at all is still covered by every test below."""
    return {
        name: job
        for name, job in document["jobs"].items()
        if any(s.get("uses", "").startswith(PUBLISH_ACTION) for s in job.get("steps", []))
    }


def _publish_steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) if s.get("uses", "").startswith(PUBLISH_ACTION)]


def _package_published_by(job_name: str, job: dict) -> str:
    """The package a publish job uploads, read off its `packages-dir`.

    Asserts one upload per job rather than taking the first: a job that
    publishes two packages has one environment for both, which is the exact
    configuration PyPI refuses, and it would also put two packages at the
    same position in the dependency graph the tests below check."""
    steps = _publish_steps(job)
    assert len(steps) == 1, (
        f"job {job_name!r} publishes {len(steps)} packages; one job publishes one package, "
        "because a trusted publisher is keyed on the job's environment"
    )
    return steps[0]["with"]["packages-dir"].strip("/").rsplit("/", 1)[-1]


def _needs(job: dict) -> list[str]:
    """`needs:` normalised to a list. A bare string is legal YAML here, and
    `"build" in needs` against one is a substring match that also accepts a
    job named `buildx`."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _must_run_before(document: dict, job_name: str) -> set[str]:
    """Every job that has to succeed before `job_name` starts.

    Transitive, not just the direct `needs:`. Ordering used to be step order
    inside one job and could be read off a list index; it is a graph now, and
    `ors-render` needing `ors-schema` is what makes `ors-daemon` -- which
    needs only `ors-render` -- run after both."""
    jobs = document["jobs"]
    seen: set[str] = set()
    frontier = _needs(jobs[job_name])
    while frontier:
        name = frontier.pop()
        assert name in jobs, f"job {job_name!r} needs {name!r}, which is not a job"
        if name in seen:
            continue
        seen.add(name)
        frontier.extend(_needs(jobs[name]))
    return seen


def _triggers() -> dict:
    """The workflow's `on:` block.

    YAML 1.1's bareword booleans mean `on:` parses under `yaml.safe_load` to
    the boolean key `True`, not the string `"on"` -- a trap for anyone reading
    this trigger back out with `document["on"]`, which raises `KeyError` and
    checks nothing. A bare `document[True]` is no better: it swaps one
    unexplained `KeyError` for another, in a file whose own docstring argues
    against exactly that failure mode. Quoting the key as `"on":` in the
    workflow -- valid YAML, identical to what GitHub Actions parses, and what
    `yamllint`'s `truthy` rule tells people to do -- would trip either bare
    lookup; `.get` against both keys, plus an explaining assert, does not."""
    document = _release_workflow()
    trigger = document.get(True, document.get("on"))
    assert trigger is not None, (
        "release.yml has no `on:` trigger under either the YAML-1.1 boolean "
        "key or a literal string 'on' key"
    )
    assert isinstance(trigger, dict), f"release.yml's `on:` is {type(trigger).__name__}, not a map"
    return trigger


def test_the_release_workflow_publish_steps_read_the_directories_grouping_creates():
    """Nothing previously tied the publish steps' `packages-dir` to the
    directories the grouping step actually creates, or to the artifact that
    `upload-artifact` actually uploads: `packages-dir` could name a directory
    the grouping step never populates, or the artifact's `path:` could be
    narrowed to a single package's subdirectory, and every existing test
    stayed green, because none of them read `packages-dir` past its basename,
    or looked at `upload-artifact`'s `path`, or the grouping step's own
    existence, all at once. Both failures are loud -- but only against a real
    `v*` tag, which is the one place this project cannot afford to find out.

    Collected across every publishing job rather than out of one job's step
    list: the five uploads are five jobs now, and a test that kept reading
    `jobs["publish"]["steps"]` would have raised `KeyError` -- or, worse for a
    future split, quietly checked four of five.
    """
    document = _release_workflow()
    build_steps = document["jobs"]["build"]["steps"]
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"

    group = _find_step(
        build_steps,
        lambda s: s.get("name") == "Group distributions by package",
        "distribution-grouping step",
    )
    assert "PACKAGES" in group["run"], (
        "the grouping step must derive its directories from tools.version.PACKAGES, "
        "not a second, hand-copied list"
    )

    directories = {
        step["with"]["packages-dir"] for job in jobs.values() for step in _publish_steps(job)
    }
    assert directories == {f"dist/{name}/" for name in PACKAGES}, (
        "every publish step's packages-dir must name a directory the grouping step creates, "
        "and between them they must name all five"
    )

    upload = _find_step(
        build_steps,
        lambda s: s.get("uses", "").startswith("actions/upload-artifact"),
        "upload-artifact step",
    )
    assert upload["with"]["path"] == "dist/", (
        "the uploaded artifact must carry every package's grouped directory, not one alone"
    )


def test_the_release_workflow_publish_steps_pin_skip_existing():
    """`skip-existing: true` is what makes a rerun after a partial publish
    failure safe: a package already on PyPI is skipped rather than rejected
    as a duplicate. Losing it from even one of the five turns the resume this
    workflow depends on into a hard failure the first time it is exercised for
    real -- and this release is exercised for real by design, because PyPI
    allows only three pending trusted publishers at a time and five packages
    therefore go out in two rounds against the same tag.

    Swept over every publishing job, so dropping the setting from a single
    job cannot hide behind the other four.
    """
    document = _release_workflow()
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"

    for name, job in jobs.items():
        steps = _publish_steps(job)
        assert steps, f"job {name!r} has no publish step"
        assert all(step["with"].get("skip-existing") is True for step in steps), (
            f"job {name!r} must keep skip-existing pinned to true, or round two of a "
            "split release fails on a duplicate instead of skipping it"
        )


def test_every_publish_job_has_its_own_trusted_publishing_identity():
    """The constraint that split one publish job into five.

    PyPI keys a *pending* trusted publisher on the whole configuration --
    owner, repository, workflow filename and environment name -- and that
    tuple has to be unique, because the OIDC token a workflow presents is
    identical no matter which package it is uploading: with two pending
    publishers on one tuple, PyPI cannot tell which project a token is
    claiming. Registering a second package against a shared `environment:
    release` is refused outright ("A pending trusted publisher matching this
    configuration has already been registered for a different project name"),
    so one environment means exactly one of these five packages can ever be
    registered. Nothing in this repository said so, and the failure is silent
    here and loud only at PyPI, on a real tag.

    The names are asserted against `RELEASE_ENVIRONMENTS` literally, because
    three of them are already registered on PyPI against exactly these
    strings: renaming one breaks nothing else in this tree, it invalidates a
    publisher.

    `permissions: id-token: write` is checked in the same place because it is
    the other half of the same identity, and because it went from one job that
    needed it to five that each do: a publish job without it has no OIDC token
    to present and cannot upload at all.
    """
    document = _release_workflow()
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"
    assert set(RELEASE_ENVIRONMENTS) == set(PACKAGES), (
        "every published package needs its own release environment: "
        f"{set(PACKAGES) ^ set(RELEASE_ENVIRONMENTS)} is unaccounted for"
    )

    seen: dict[str, str] = {}
    for name, job in jobs.items():
        package = _package_published_by(name, job)
        environment = job.get("environment")
        assert environment == RELEASE_ENVIRONMENTS.get(package), (
            f"job {name!r} publishes {package} from environment {environment!r}, "
            f"expected {RELEASE_ENVIRONMENTS.get(package)!r}"
        )
        assert environment not in seen, (
            f"jobs {seen.get(environment)!r} and {name!r} share environment "
            f"{environment!r}; PyPI cannot register a pending publisher for both"
        )
        seen[environment] = name
        assert job.get("permissions", {}).get("id-token") == "write", (
            f"job {name!r} has no `permissions: id-token: write` and cannot present the "
            "OIDC token trusted publishing is built on"
        )

    assert set(seen) == set(RELEASE_ENVIRONMENTS.values()), (
        f"the publish jobs use {sorted(seen)}, expected exactly "
        f"{sorted(RELEASE_ENVIRONMENTS.values())}"
    )


def test_the_release_workflow_warns_before_a_silent_skip():
    """`skip-existing: true` is correct for its intended case -- a resume
    after a partial failure -- but it makes another case silent: delete and
    re-push a `v*` tag at a different commit to "fix" a released bug, and all
    five uploads are skipped as duplicates, the workflow reports success, and
    PyPI still serves the old artifacts. This asserts the step this repo
    added to make that run's log visibly different: it must run before any
    package is actually published, it must actually query PyPI rather than
    just print a static message, and it must not be able to fail the job --
    a publish that would otherwise succeed must never be blocked by a PyPI
    hiccup answering this check.

    The step lives in `build` now, not in a publish job. That is what lets the
    publish jobs drop `actions/checkout` and `astral-sh/setup-uv` entirely --
    it was the only thing in them that read the repository -- and it does not
    weaken "before any package is published": every publishing job has to wait
    on `build`, which is a stronger statement than a step index inside one
    job, since it holds for all five at once.
    """
    document = _release_workflow()
    warn_job = "build"
    warn = _find_step(
        document["jobs"][warn_job]["steps"],
        lambda s: s.get("name") == "Warn about packages already published at this version",
        "pre-publish PyPI check",
    )
    assert "continue-on-error" not in warn, (
        "the warning step must not need continue-on-error -- it must not raise at all"
    )
    assert "if" not in warn, "the warning step must not be conditionally skipped"
    assert "pypi.org" in warn["run"], "the warning step must actually query PyPI"
    # Deliberately narrow, and deliberately not a search for "raise": that
    # spelling matched the word inside a *comment* explaining which exception
    # the handler has to catch, so the assertion failed on prose. It was never
    # the real guarantee anyway -- a step can fail without the word appearing
    # at all, which is exactly how a bare `TimeoutError` used to get through
    # here. `test_a_slow_pypi_does_not_fail_the_release` runs the script and
    # is what actually holds this; these two are the cheap spelling check.
    assert "SystemExit" not in warn["run"], "the warning step must never fail the job"
    assert "sys.exit" not in warn["run"], "the warning step must never fail the job"

    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"
    for name in jobs:
        assert warn_job in _must_run_before(document, name), (
            f"job {name!r} can publish without {warn_job!r} -- and so without the warning -- "
            "having run first"
        )


def test_every_job_that_runs_uv_or_reads_tools_sets_itself_up_first():
    """A `run:` naming `uv` or `tools` needs a checkout and a `uv` on PATH.

    The old `publish` job had neither and could not run at all. Its only
    pre-step was `download-artifact`, so its warning step -- `uv run
    --no-project ... sys.path.insert(0, "tools"); from version import ...` --
    met a bare runner with no `tools/`, no manifests and no `uv`. It carries
    no `continue-on-error`, so the step's non-zero exit failed the job and the
    five `pypa/gh-action-pypi-publish` steps under it never ran: a tagged
    release that published nothing.

    That step now lives in `build`, where a checkout and a `uv` already exist
    for the build itself, and the five publish jobs read nothing from the
    repository at all. This is still deliberately a sweep and not an assertion
    about one job: it was written as a sweep because the next job to grow a
    `uv run` line is the next one to have this exact hole, and an edit that
    moves a script *between* jobs is precisely how one lands in a job with no
    `uv`. `checked` is asserted non-empty because a sweep that matches nothing
    is the one way this could pass while holding nothing -- and after this
    move, `build` is the only job it matches.

    `test_a_slow_pypi_does_not_fail_the_release` below could not see the
    original hole, and says so in its own docstring -- it lifts the script out
    of the workflow and executes it with cwd set to the repository, which is
    the one thing the runner did not give it.
    """
    document = _release_workflow()
    checked = []

    for name, job in document["jobs"].items():
        steps = job.get("steps", [])
        needs_setup = [
            step for step in steps if "uv" in step.get("run", "") or "tools" in step.get("run", "")
        ]
        if not needs_setup:
            continue
        checked.append(name)
        uses = [step.get("uses", "") for step in steps]
        assert any(u.startswith("actions/checkout") for u in uses), (
            f"job {name!r} runs a step naming uv or tools with no actions/checkout: "
            f"{[step.get('name') for step in needs_setup]}"
        )
        assert any(u.startswith("astral-sh/setup-uv") for u in uses), (
            f"job {name!r} runs a step naming uv or tools with no astral-sh/setup-uv: "
            f"{[step.get('name') for step in needs_setup]}"
        )
        # And in that order relative to the first step that needs them --
        # `actions/checkout` cleans the workspace, so a checkout landing after
        # a step that has already written files deletes them.
        first_need = min(steps.index(step) for step in needs_setup)
        for prefix in ("actions/checkout", "astral-sh/setup-uv"):
            at = min(i for i, u in enumerate(uses) if u.startswith(prefix))
            assert at < first_need, (
                f"job {name!r}: {prefix} must come before the step that needs it"
            )

    assert checked, (
        "no job in release.yml runs uv or reads tools/ -- this sweep is checking nothing, "
        "which is not the same as passing"
    )


def test_no_publish_job_can_lose_the_distributions_before_uploading_them():
    """`actions/checkout` runs with `clean: true` by default, which wipes the
    workspace. `download-artifact` writes the five packages into `dist/` in
    that same workspace, so checking out afterwards would delete every file
    the job was created to upload -- and `pypa/gh-action-pypi-publish` over an
    empty directory is a workflow that reports green having published nothing.

    The publish jobs carry no checkout at all now: the one step that needed
    one moved into `build`. That makes the original assertion -- checkout
    before download -- vacuous rather than false, so it is kept as a
    conditional (a checkout added back here later must still come first) and
    joined by the two halves of the same property that are *not* vacuous:
    every publish job must actually download the artifact, exactly once, and
    must do it before it uploads. A publish step ahead of the download
    uploads an empty directory, which is the identical green-and-published-
    nothing outcome by the other route.
    """
    document = _release_workflow()
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"

    for name, job in jobs.items():
        uses = [step.get("uses", "") for step in job["steps"]]
        downloads = [i for i, u in enumerate(uses) if u.startswith("actions/download-artifact")]
        assert len(downloads) == 1, (
            f"job {name!r} must download the distributions exactly once, found {len(downloads)}"
        )
        for at in (i for i, u in enumerate(uses) if u.startswith("actions/checkout")):
            assert at < downloads[0], (
                f"job {name!r}: actions/checkout after download-artifact deletes the dist/ "
                "this job exists to upload"
            )
        uploads = [i for i, u in enumerate(uses) if u.startswith(PUBLISH_ACTION)]
        assert min(uploads) > downloads[0], (
            f"job {name!r} publishes before it downloads the distributions, which uploads "
            "an empty directory and reports success"
        )


def test_a_slow_pypi_does_not_fail_the_release(tmp_path: Path):
    """The warning step runs the real script against a PyPI that never answers.

    `"raise" not in run` above is a *textual* check, and it passed while the
    step could still crash: the handler caught `urllib.error.URLError`, and a
    server that accepts the connection and is then slow to answer raises a
    bare `TimeoutError` from the read, which urllib does not wrap. A connect
    refusal and a connect timeout do arrive as `URLError`; a read timeout does
    not. So the single most likely thing to go wrong -- PyPI being slow or
    rate-limiting a release -- failed the publish on a step whose entire
    purpose is to print a warning.

    Executed rather than asserted about, because that is the only way to see
    it. The script is pulled out of the live workflow -- out of `build` now,
    which is where it moved to, not out of a publish job -- `urlopen` is
    replaced before it runs, and the working directory is the repository so
    the real `tools/version.py` and the real manifests are what it reads.
    Nothing is written anywhere.
    """
    warn = _find_step(
        _release_workflow()["jobs"]["build"]["steps"],
        lambda s: s.get("name") == "Warn about packages already published at this version",
        "pre-publish PyPI check",
    )
    script = tmp_path / "warn.py"
    script.write_text(_heredoc_script(warn["run"]))

    finished = subprocess.run(
        [
            sys.executable,
            "-c",
            "import urllib.request, sys\n"
            "def _never_answers(*args, **kwargs):\n"
            "    raise TimeoutError('The read operation timed out')\n"
            "urllib.request.urlopen = _never_answers\n"
            f"exec(compile(open({str(script)!r}).read(), 'warn-step', 'exec'))\n",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 0, f"a slow PyPI failed the release step:\n{finished.stderr}"
    assert "could not check PyPI" in finished.stdout


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

    # And the other half of "before any upload", which is now a job boundary
    # rather than a step index: the gate is in `build`, the uploads are in
    # five separate jobs, and each of them has to be waiting on `build`.
    # `"build" in needs` alone would not do it -- `ors-daemon` needs only
    # `ors-render` -- so this walks the graph.
    _assert_every_publish_job_waits_for_the_gates(document)


def test_the_release_workflow_refuses_a_wheel_with_a_local_path_dependency():
    """Spec §9: `[tool.uv.sources]` is uv-only and should never reach a
    published `Requires-Dist`, but "should" is how a `file://` requirement
    gets published and fails to install for everyone outside this workspace.
    Mirrors the interface gate's test above: asserts the check inspects
    wheel metadata, that nothing can silence it, and that it runs before
    anything is uploaded -- not merely that a step with a plausible name
    exists somewhere in the file.
    """
    document = _release_workflow()
    steps = document["jobs"]["build"]["steps"]
    check = _find_step(steps, lambda s: "local path" in s.get("name", ""), "local-path gate")
    upload = _find_step(
        steps,
        lambda s: s.get("uses", "").startswith("actions/upload-artifact"),
        "upload-artifact step",
    )

    assert "continue-on-error" not in check, (
        "the local-path gate must not be allowed to fail silently"
    )
    assert "if" not in check, "the local-path gate must not be conditionally skipped"
    assert "Requires-Dist" in check["run"], (
        "the local-path gate must actually inspect wheel metadata for Requires-Dist"
    )
    assert steps.index(check) < steps.index(upload), (
        "the local-path check must run before the artifact is uploaded"
    )
    _assert_every_publish_job_waits_for_the_gates(document)


def _assert_every_publish_job_waits_for_the_gates(document: dict) -> None:
    """Both pre-upload gates live in `build`. Before the split there was one
    `publish` job and `"build" in publish["needs"]` said everything; now there
    are five, chained to each other, and only `publish-ors-schema` names
    `build` directly. Reachability is the assertion -- a publish job that
    dropped out of the chain would still have a `needs:` and would still look
    fine to a direct-membership check."""
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"
    for name in jobs:
        assert "build" in _must_run_before(document, name), (
            f"job {name!r} can publish without build -- and so without the pre-upload "
            "gates -- having succeeded"
        )


def test_the_release_workflow_uploads_and_downloads_the_same_artifact_name():
    """A rename on either side of the `build` / publish boundary makes
    `download-artifact` fetch nothing. The publish job then runs against an
    empty `dist/`, and `pypa/gh-action-pypi-publish` over zero files is a
    workflow that reports green having published nothing.

    Checked for every publishing job: five jobs each download the artifact
    now, so a rename that was one edit is five, and getting four of them right
    is the version of this bug that publishes most of a release.
    """
    document = _release_workflow()
    upload = _find_step(
        document["jobs"]["build"]["steps"],
        lambda s: s.get("uses", "").startswith("actions/upload-artifact"),
        "upload-artifact step",
    )
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"

    for name, job in jobs.items():
        download = _find_step(
            job["steps"],
            lambda s: s.get("uses", "").startswith("actions/download-artifact"),
            f"download-artifact step in {name}",
        )
        assert upload["with"]["name"] == download["with"]["name"], (
            f"job {name!r} downloads {download['with']['name']!r}, but build uploads "
            f"{upload['with']['name']!r}"
        )


def test_the_release_workflow_only_triggers_on_version_tags():
    """Firing on a branch push instead of a tag push would let CI attempt to
    publish on every commit merged to a branch, and publishing is irreversible
    per version. `push:` must carry `tags:` and nothing else -- a `branches:`
    key added alongside is the whole failure, and it is invisible to a check
    that only reads `tags`.

    `_triggers()` explains why the `on:` block is not read as `document["on"]`.
    """
    trigger = _triggers()
    assert trigger.get("push", {}).get("tags") == ["v*"], (
        "release must trigger only on pushes of version tags (on: push: tags: ['v*'])"
    )
    assert set(trigger["push"]) == {"tags"}, (
        f"release's push trigger also carries {sorted(set(trigger['push']) - {'tags'})}; "
        "publishing must not fire on a branch push"
    )


def test_the_release_workflow_can_be_re_run_by_hand_against_the_same_tag():
    """PyPI caps an account at three *pending* trusted publishers at a time,
    and this project publishes five packages, so its first release cannot go
    out in one run: three publish, two fail for want of a publisher, those two
    get registered, and the same tag has to run again.

    Without `workflow_dispatch` the only way to run a tag again is to delete
    and re-push it -- which moves the tag off the commit that was actually
    built, and is exactly the silent-skip case
    `test_the_release_workflow_warns_before_a_silent_skip` exists for.
    Dispatching selects a ref, and a tag is a ref, so `GITHUB_REF_NAME` is the
    tag's short name and the tag/version gate reads the same string a push
    gives it; `skip-existing: true` makes round two a no-op for everything
    round one already published.
    """
    trigger = _triggers()
    assert "workflow_dispatch" in trigger, (
        "release must be runnable by hand against a tag, or round two of a split first "
        "release has to delete and re-push the tag to happen at all"
    )


def test_the_release_workflow_checks_the_tag_matches_the_version_before_publishing():
    """`tools/version.read_versions` is the one place this repository already
    knows what version every package believes it is. Publishing a tag that
    disagrees with the tree ships a version number the tag on GitHub and the
    version on PyPI will forever disagree about.

    It reads `GITHUB_REF_NAME`, which is the short name of whatever ref
    started the run -- the tag on a tag push, and the tag again on a
    `workflow_dispatch` aimed at one, so round two of a split release is
    gated identically. A dispatch aimed at a branch arrives here with a branch
    name and fails, which is the correct answer.
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
    assert "GITHUB_REF_NAME" in check["run"], (
        "the tag/version gate must read the ref the run was started against"
    )


def test_the_release_meta_package_publishes_last():
    """`openrackscreen` pins `ors-daemon==` and `ors-server==`; if it published
    before either, a failure partway through would leave a meta-package on
    PyPI pinning a dependency that never made it there -- a version that can
    never resolve and, once published, can never be re-uploaded. Every package
    with dependents must publish after all of them.

    Asserted on the *job* graph, not on step order. The five uploads used to
    be five sequential steps of one job, where order was a list index; they
    are five jobs now, one per trusted-publishing environment, and their order
    is `needs:`. Reachability rather than direct membership, because
    `publish-ors-daemon` names only `publish-ors-render` and still runs after
    `publish-ors-schema` through it. The final loop is the other direction of
    "last": nothing may be waiting on the meta-package, which is what a
    re-chained graph that merely kept it at the bottom of the file would
    otherwise pass.
    """
    document = _release_workflow()
    jobs = _publish_jobs(document)
    assert jobs, "the release workflow must actually publish something"

    by_package = {_package_published_by(name, job): name for name, job in jobs.items()}
    assert set(by_package) == set(PACKAGES), (
        f"the publish jobs cover {sorted(by_package)}, expected {sorted(PACKAGES)}"
    )

    def publishes_after(dependent: str, dependency: str) -> bool:
        return by_package[dependency] in _must_run_before(document, by_package[dependent])

    assert publishes_after("ors-render", "ors-schema"), (
        "ors-render depends on ors-schema and must publish after it"
    )
    for dependent in ("ors-daemon", "ors-server"):
        assert publishes_after(dependent, "ors-render"), (
            f"{dependent} depends on ors-render and must publish after it"
        )
        assert publishes_after("openrackscreen", dependent), (
            f"openrackscreen depends on {dependent} and must publish after it"
        )

    meta = by_package["openrackscreen"]
    for package, name in by_package.items():
        if package == "openrackscreen":
            continue
        assert meta not in _must_run_before(document, name), (
            f"{package} waits on the meta-package; openrackscreen must publish last"
        )
