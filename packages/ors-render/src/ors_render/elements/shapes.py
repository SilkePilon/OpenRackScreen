from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import LineElement, RectElement

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color


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
    # Pillow draws the outline *inward* from the box, so a stroked rect stays
    # inside the width and height the scene asked for rather than straddling it.
    width = max(1, int(geometry.span(element.stroke_width)))
    radius = geometry.span(element.radius)
    if radius > 0:
        # Pillow clamps the radius to half the box's smallest dimension, so an
        # over-large radius renders a stadium rather than distorting the box.
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
        width=max(1, int(geometry.span(element.width))),
    )
