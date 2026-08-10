from __future__ import annotations

from ors_schema.scene import Element, Scene
from PIL import Image

from ors_render.bindings import resolve_number
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS
from ors_render.elements import ring as _ring  # noqa: F401 - registers the renderers
from ors_render.elements import shapes as _shapes  # noqa: F401 - registers the renderers
from ors_render.elements import text as _text  # noqa: F401 - registers the renderer
from ors_render.expr import ExpressionError, truthy
from ors_render.geometry import Geometry
from ors_render.palettes import resolve_palette


def render_scene(
    scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    canvas = Canvas(Geometry(size=size, supersample=supersample), scene.background)
    for element in scene.elements:
        draw_element(canvas, element, ctx)
    return canvas.finish()


def draw_element(canvas: Canvas, element: Element, ctx: RenderContext) -> None:
    try:
        if not truthy(element.when, ctx.data):
            return
    except ExpressionError:
        # `when` comes from user-authored scene JSON, so a malformed condition
        # hides the element rather than taking the whole screen down.
        return
    renderer = RENDERERS.get(element.type)
    if renderer is None:
        return
    # A renderer always receives a *gradient*, so the threshold band is picked
    # here, from the element's own reading -- the ring is the only family with
    # one today, and it is the family whose colour is supposed to change with
    # it. The reading is compared against the thresholds in the element's own
    # units (`min`/`max` are the ring's business, not the palette's), and an
    # element with no `value` at all resolves to the first band.
    palette_ref = getattr(element, "palette", None) or "mono"
    value = resolve_number(getattr(element, "value", 0.0), ctx.data)
    renderer(canvas, element, ctx, resolve_palette(palette_ref, value))
