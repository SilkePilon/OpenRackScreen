from __future__ import annotations

import re

from PIL import Image, ImageDraw

from ors_render.geometry import Geometry
from ors_render.palettes import hex_to_rgb

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def parse_hex_color(color: str, fallback: tuple[int, int, int] = (0, 0, 0)) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` literal, degrading to ``fallback`` for anything else.

    The single place that decides what counts as a colour *literal*, shared by
    the canvas background and `ors_render.elements.resolve_color`. `hex_to_rgb`
    alone is not enough: the schema's ``Color`` type also admits ``@palette``,
    and a binding can resolve to arbitrary text, either of which would make
    `hex_to_rgb` raise. Rendering degrades rather than crashing, so a value this
    function cannot read becomes ``fallback``.
    """
    if not _HEX_COLOR.fullmatch(color):
        return fallback
    return hex_to_rgb(color)


class Canvas:
    """The supersampled RGB surface a scene is drawn onto.

    Every element draws at ``geometry.px`` resolution; `finish` downsamples that
    to the panel's real ``geometry.size`` with LANCZOS, which is where the
    antialiasing on arcs and glyph edges actually comes from.
    """

    def __init__(self, geometry: Geometry, background: str = "#000000") -> None:
        self.geometry = geometry
        self.image = Image.new("RGB", (geometry.px, geometry.px), parse_hex_color(background))
        self.draw = ImageDraw.Draw(self.image)

    def finish(self) -> Image.Image:
        if self.geometry.supersample == 1:
            return self.image
        return self.image.resize((self.geometry.size, self.geometry.size), Image.Resampling.LANCZOS)
