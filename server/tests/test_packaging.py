"""Whether the built interface actually ends up inside the wheel.

`server/pyproject.toml`'s `[tool.hatch.build.targets.wheel]` table is the one
piece of this server that nothing else in the suite exercises: `create_app`
only ever sees `web_dir` as a directory that already holds files or does not,
and no test builds a wheel. `uv tool install ors-server` is the first thing in
the whole pipeline that reads this table, and by the time it does, the
interface is already missing or already there -- which is exactly the failure
this milestone exists to close (see `.superpowers/sdd/task-2-report.md`).

**Why hatchling's file-selection API, not a real `uv build`.** The root
`pyproject.toml`'s `[tool.pytest.ini_options]` sets a ten-second per-test
budget and says why it is not to be raised. A real `uv build` resolves the
PEP 517 backend into an isolated environment and then copies and zips every
file the distribution ships -- measured here at several seconds even with a
warm cache, an order of magnitude over budget for a question that is really
just "which paths does this config select." `WheelBuilder(...).config` answers
that directly: `include_path(relative_path)` runs the exact
`pathspec.GitIgnoreSpec` matches (`.gitignore`, `exclude`, `artifacts`) that a
real build applies to each file while walking the tree, purely against the
config already on disk. No directory is walked, no file has to exist, and
nothing is written anywhere -- tmp_path included, which is why this file
creates none.

**What actually governs this today, established empirically.** Four
configurations were built (`uv build --package ors-server`) and inspected;
the full matrix, commands and output are recorded under "Fix round 1" in
`.superpowers/sdd/task-2-report.md`. Summarised:

- Today's real files -- root `.gitignore`'s `server/src/ors_server/web/`,
  `packages = ["src/ors_server"]`, `artifacts` present or absent -- ship the
  directory either way. Hatchling locates the *nearest* `.gitignore` by
  walking up from the wheel target's root (`server/`, for this distribution,
  since `server/` has none of its own) and lands on the repository's root
  `.gitignore`. That file's pattern is written repo-relative
  (`server/src/ors_server/web/`), but hatchling matches every candidate path
  relative to `server/` (`src/ors_server/web/index.html`), which never starts
  with `server/`. The pattern is loaded and reachable -- it is simply never
  the *right shape* to match this path -- so the exclusion the old comment on
  `artifacts` credited has never once fired here. The directory ships purely
  because `packages = ["src/ors_server"]` already includes everything beneath
  it, `artifacts` or no `artifacts`.
- The same config, but with a `.gitignore` placed *inside* `server/` carrying
  the package-relative pattern (`src/ors_server/web/`) instead: with
  `artifacts` absent, the directory is excluded -- proof that the anchoring
  mismatch above, and not some other mechanism, is what makes today's
  exclusion inert. With `artifacts` restored, the directory ships regardless:
  `hatchling.builders.config.BuilderConfig.include_path` checks
  `path_is_artifact` before `path_is_excluded` and short-circuits on it, so a
  path matching `artifacts` ships no matter what any `exclude` pattern --
  vcs-derived or explicit -- says about it.

So `artifacts` is redundant against the `.gitignore` pattern exactly as it is
written today, and would become load-bearing the moment that pattern (or any
future `exclude` entry naming this path) were written to actually match. It
stays in `server/pyproject.toml` rather than being deleted: the alternative is
a wheel that stops shipping a page the day somebody "fixes" the anchoring bug
this docstring just explained, with nothing here or anywhere else to notice.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.wheel import WheelBuilder

SERVER_ROOT = Path(__file__).resolve().parents[1]
"""The wheel target's own root -- `server/`, not the repository root. This is
the directory hatchling's file-selection walks and the root every pattern
below is matched relative to; building `WheelBuilder` against anything else
would answer a question about a different distribution."""

WEB_INDEX = "src/ors_server/web/index.html"
"""One file out of the built interface, package-root-relative -- the
coordinate system `include_path` and `exclude_spec.match_file` use once
`WheelBuilder` is rooted at `SERVER_ROOT`. Standing in for the whole
directory: `artifacts = ["src/ors_server/web/**"]` and `packages =
["src/ors_server"]` both select by directory, so a single representative file
under it either is or is not shipped for exactly the same reason as every
other file there."""


def _wheel_config():
    return WheelBuilder(str(SERVER_ROOT)).config


def test_the_built_interface_would_be_selected_into_the_wheel() -> None:
    """The one assertion this file exists to make.

    `include_path` is the same predicate a real build's file walk calls, once
    per candidate file, to decide what goes in the zip -- so a
    `server/pyproject.toml` edit that stops selecting `web/index.html` fails
    this exactly as it would fail a real `uv build`, just without paying for
    one. Proved by mutation, not by inspection: with `artifacts` removed from
    `server/pyproject.toml` and `exclude = ["src/ors_server/web/**"]` added in
    its place -- the realistic future edit the module docstring's empirical
    section warns about, someone "helpfully" excluding what they believe
    `.gitignore` already excludes -- this assertion fails. Reverted before
    committing; the exact commands and output are recorded in
    `.superpowers/sdd/task-2-report.md` under "Fix round 1".
    """
    assert _wheel_config().include_path(WEB_INDEX, is_package=False)


def test_the_root_gitignores_pattern_never_matches_this_path() -> None:
    """Why the assertion above passes for a reason other than `artifacts`.

    `exclude_spec` is the matcher `include_path` derives from `.gitignore`
    plus any explicit `exclude` entries; this pins that it is loaded and
    reachable (there is a spec at all) and still does not match `WEB_INDEX` --
    the anchoring mismatch the module docstring describes. If this test ever
    starts failing on its own, with nothing else changed, that means the root
    `.gitignore`'s pattern (or hatchling's resolution of it) was fixed to
    match package-relative paths -- `artifacts` has quietly become
    load-bearing where it was previously redundant, and both this file's
    docstring and the comment on `pyproject.toml`'s `artifacts` line need
    rereading, not just this test.
    """
    exclude_spec = _wheel_config().exclude_spec
    assert exclude_spec is not None
    assert not exclude_spec.match_file(WEB_INDEX)


def test_artifacts_pattern_covers_the_built_interface() -> None:
    """The belt-and-braces protection is real, not just present in the file.

    `artifact_spec` is what `artifacts = ["src/ors_server/web/**"]` compiles
    to; this asserts the pattern actually matches the path it is meant to
    protect, so a typo in that glob (a trailing slash where a wildcard was
    meant, say) would be caught here even on days `exclude_spec` agrees with
    it for free.
    """
    artifact_spec = _wheel_config().artifact_spec
    assert artifact_spec is not None
    assert artifact_spec.match_file(WEB_INDEX)
