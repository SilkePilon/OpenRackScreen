from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import TextElement
from PIL import ImageDraw, ImageFont

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color
from ors_render.fonts import load_font


@register("text")
def render_text(
    canvas: Canvas, element: TextElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    text = resolve_text(element.text, ctx.data)
    if not text:
        return

    geometry = canvas.geometry
    font = load_font(element.font, geometry.font_px(element.size))
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
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    if element.align == "left":
        x = cx - box[0]
    elif element.align == "right":
        x = cx - width - box[0]
    else:
        x = cx - width / 2 - box[0]
    canvas.draw.text((x, cy - height / 2 - box[1]), text, font=font, fill=fill)


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
