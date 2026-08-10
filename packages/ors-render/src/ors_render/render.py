from __future__ import annotations

from ors_schema.scene import Element, Scene
from PIL import Image

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS
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
    # Only some element families carry a palette, and the ones that do resolve
    # their own value-dependent band; the flat 0.0 here just picks the first
    # band of a threshold palette so a renderer always receives a gradient.
    palette_ref = getattr(element, "palette", None) or "mono"
    renderer(canvas, element, ctx, resolve_palette(palette_ref, 0.0))
