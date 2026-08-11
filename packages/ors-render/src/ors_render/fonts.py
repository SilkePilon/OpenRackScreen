from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FILES = {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}


@lru_cache(maxsize=128)
def load_font(weight: Literal["regular", "bold"], px: int) -> ImageFont.FreeTypeFont:
    # Literal is a type-checker hint only; scene JSON and callers can still
    # hand us anything, and a bare KeyError says nothing useful.
    if weight not in _FILES:
        raise ValueError(f"unknown font weight {weight!r}; expected one of {sorted(_FILES)}")
    path = FONT_DIR / _FILES[weight]
    if not path.exists():
        raise FileNotFoundError(f"bundled font missing: {path}")
    return ImageFont.truetype(str(path), px)
