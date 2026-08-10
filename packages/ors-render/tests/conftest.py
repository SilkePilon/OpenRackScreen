"""Golden-image comparison fixture shared by every visual test in ``ors-render``.

Rendering is deterministic: PNG is lossless, the bundled DejaVu 2.37 faces are
pinned by SHA-256 (see ``test_fonts.py``) and nothing here reads a clock or the
network. So a golden and a fresh render should be byte-identical, and the only
slack worth granting is cross-version Pillow/freetype antialiasing drift — a
*small per-pixel* delta along a handful of glyph and arc edges.

The comparison therefore uses two bounds, both of which must hold:

1. ``MAX_CHANNEL_DELTA``  — how wrong any single pixel may be.
2. ``MAX_DIFFERING_FRACTION`` — how many pixels may differ at all.

A mean over the whole panel is deliberately *not* used: a rendered element
covers a tiny fraction of a 240x240 canvas, so deleting a whole text label
barely moves the mean while being exactly the regression these tests exist to
catch.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageChops

GOLDEN_DIR = Path(__file__).parent / "golden"

MAX_CHANNEL_DELTA = 8
"""Largest per-channel difference (0-255) any single pixel may show.

8/255 is ~3% of the range: enough to absorb a rasteriser rounding an edge
pixel's coverage slightly differently, far too little to hide an element that
moved, vanished or changed colour (those flip pixels by 100+ levels against any
usable background contrast).
"""

MAX_DIFFERING_FRACTION = 0.001
"""Largest fraction of pixels that may differ at all, at any magnitude.

0.1% is 57 pixels of a 240x240 panel — room for antialiasing drift scattered
along a few edges, but not for a re-rendered element or a whole-canvas shade
change. The smallest regression we must catch (a 16 px tick mark) already
exceeds a third of this budget on its own, so this bound is not load-bearing
for element-sized defects; ``MAX_CHANNEL_DELTA`` catches those. Its job is the
*low-amplitude, wide-area* defect: a background shade off by two, a gradient
computed slightly wrong, a supersample factor changed.
"""

_UPDATE_GOLDEN_OFF = frozenset({"", "0", "false"})


def _update_golden_enabled() -> bool:
    """True only when ``UPDATE_GOLDEN`` is set to something that means "yes".

    ``UPDATE_GOLDEN=0`` and ``UPDATE_GOLDEN=false`` are how a developer turns a
    flag off; treating them as truthy would silently rewrite every reference
    image instead of comparing against it.
    """
    return os.environ.get("UPDATE_GOLDEN", "").strip().lower() not in _UPDATE_GOLDEN_OFF


def _diff_stats(image: Image.Image, reference: Image.Image) -> tuple[int, int, int]:
    """Return ``(max per-channel delta, differing pixel count, total pixels)``.

    Both sides are compared in RGBA, so alpha is diffed like any other channel
    rather than being dropped by a ``convert("RGB")``. A renderer that destroys
    the alpha channel is a real defect and must not compare equal.
    """
    diff = ImageChops.difference(image.convert("RGBA"), reference.convert("RGBA"))
    bands = diff.split()
    worst = bands[0]
    for band in bands[1:]:
        worst = ImageChops.lighter(worst, band)
    histogram = worst.histogram()
    total = worst.size[0] * worst.size[1]
    max_delta = max((level for level, count in enumerate(histogram) if count), default=0)
    return max_delta, total - histogram[0], total


@pytest.fixture
def assert_golden(request: pytest.FixtureRequest) -> Callable[..., None]:
    """Compare an image against ``tests/golden/<test module>/<name>.png``.

    Goldens are namespaced by test module so two modules may use the same
    short name without silently overwriting each other under ``UPDATE_GOLDEN``.

    ``max_channel_delta`` / ``max_differing_fraction`` override the module
    defaults for a single call. Loosening them is legitimate only when the
    render under test is genuinely non-deterministic at the pixel level —
    e.g. it downsamples a supersampled canvas with a resampling filter whose
    rounding differs across Pillow builds, or it draws a large soft gradient
    where a one-level rounding difference covers most of the panel. It is not
    legitimate as a way to make a failing golden pass; regenerate the golden
    instead, and justify the regeneration.
    """
    module = request.path.stem

    def _assert(
        image: Image.Image,
        name: str,
        *,
        max_channel_delta: int = MAX_CHANNEL_DELTA,
        max_differing_fraction: float = MAX_DIFFERING_FRACTION,
    ) -> None:
        path = GOLDEN_DIR / module / f"{name}.png"
        if _update_golden_enabled():
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)
            return
        assert path.exists(), (
            f"missing golden {path}; regenerate with UPDATE_GOLDEN=1 uv run pytest"
        )
        with Image.open(path) as reference:
            reference.load()
            assert image.size == reference.size, f"size {image.size} != golden {reference.size}"
            assert image.mode == reference.mode, f"mode {image.mode} != golden {reference.mode}"
            max_delta, differing, total = _diff_stats(image, reference)
        fraction = differing / total
        assert max_delta <= max_channel_delta and fraction <= max_differing_fraction, (
            f"golden {module}/{name} differs: "
            f"max channel delta {max_delta} (limit {max_channel_delta}); "
            f"{differing} of {total} pixels differ "
            f"({fraction:.4%}, limit {max_differing_fraction:.4%})"
        )

    return _assert
