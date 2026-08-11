from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import LineElement, RectElement

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import pixel_width, register, resolve_color


@register("rect")
def render_rect(
    canvas: Canvas, element: RectElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    geometry = canvas.geometry
    w, h = geometry.span(element.w), geometry.span(element.h)
    if w <= 0 or h <= 0:
        # `w` and `h` carry no lower bound in the schema, and Pillow raises on a
        # box whose end coordinate precedes its start. A rect with no area has
        # nothing to show either way, so it is skipped rather than crashing the
        # screen or leaving the hairline that a zero-width box would draw.
        return
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    fill = resolve_color(element.fill, palette, ctx.data)
    stroke = resolve_color(element.stroke, palette, ctx.data)
    if fill is None and stroke is None:
        # `fill` and `stroke` are both nullable in the schema, so a rect asking
        # for neither is valid JSON -- and it must come out invisible. Pillow
        # will not do that for us: `ImageDraw._getink(outline, fill)` reads a
        # `None` ink as "use the draw object's *default* ink" (white), not as
        # "draw nothing", so handing it two `None`s paints a white outline.
        return
    # Pillow draws the outline *inward* from the box, so a stroked rect stays
    # inside the width and height the scene asked for rather than straddling it.
    width = pixel_width(geometry, element.stroke_width)
    radius = geometry.span(element.radius)
    if radius > 0:
        # The radius is capped here rather than left to Pillow. Pillow does cap
        # it -- it has never let an over-large radius distort the box or raise --
        # but the form that makes an over-large radius render *identically* to an
        # exactly-half one, `d = min(x1 - x0, y1 - y0, radius * 2)`, only landed
        # in 12.3.0. Before that the two took different drawing paths (ellipse vs
        # joined halves) and differed by a few pixels. Capping locally keeps this
        # renderer's output the same across every Pillow we support, and says
        # what the shape is meant to be instead of inheriting it.
        radius = min(radius, w / 2, h / 2)
        canvas.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=stroke, width=width)
    else:
        canvas.draw.rectangle(box, fill=fill, outline=stroke, width=width)


@register("line")
def render_line(
    canvas: Canvas, element: LineElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    geometry = canvas.geometry
    canvas.draw.line(
        (
            geometry.x(element.x1),
            geometry.y(element.y1),
            geometry.x(element.x2),
            geometry.y(element.y2),
        ),
        fill=resolve_color(element.color, palette, ctx.data),
        width=pixel_width(geometry, element.width),
    )
