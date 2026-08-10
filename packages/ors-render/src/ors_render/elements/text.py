"""The text element, and the three ways it declines to draw.

Text is the family with the least tolerance for a strange number, because it is
the only one that hands its coordinates to freetype and to Pillow's glyph
blitter rather than to a shape primitive that clips them. `cx`, `cy` and `size`
are plain unbounded floats in the schema, so everything below is valid scene
JSON someone can author directly -- no `step` on a group required -- and each
case used to arrive as a traceback out of `render_scene`:

* **A position that is not a number** -- NaN or an infinity, or a finite value
  so large that scaling it to pixels overflows (``cy: 1e308``). The element is
  *skipped*. There is no honest place to put it: a NaN is not a coordinate at
  all, and clamping an overflowed one to the canvas edge would invent a
  position the scene never asked for and drop the label on top of real content.
  Compare `ors_render.elements.pixel_width`, which clamps a non-finite stroke
  width instead of skipping: a rect with a broken stroke still has a fill and a
  place to be, so there is something left to draw. Here the position *is* the
  element.
* **A size that is not a number**, likewise skipped, for the reason
  `ors_render.elements.ring` skips a ring whose `thickness` is non-finite: the
  size is the element, and a glyph with no size is nothing.
* **A size that is merely absurd**, which is *clamped* rather than skipped -- a
  number the scene meant, just bigger than the panel. The cap is one panel
  height, past which a glyph shows at most a fragment of one stroke. It is also
  well below where the stack gives up: measured, ``size: 1e4`` trips Pillow's
  own decompression-bomb guard inside `getmask2` and ``size: 1e5`` freetype's
  "invalid pixel size". The clamp is two-sided, because the *bottom* end
  overflows too: `Geometry.font_px` already floors a negative size at one pixel,
  but it does so with a `round` that raises on the ``-inf`` that scaling
  ``size: -1e308`` produces before the floor is ever reached.

A fourth case is not degradation but plain culling: text whose box lies wholly
off the canvas is not drawn. Pillow would clip it to the same result, but only
after the coordinate has reached its C rasteriser, which indexes with a signed
64-bit integer -- measured, ``cy: 1e17`` raises ``SystemError`` out of
``draw_bitmap``. Culling first means no finite coordinate can get that far.
"""

from __future__ import annotations

import math

from ors_schema.palette import GradientPalette
from ors_schema.scene import TextElement
from PIL import ImageDraw, ImageFont

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color
from ors_render.fonts import load_font

_MAX_SIZE = 240.0
"""Largest `size` honoured, in the schema's own px-at-a-240px-baseline units.

One panel height. `Geometry.font_px` scales it to the supersampled canvas, so
this is a glyph exactly as tall as the render surface whatever the supersample
factor.
"""


@register("text")
def render_text(
    canvas: Canvas, element: TextElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    text = resolve_text(element.text, ctx.data)
    if not text:
        return

    geometry = canvas.geometry
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(element.size)):
        # Checked *after* scaling for the position, because that is where a
        # finite-but-huge field turns into an infinity: `cy: 1e308` is a valid
        # float right up to the moment it is multiplied by the canvas size. See
        # the module docstring for why these skip rather than clamp.
        return

    # Clamped at both ends before scaling, so `font_px` can never be handed a
    # product that overflows. Zero is not a special case: `font_px` floors at
    # one pixel, which is what a negative size already rendered as.
    font = load_font(element.font, geometry.font_px(max(0.0, min(element.size, _MAX_SIZE))))
    fill = resolve_color(element.color, palette, ctx.data)

    # Pillow's `textlength` refuses multiline text, and the schema gives a text
    # element no line-spacing control anyway, so a newline-bearing string is
    # simply drawn as-is rather than measured and clipped.
    if element.max_width is not None and "\n" not in text:
        text = _truncate(
            canvas.draw, text, font, geometry.span(element.max_width), element.ellipsis
        )

    box = canvas.draw.textbbox((0, 0), text, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    if element.align == "left":
        x = cx - box[0]
    elif element.align == "right":
        x = cx - width - box[0]
    else:
        x = cx - width / 2 - box[0]
    y = cy - height / 2 - box[1]

    # `x`/`y` are the *anchor* Pillow draws from; the ink lands at the anchor
    # plus the bbox origin, which is what has to miss the canvas for the label
    # to be invisible. See the module docstring: Pillow would clip to the same
    # picture, but only after handing the coordinate to a rasteriser that
    # indexes with a C integer.
    left, top = x + box[0], y + box[1]
    if left + width <= 0 or top + height <= 0 or left >= geometry.px or top >= geometry.px:
        return
    canvas.draw.text((x, y), text, font=font, fill=fill)


def _truncate(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    limit: float,
    ellipsis: bool,
) -> str:
    """Drop characters from the end until `text` measures no wider than `limit`.

    Text that already fits is returned untouched. When `ellipsis` is set the
    last surviving character is traded for a ``.`` as soon as that fits, which
    is why the trailing dot never pushes the result back over the limit. A
    limit too narrow for even one glyph empties the string; drawing "" is a
    no-op, so the element simply disappears rather than overflowing.
    """
    while text and draw.textlength(text, font=font) > limit:
        text = text[:-1]
        if ellipsis and text:
            candidate = text[:-1] + "."
            if draw.textlength(candidate, font=font) <= limit:
                return candidate
    return text
