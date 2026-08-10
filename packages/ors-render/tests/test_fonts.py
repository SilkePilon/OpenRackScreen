from __future__ import annotations

import pytest
from ors_render.fonts import FONT_DIR, load_font
from PIL import ImageFont


@pytest.mark.parametrize("filename", ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf"])
def test_font_files_are_bundled(filename):
    assert (FONT_DIR / filename).is_file()


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
