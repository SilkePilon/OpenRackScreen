"""Tests for the ``assert_golden`` fixture itself.

This task ships no golden images, but every later visual test leans on this
harness, so its failure modes are exercised directly here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


def _redirect_golden_dir(assert_golden, monkeypatch, tmp_path) -> Path:
    """Point the fixture at a throwaway golden directory and clear UPDATE_GOLDEN."""
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)
    directory = tmp_path / "golden"
    monkeypatch.setitem(assert_golden.__globals__, "GOLDEN_DIR", directory)
    return directory


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (8, 8)) -> Image.Image:
    return Image.new("RGB", size, color)


def test_missing_golden_fails_with_a_regeneration_hint(assert_golden, monkeypatch, tmp_path):
    _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    with pytest.raises(AssertionError, match="regenerate with UPDATE_GOLDEN=1"):
        assert_golden(_solid((0, 0, 0)), "nope")


def test_identical_image_passes(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    _solid((12, 34, 56)).save(directory / "same.png")
    assert_golden(_solid((12, 34, 56)), "same")


def test_differing_pixels_fail(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    _solid((0, 0, 0)).save(directory / "flip.png")
    with pytest.raises(AssertionError, match="differs by mean 255.00"):
        assert_golden(_solid((255, 255, 255)), "flip")


def test_difference_within_tolerance_passes(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    _solid((10, 10, 10)).save(directory / "nudge.png")
    assert_golden(_solid((11, 11, 11)), "nudge")


def test_size_mismatch_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    _solid((0, 0, 0), (8, 8)).save(directory / "resize.png")
    with pytest.raises(AssertionError, match=r"size \(16, 16\) != golden \(8, 8\)"):
        assert_golden(_solid((0, 0, 0), (16, 16)), "resize")


def test_update_golden_rewrites_the_reference(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    _solid((0, 0, 0)).save(directory / "rewrite.png")
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(_solid((255, 0, 0)), "rewrite")
    assert Image.open(directory / "rewrite.png").convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_a_regression_still_fails_once_update_golden_is_unset(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    directory.mkdir()
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(_solid((0, 0, 0)), "regress")
    monkeypatch.delenv("UPDATE_GOLDEN")
    with pytest.raises(AssertionError, match="differs by mean"):
        assert_golden(_solid((255, 255, 255)), "regress")
