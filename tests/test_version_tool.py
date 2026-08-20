"""`tools/version.py` itself, exercised against a throwaway package tree.

Nothing above this file ever ran the rewrite script under test -- every
existing check only reads the *result* of a hand-run `uv run python
tools/version.py 0.2.0` sitting in the working tree. That is why the `name =
"..."` corruption bug (documented in `task-1-report.md`: the pin pass matched
a package's own `name` line because every published name is also a key in
`PACKAGES`) shipped once already, undetected, and would ship again the moment
the `_NAME` guard in `rewrite()` is removed or narrowed.

The fixture tree lives entirely under `tmp_path` and `PACKAGES` /
`VERSION_MODULES` are monkeypatched to point at it before `rewrite()` runs.
Nothing here may touch this repository's own `pyproject.toml` files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import version as version_tool

PKG_A_PYPROJECT = """\
[project]
name = "pkg-a"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

PKG_B_PYPROJECT = """\
[project]
name = "pkg-b"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pkg-a"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

MODULE_TEMPLATE = '__version__ = "0.1.0"\n'


@pytest.fixture
def package_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A two-package fixture tree (`pkg-b` depends on `pkg-a`, mirroring the
    real `ors-render` -> `ors-schema` edge) with a `__version__` module for
    each, wired into `tools.version.PACKAGES` / `VERSION_MODULES` in place of
    the real ones so `rewrite()` cannot reach this repository's own files.
    """
    pkg_a_dir = tmp_path / "pkg-a"
    pkg_b_dir = tmp_path / "pkg-b"
    pkg_a_dir.mkdir()
    pkg_b_dir.mkdir()

    pkg_a_pyproject = pkg_a_dir / "pyproject.toml"
    pkg_b_pyproject = pkg_b_dir / "pyproject.toml"
    pkg_a_pyproject.write_text(PKG_A_PYPROJECT)
    pkg_b_pyproject.write_text(PKG_B_PYPROJECT)

    pkg_a_module_dir = pkg_a_dir / "src" / "pkg_a"
    pkg_b_module_dir = pkg_b_dir / "src" / "pkg_b"
    pkg_a_module_dir.mkdir(parents=True)
    pkg_b_module_dir.mkdir(parents=True)

    pkg_a_module = pkg_a_module_dir / "__init__.py"
    pkg_b_module = pkg_b_module_dir / "__init__.py"
    pkg_a_module.write_text(MODULE_TEMPLATE)
    pkg_b_module.write_text(MODULE_TEMPLATE)

    packages = {"pkg-a": pkg_a_pyproject, "pkg-b": pkg_b_pyproject}
    version_modules = {"pkg-a": pkg_a_module, "pkg-b": pkg_b_module}
    monkeypatch.setattr(version_tool, "PACKAGES", packages)
    monkeypatch.setattr(version_tool, "VERSION_MODULES", version_modules)

    return {
        "pkg-a-pyproject": pkg_a_pyproject,
        "pkg-b-pyproject": pkg_b_pyproject,
        "pkg-a-module": pkg_a_module,
        "pkg-b-module": pkg_b_module,
    }


def test_rewrite_sets_the_version_and_the_intra_project_pin(package_tree: dict[str, Path]):
    changed = version_tool.rewrite("9.9.9")

    assert set(changed) == set(package_tree.values())

    a_text = package_tree["pkg-a-pyproject"].read_text()
    b_text = package_tree["pkg-b-pyproject"].read_text()
    assert 'version = "9.9.9"' in a_text
    assert 'version = "9.9.9"' in b_text
    assert '"pkg-a==9.9.9"' in b_text


def test_rewrite_updates_the_dunder_version_in_each_module(package_tree: dict[str, Path]):
    version_tool.rewrite("9.9.9")

    assert package_tree["pkg-a-module"].read_text() == '__version__ = "9.9.9"\n'
    assert package_tree["pkg-b-module"].read_text() == '__version__ = "9.9.9"\n'


def test_rewrite_leaves_the_name_line_verbatim(package_tree: dict[str, Path]):
    # The regression this guards: `_pin("pkg-a")` matches any quoted bare
    # occurrence of "pkg-a", and `name = "pkg-a"` is exactly such an
    # occurrence, because every published name is also a key in `PACKAGES`.
    # Without the `_NAME` guard in `rewrite()`, this becomes
    # `name = "pkg-a==9.9.9"` -- an invalid project name, corrupted silently.
    version_tool.rewrite("9.9.9")

    a_text = package_tree["pkg-a-pyproject"].read_text()
    b_text = package_tree["pkg-b-pyproject"].read_text()
    assert 'name = "pkg-a"' in a_text
    assert 'name = "pkg-b"' in b_text
    assert 'name = "pkg-a==9.9.9"' not in a_text


def test_rewrite_is_idempotent(package_tree: dict[str, Path]):
    first = version_tool.rewrite("9.9.9")
    assert first

    second = version_tool.rewrite("9.9.9")
    assert second == []


def test_read_versions_reflects_the_fixture_tree(package_tree: dict[str, Path]):
    version_tool.rewrite("9.9.9")

    assert version_tool.read_versions() == {"pkg-a": "9.9.9", "pkg-b": "9.9.9"}


@pytest.mark.parametrize("bad_version", ["1.0 beta", "not-a-version", ""])
def test_main_rejects_a_version_that_is_not_pep_440(
    package_tree: dict[str, Path], bad_version: str
):
    exit_code = version_tool.main([bad_version])

    assert exit_code != 0
    # Nothing was written: an invalid version rejected up front, not one that
    # made it into a file and was reported as a success.
    assert package_tree["pkg-a-pyproject"].read_text() == PKG_A_PYPROJECT
