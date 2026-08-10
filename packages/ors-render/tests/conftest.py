"""Golden-image comparison fixture shared by every visual test in ``ors-render``.

Rendering is deterministic: PNG is lossless, the bundled DejaVu 2.37 faces are
pinned by SHA-256 (see ``test_fonts.py``) and nothing here reads a clock or the
network. So a golden and a fresh render should be byte-identical, and the only
slack worth granting is cross-version Pillow/freetype antialiasing drift — a
*small per-pixel* delta along a handful of glyph and arc edges.

The comparison therefore uses two bounds, both of which must hold:

1. ``MAX_CHANNEL_DELTA``  — how wrong any single pixel may be.
2. ``MAX_DIFFERING_FRACTION`` — how many pixels may differ at all.

Which bound binds depends on the *shape* of the failure, and it is worth being
blunt about it because the two are ANDed:

* A **displaced, deleted or recoloured element** trips ``MAX_CHANNEL_DELTA``
  first: it flips its pixels by 100+ levels against any usable background
  contrast, so it fails even when it is far too small to spend the pixel
  budget.
* **Benign cross-version rasteriser drift** trips ``MAX_DIFFERING_FRACTION``
  first, not ``MAX_CHANNEL_DELTA``. Text is dense in antialiased pixels (see
  that constant's docstring for measured counts), so a drift of only a few
  coverage levels — comfortably inside the per-pixel bound — still moves
  hundreds of pixels and blows the count budget on any panel carrying a label.

So a maintainer whose first failure is a wall of low-amplitude text drift must
not reach for ``MAX_CHANNEL_DELTA``: raising it changes nothing, because it was
never the bound that failed. The correct response to a genuine Pillow or
freetype bump is to regenerate the goldens deliberately — ``UPDATE_GOLDEN=1 uv
run pytest``, then eyeball the diff and say in the commit message which
dependency moved — not to loosen either threshold. Pillow is pinned in
``uv.lock`` and CI syncs against that lock, so such a bump is always a
deliberate, reviewable change rather than something that drifts in on its own;
that is what makes these strict values safe to keep.

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

This is the bound that catches **element-sized** defects, and it catches them
regardless of how few pixels they cover. It is *not* the bound that a benign
antialiasing drift fails on — see ``MAX_DIFFERING_FRACTION`` — so raising this
number is never the fix for a drift failure.
"""

MAX_DIFFERING_FRACTION = 0.001
"""Largest fraction of pixels that may differ at all, at any magnitude.

0.1% is 57 pixels of a 240x240 panel. That is roughly the antialiased edge of a
single 18 px *glyph* — measured with the bundled DejaVu face, one 18 px glyph
carries 43 ("4") to 112 ("%") partially-covered pixels — and nowhere near a
whole label. Measured antialiased-pixel counts for realistic content:

===========================================  ======
one 18 px ``CPU 42%`` label                     372
one 36 px ``42`` readout                        193
one 12 px ``MEM 61%  NET 12M`` line             466
a panel: ring + 18 px label + 12 px sub-label   600-800
the same panel supersampled 4x, then downscaled  ~5700
===========================================  ======

Two consequences follow, and the second is the one that bites:

* This budget is not load-bearing for **element-sized** defects. A 16x2 px tick
  mark spends 32 of the 57 pixels, so it can sit inside the budget;
  ``MAX_CHANNEL_DELTA`` is what fails it.
* This budget **is** the bound that binds for **benign low-amplitude drift**.
  A Pillow/freetype bump that shifts every antialiased pixel by a few coverage
  levels stays inside ``MAX_CHANNEL_DELTA`` (measured: a uniform 4-level shift
  on the labels above yields max delta 4, half the limit) yet moves every one
  of those 372 / 193 / 466 pixels — 3x to 8x over this budget. Any golden with
  text on it fails here first.

So the count bound is strict *by design* for text panels: it says "text
rendered identically", which is exactly what a pinned Pillow guarantees and
what a Pillow bump legitimately breaks. Regenerate goldens for such a bump; do
not raise this number to absorb it. Its other job is the genuinely wide-area
defect: a background shade off by two, a gradient computed slightly wrong, a
supersample factor changed.
"""

_UPDATE_GOLDEN_OFF = frozenset({"", "0", "false"})


def _flag(name: str) -> bool:
    """True when env var ``name`` is set to something that means "yes".

    ``0`` and ``false`` are how a developer turns a flag off; treating them as
    truthy would silently rewrite every reference image instead of comparing
    against it.
    """
    return os.environ.get(name, "").strip().lower() not in _UPDATE_GOLDEN_OFF


def _update_golden_enabled() -> bool:
    """True only when ``UPDATE_GOLDEN`` means "yes"; hard error under CI.

    ``UPDATE_GOLDEN`` turns every ``assert_golden`` call into a no-op that
    overwrites its own reference, so a run with it set proves nothing about the
    renderer. That is the point locally — but if it ever leaks into CI, which
    runs a bare ``uv run pytest``, the whole visual suite goes green while
    rewriting the very files it was supposed to check, and nothing in the log
    says so.

    Refusing to run is chosen over merely printing a warning banner: a banner is
    only as loud as its reader, and nobody reads the log of a *green* CI job,
    which is precisely the case that must not go unnoticed. Locally the mode is
    already self-announcing — the rewritten goldens show up in ``git status``
    — so CI is the only place the silence is dangerous, and a red build is the
    one signal that cannot be scrolled past.
    """
    if not _flag("UPDATE_GOLDEN"):
        return False
    if _flag("CI"):
        raise RuntimeError(
            "UPDATE_GOLDEN is set in a CI environment. It would rewrite every "
            "golden and pass the visual suite vacuously. Regenerate goldens "
            "locally, commit them, and let CI compare against them."
        )
    return True


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
