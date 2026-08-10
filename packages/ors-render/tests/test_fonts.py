from __future__ import annotations

import hashlib

import pytest
from ors_render.fonts import FONT_DIR, load_font
from PIL import ImageFont

# Every golden image from here on is tied to these exact glyph outlines and
# metrics. A font bump would invalidate all of them at once, so pin the bytes
# and fail loudly rather than silently rendering different pixels. Hashes are
# recorded alongside their provenance in assets/fonts/LICENSE.
EXPECTED_SHA256 = {
    "DejaVuSans.ttf": "7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954",
    "DejaVuSans-Bold.ttf": "e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724",
}


@pytest.mark.parametrize("filename", ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf"])
def test_font_files_are_bundled(filename):
    assert (FONT_DIR / filename).is_file()


@pytest.mark.parametrize("filename", sorted(EXPECTED_SHA256))
def test_bundled_font_bytes_are_pinned_to_dejavu_2_37(filename):
    digest = hashlib.sha256((FONT_DIR / filename).read_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256[filename], (
        f"{filename} changed (sha256 {digest}); every committed golden image was "
        "rendered against DejaVu 2.37 metrics and must be regenerated deliberately"
    )


def test_the_pinned_hashes_match_the_bundled_licence_record():
    # The LICENSE provenance header is the human-facing record; keep the two
    # in step so neither can drift without the other noticing.
    licence = " ".join((FONT_DIR / "LICENSE").read_text(encoding="utf-8").split())
    for filename, digest in EXPECTED_SHA256.items():
        assert f"{filename} sha256 {digest}" in licence


def test_load_font_returns_a_truetype_font_at_the_requested_size():
    font = load_font("regular", 24)
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert font.size == 24


def test_regular_and_bold_are_distinct_dejavu_sans_faces():
    regular_family, regular_style = load_font("regular", 20).getname()
    bold_family, bold_style = load_font("bold", 20).getname()
    assert regular_family == bold_family == "DejaVu Sans"
    assert bold_style == "Bold"
    assert regular_style != bold_style


def test_loading_is_cached():
    assert load_font("bold", 18) is load_font("bold", 18)


def test_unknown_weight_raises_a_clear_value_error():
    with pytest.raises(ValueError, match="unknown font weight 'italic'"):
        load_font("italic", 18)  # type: ignore[arg-type]
