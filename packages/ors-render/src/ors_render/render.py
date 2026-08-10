from __future__ import annotations

from collections.abc import Sequence

from ors_schema.scene import Element, Scene
from PIL import Image

from ors_render.bindings import resolve_number
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS
from ors_render.elements import media as _media  # noqa: F401 - registers the renderers
from ors_render.elements import ring as _ring  # noqa: F401 - registers the renderers
from ors_render.elements import shapes as _shapes  # noqa: F401 - registers the renderers
from ors_render.elements import text as _text  # noqa: F401 - registers the renderer
from ors_render.elements.group import work_budget  # this import registers the group renderer
from ors_render.expr import ExpressionError, truthy
from ors_render.geometry import Geometry
from ors_render.palettes import resolve_palette


def select_scene(scenes: Sequence[Scene], ctx: RenderContext) -> Scene | None:
    """The first scene whose `when` passes, or None when none of them do.

    A scene without a `when` always passes, which is what makes it the screen's
    fallback -- and also means every scene after it is unreachable, so a
    template has to author its catch-all last. That ordering is a property of
    the scene list, not something this function can check: "no condition" and
    "a condition that happens to hold" are indistinguishable here.

    Selecting nothing is a legitimate state (a screen whose conditions all
    describe situations that aren't happening), not an error; `render_screen`
    turns it into a blank panel.
    """
    for scene in scenes:
        try:
            if truthy(scene.when, ctx.data):
                return scene
        except ExpressionError:
            # Same bargain as `draw_element`: a malformed condition in
            # user-authored JSON skips its scene and lets the next one -- in
            # practice the unconditional fallback -- take the screen, rather
            # than taking the whole display down.
            continue
    return None


def render_screen(
    scenes: Sequence[Scene], ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    """Render the screen's active scene, or a blank panel when none is active.

    The blank panel is built through the same `Canvas` as a real render, so it
    is identical in size and mode to every other frame and needs no special
    handling from the caller pushing it to a panel.
    """
    scene = select_scene(scenes, ctx)
    if scene is None:
        return Canvas(Geometry(size=size, supersample=supersample), "#000000").finish()
    return render_scene(scene, ctx, size=size, supersample=supersample)


def render_scene(
    scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    canvas = Canvas(Geometry(size=size, supersample=supersample), scene.background)
    # One work budget for the whole scene, so nested `repeat` groups cannot
    # multiply their way to a frame that takes minutes. See
    # `ors_render.elements.group._MAX_CHILD_DRAWS`.
    with work_budget():
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
    # it. `resolve_palette` matches a 0..100 *percentage* against each band's
    # `at`, so the reading is scaled by the element's own range first: a fan on
    # a 0..8000 rpm scale would otherwise sit in the top band from 90 rpm up,
    # and a 0..8 load ring could never leave the first one. The `getattr`
    # defaults are the schema's own, so an element with no `value`/`min`/`max`
    # at all resolves to 0% -- the first band -- exactly as before.
    palette_ref = getattr(element, "palette", None) or "mono"
    low = getattr(element, "min", 0.0)
    high = getattr(element, "max", 100.0)
    # `default=low` matches what `ring.render_ring` uses when it resolves the
    # same binding for the sweep. The two resolutions cannot be collapsed into
    # one without giving every renderer a fifth parameter it has no use for, so
    # they are kept in step by sharing the default instead: a reading that never
    # arrived has to pick the band of an *empty* gauge, and on a scale starting
    # below zero a bare 0.0 would be a mid-scale -- possibly critical -- band
    # under a visibly empty ring.
    value = resolve_number(getattr(element, "value", low), ctx.data, default=low)
    percent = 100.0 * (value - low) / ((high - low) or 1.0)
    renderer(canvas, element, ctx, resolve_palette(palette_ref, percent))
