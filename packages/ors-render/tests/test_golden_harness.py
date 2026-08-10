"""Tests for the ``assert_golden`` fixture itself.

This task ships no golden images, but every later visual test leans on this
harness, so its failure modes are exercised directly here.

The comparison is deliberately probed in the *middle* regime — a small element
moved or removed on a panel-sized canvas — because that is what real renderer
regressions look like, and a metric that only distinguishes "identical" from
"black vs white" would sail through such a change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

PANEL = (240, 240)
BG = (13, 13, 26)
ACCENT = (0, 229, 255)


def _redirect_golden_dir(assert_golden, monkeypatch, tmp_path) -> Path:
    """Point the fixture at a throwaway golden root and clear UPDATE_GOLDEN.

    Returns the *module-namespaced* directory the fixture will actually read,
    i.e. ``<root>/test_golden_harness``.
    """
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)
    root = tmp_path / "golden"
    monkeypatch.setitem(assert_golden.__globals__, "GOLDEN_DIR", root)
    return root / Path(__file__).stem


def _limits(assert_golden) -> tuple[int, float]:
    """The two module-level thresholds the fixture defaults to."""
    globals_ = assert_golden.__globals__
    return globals_["MAX_CHANNEL_DELTA"], globals_["MAX_DIFFERING_FRACTION"]


def _made(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (8, 8)) -> Image.Image:
    return Image.new("RGB", size, color)


def _panel() -> Image.Image:
    return Image.new("RGB", PANEL, BG)


def _panel_with_block(at: tuple[int, int] = (100, 100), size: int = 20) -> Image.Image:
    image = _panel()
    ImageDraw.Draw(image).rectangle(
        [at, (at[0] + size - 1, at[1] + size - 1)],
        fill=ACCENT,
    )
    return image


def _nudged(image: Image.Image, count: int, delta: int) -> Image.Image:
    """Copy of ``image`` with ``count`` pixels brightened by ``delta`` in one channel."""
    out = image.copy()
    width = out.size[0]
    for i in range(count):
        x, y = i % width, i // width
        r, g, b = out.getpixel((x, y))
        out.putpixel((x, y), (min(255, r + delta), g, b))
    return out


# --- golden bookkeeping ------------------------------------------------------


def test_missing_golden_fails_with_a_regeneration_hint(assert_golden, monkeypatch, tmp_path):
    _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    with pytest.raises(AssertionError, match="regenerate with UPDATE_GOLDEN=1"):
        assert_golden(_solid((0, 0, 0)), "nope")


def test_goldens_are_namespaced_by_test_module(assert_golden, monkeypatch, tmp_path):
    _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    with pytest.raises(AssertionError, match=r"golden/test_golden_harness/nope\.png"):
        assert_golden(_solid((0, 0, 0)), "nope")


def test_comparison_does_not_create_the_golden_directory(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    with pytest.raises(AssertionError):
        assert_golden(_solid((0, 0, 0)), "nope")
    assert not directory.exists()
    assert not (tmp_path / "golden").exists()


@pytest.mark.parametrize("value", ["", "0", "false", "False", " 0 "])
def test_falsey_update_golden_values_do_not_rewrite(assert_golden, monkeypatch, tmp_path, value):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    monkeypatch.setenv("UPDATE_GOLDEN", value)
    with pytest.raises(AssertionError, match="regenerate with UPDATE_GOLDEN=1"):
        assert_golden(_solid((0, 0, 0)), "nope")
    assert not directory.exists()


def test_update_golden_rewrites_the_reference(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((0, 0, 0)).save(_made(directory) / "rewrite.png")
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(_solid((255, 0, 0)), "rewrite")
    assert Image.open(directory / "rewrite.png").convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_update_golden_creates_the_namespaced_directory(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(_solid((1, 2, 3)), "fresh")
    assert (directory / "fresh.png").is_file()


def test_a_regression_still_fails_once_update_golden_is_unset(assert_golden, monkeypatch, tmp_path):
    _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(_solid((0, 0, 0)), "regress")
    monkeypatch.delenv("UPDATE_GOLDEN")
    with pytest.raises(AssertionError, match="max channel delta 255"):
        assert_golden(_solid((255, 255, 255)), "regress")


# --- shape and mode ----------------------------------------------------------


def test_identical_image_passes(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((12, 34, 56)).save(_made(directory) / "same.png")
    assert_golden(_solid((12, 34, 56)), "same")


def test_size_mismatch_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((0, 0, 0), (8, 8)).save(_made(directory) / "resize.png")
    with pytest.raises(AssertionError, match=r"size \(16, 16\) != golden \(8, 8\)"):
        assert_golden(_solid((0, 0, 0), (16, 16)), "resize")


def test_mode_mismatch_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((255, 0, 0)).save(_made(directory) / "mode.png")
    with pytest.raises(AssertionError, match="mode RGBA != golden RGB"):
        assert_golden(Image.new("RGBA", (8, 8), (255, 0, 0, 255)), "mode")


def test_alpha_only_difference_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(_made(directory) / "alpha.png")
    with pytest.raises(AssertionError, match="max channel delta 255"):
        assert_golden(Image.new("RGBA", (8, 8), (255, 0, 0, 0)), "alpha")


# --- realistic regressions ---------------------------------------------------


def test_whole_image_flip_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((0, 0, 0)).save(_made(directory) / "flip.png")
    with pytest.raises(AssertionError, match="max channel delta 255"):
        assert_golden(_solid((255, 255, 255)), "flip")


def test_element_displaced_by_three_pixels_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _panel_with_block((100, 100)).save(_made(directory) / "shift.png")
    with pytest.raises(AssertionError, match="differs: max channel delta"):
        assert_golden(_panel_with_block((103, 100)), "shift")


def test_small_element_removed_fails(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    reference = _panel()
    ImageDraw.Draw(reference).rectangle([(30, 118), (45, 119)], fill=ACCENT)
    reference.save(_made(directory) / "tick.png")
    with pytest.raises(AssertionError, match="differs: max channel delta"):
        assert_golden(_panel(), "tick")


def test_global_one_level_shift_fails(assert_golden, monkeypatch, tmp_path):
    # A uniform +1 is not antialiasing drift: it is every pixel changing, which
    # is exactly what the differing-pixel budget exists to catch.
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((10, 10, 10)).save(_made(directory) / "nudge.png")
    with pytest.raises(AssertionError, match="64 of 64 pixels differ"):
        assert_golden(_solid((11, 11, 11)), "nudge")


def test_edge_antialiasing_drift_passes(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    reference = _panel_with_block()
    reference.save(_made(directory) / "aa.png")
    assert_golden(_nudged(reference, count=40, delta=3), "aa")


# --- threshold boundaries ----------------------------------------------------


def test_max_channel_delta_boundary(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    limit, _ = _limits(assert_golden)
    reference = _panel()
    reference.save(_made(directory) / "delta.png")

    assert_golden(_nudged(reference, count=1, delta=limit), "delta")

    with pytest.raises(AssertionError, match=f"max channel delta {limit + 1}"):
        assert_golden(_nudged(reference, count=1, delta=limit + 1), "delta")


def test_differing_pixel_fraction_boundary(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _, fraction = _limits(assert_golden)
    total = PANEL[0] * PANEL[1]
    allowed = int(fraction * total)
    reference = _panel()
    reference.save(_made(directory) / "count.png")

    assert_golden(_nudged(reference, count=allowed, delta=1), "count")

    with pytest.raises(AssertionError, match=f"{allowed + 1} of {total} pixels differ"):
        assert_golden(_nudged(reference, count=allowed + 1, delta=1), "count")


def test_per_call_override_can_loosen_both_bounds(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    reference = _panel()
    reference.save(_made(directory) / "loose.png")
    candidate = _nudged(reference, count=500, delta=20)

    with pytest.raises(AssertionError):
        assert_golden(candidate, "loose")

    assert_golden(candidate, "loose", max_channel_delta=20, max_differing_fraction=0.01)


def test_failure_message_reports_delta_count_and_fraction(assert_golden, monkeypatch, tmp_path):
    directory = _redirect_golden_dir(assert_golden, monkeypatch, tmp_path)
    _solid((0, 0, 0)).save(_made(directory) / "diag.png")
    with pytest.raises(AssertionError) as excinfo:
        assert_golden(_solid((255, 255, 255)), "diag")
    message = str(excinfo.value)
    assert "max channel delta 255" in message
    assert "64 of 64 pixels differ" in message
    assert "100.0000%" in message
