from __future__ import annotations

from collections.abc import Mapping, Sequence

from ors_schema.scene import Element, Scene
from PIL import Image

from ors_render.bindings import resolve, resolve_number, resolve_text
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

    The concrete case a screen has to get right today: the built-in `torrent`
    scene must be ordered **before** `node-health`'s scene, which carries no
    `when` and therefore always matches. A screen composing the pair the other
    way round renders the node readout forever and never switches to the
    download view, with nothing failing to say so.
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


def expand_params(ctx: RenderContext) -> RenderContext:
    """Resolve the bindings a screen supplied as template *parameters*, once.

    A template is a scene with holes in it: `ring-gauge` draws
    ``{{params.big}}``, and the screen using it supplies
    ``big = "{{prom.cpu | round:0}}%"``. That is what `ParamSpec(type="binding")`
    means -- the parameter's *value* is itself a binding -- so a field resolving
    to ``{{params.big}}`` lands on another binding and has to be resolved again.
    Without this pass every gauge on the rack paints its own source text across
    the panel, which is what the parity goldens caught.

    Done here, on the parameters, rather than by making
    `ors_render.bindings.resolve` recursive, because the two inputs have
    different provenance and only one may be treated as code. Parameters come
    from the operator's own screen config, the same trust level as the scene
    JSON around them. The values bindings resolve *to* come from Prometheus and
    qBittorrent -- a torrent name, a node label -- and a torrent named
    ``{{prom.cpu}}`` must be drawn as those fourteen characters, not evaluated.
    One pass over one namespace is the whole distinction, and it also means
    expansion cannot recurse: there is no depth to bound.

    The parameters are resolved against the *unexpanded* data, so a parameter
    referring to another parameter reads its raw value, and one referring to a
    ``repeat`` alias sees nothing -- the alias only exists inside the group, far
    below here. Both are limits of a single pass.

    So a value that still contains ``{{`` after its pass is **blanked**. That is
    the residue guard, and it is what makes the single pass defensible rather
    than merely cheap: ``big = "{{params.inner}}"`` over ``inner =
    "{{prom.cpu}}"`` would otherwise leave the literal text ``{{prom.cpu}}`` in
    a parameter, and a scene drawing ``{{params.big}}`` at 52 px paints that
    source straight across the panel and out past the bezel -- precisely the
    visible garbage this function exists to prevent. Blank is the right
    degradation because there is no reading to show: the operator's config is
    wrong, and an empty field says so more honestly than its own source code
    does. Note the guard fires on the *resolved* value, so a parameter can still
    carry braces a screen genuinely wants drawn only if they arrive from live
    data, which is exactly what the second pass below must not evaluate.

    That also makes this function **idempotent**: expanding twice renders what
    expanding once does. It matters because `expand_params` is public and
    `render_scene` calls it internally, so a caller that pre-expands -- a daemon
    inspecting resolved parameters before it renders -- gets two passes. Without
    the guard the second pass would evaluate a binding that came out of a param
    resolving to *upstream* data: a torrent named ``{{prom.cpu}}`` would print a
    reading, which is the trust boundary above collapsing. With it, one pass
    leaves nothing for a second to find.
    """
    params = ctx.data.get("params")
    if not isinstance(params, Mapping):
        return ctx
    expanded = {}
    for key, value in params.items():
        if isinstance(value, str) and "{{" in value:
            value = resolve(value, ctx.data)
        if isinstance(value, str) and "{{" in value:
            value = ""
        expanded[key] = value
    return ctx.child({"params": expanded})


def render_screen(
    scenes: Sequence[Scene], ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    """Render the screen's active scene, or a blank panel when none is active.

    The blank panel is built through the same `Canvas` as a real render, so it
    is identical in size and mode to every other frame and needs no special
    handling from the caller pushing it to a panel.

    This is *scene selection*, so it is the wrong entry point for the built-in
    `system` template: none of its four scenes (`connecting`, `stale`, `error`,
    `identify`) carries a `when`, so the first one always wins and
    ``render_screen(system.scenes, ctx)`` can only ever draw `connecting`. Those
    scenes describe states the daemon knows about and the scene JSON cannot --
    an integration that has not connected yet, data that has gone stale, a
    physical identify request -- so the daemon picks one **by name** and renders
    it alone (``render_screen([scene], ctx)`` or `render_scene`).
    """
    ctx = expand_params(ctx)
    scene = select_scene(scenes, ctx)
    if scene is None:
        return Canvas(Geometry(size=size, supersample=supersample), "#000000").finish()
    return _draw_scene(scene, ctx, size=size, supersample=supersample)


def render_scene(
    scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    return _draw_scene(scene, expand_params(ctx), size=size, supersample=supersample)


def _draw_scene(
    scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    """Draw one scene against an already-expanded context.

    Split from `render_scene` so that `render_screen` expands the parameters
    exactly once -- before `select_scene`, so a scene condition sees the same
    values its elements will -- rather than once for the selection and again for
    the render.
    """
    # `Scene.background` is a schema `Color`, so it may be a binding --
    # `"background": "{{params.bg}}"` is a themed screen supplying its own panel
    # colour, the same way `text-only` supplies a text one. Resolved here rather
    # than in `Canvas` because this is where the data is; unresolved it would
    # reach `parse_hex_color` as literal binding source and paint black without
    # anything saying why. An unreadable result still lands on that black, which
    # is the panel's own default.
    background = scene.background
    if "{{" in background:
        background = resolve_text(background, ctx.data)
    canvas = Canvas(Geometry(size=size, supersample=supersample), background)
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
    if isinstance(palette_ref, str) and "{{" in palette_ref:
        # A palette is a template *parameter* -- `"palette": "{{params.palette}}"`
        # is what makes one `ring-gauge` serve both the cyan CPU screen and the
        # green MEM one -- so the binding has to be resolved before
        # `resolve_palette` ever sees it, which only understands names and
        # inline definitions. An unresolvable one falls back to `mono` rather
        # than to the empty string `resolve` yields, which would otherwise reach
        # `NAMED_PALETTES` as a missing key and land on `mono` anyway, just less
        # deliberately. The `str` check is load-bearing for the same reason: a
        # whole-string binding resolves to the *raw* value, so params holding an
        # inline palette as a plain dict would otherwise arrive at
        # `gradient_color` as a dict and raise.
        resolved = resolve(palette_ref, ctx.data)
        palette_ref = resolved if isinstance(resolved, str) and resolved else "mono"
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
