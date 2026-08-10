from __future__ import annotations

from PIL import Image, ImageDraw

from ors_render.geometry import Geometry
from ors_render.palettes import parse_hex_color

# `parse_hex_color` used to be defined here and is still imported from here by
# the element renderers. It moved to `ors_render.palettes` when `gradient_color`
# needed it too: a gradient *stop* is a schema `Color` like any other, and a
# palette module importing the canvas to say so would close an import cycle.
__all__ = ["Canvas", "parse_hex_color"]


class Canvas:
    """The supersampled RGB surface a scene is drawn onto.

    Every element draws at ``geometry.px`` resolution; `finish` downsamples that
    to the panel's real ``geometry.size`` with LANCZOS, which is where the
    antialiasing on arcs and glyph edges actually comes from.
    """

    def __init__(self, geometry: Geometry, background: str = "#000000") -> None:
        # `background` is a colour *literal* by the time it gets here: a scene's
        # may be a binding, and `ors_render.render` resolves it before building
        # the canvas, because an unresolved one would land on `parse_hex_color`'s
        # fallback and paint black without saying so.
        self.geometry = geometry
        self.image = Image.new("RGB", (geometry.px, geometry.px), parse_hex_color(background))
        self.draw = ImageDraw.Draw(self.image)

    def finish(self) -> Image.Image:
        if self.geometry.supersample == 1:
            return self.image
        return self.image.resize((self.geometry.size, self.geometry.size), Image.Resampling.LANCZOS)
