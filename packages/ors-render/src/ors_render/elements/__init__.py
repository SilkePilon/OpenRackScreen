"""The element-renderer registry.

Each element family lives in its own module here and registers itself with
`register`, so `ors_render.render` dispatches on the element's ``type`` without
knowing which families exist.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from ors_schema.palette import GradientPalette

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas, parse_hex_color
from ors_render.context import RenderContext
from ors_render.geometry import Geometry
from ors_render.palettes import gradient_color

ElementRenderer = Callable[[Canvas, Any, RenderContext, GradientPalette], None]
"""Draw one element. The element is typed `Any` rather than `Element` because
each renderer narrows it to its own concrete model, which a parameter typed with
the full union would not permit."""

RENDERERS: dict[str, ElementRenderer] = {}


def register(type_name: str) -> Callable[[ElementRenderer], ElementRenderer]:
    """Register `func` as the renderer for elements of type `type_name`.

    A second registration of a name already taken is refused rather than
    honoured. Element families are registered by importing their module for its
    side effect, so a copy-paste slip -- two modules both claiming ``"arc"`` --
    would otherwise disable a whole family silently, with the loser depending on
    nothing more visible than import order in `ors_render.render`.
    """

    def decorator(func: ElementRenderer) -> ElementRenderer:
        existing = RENDERERS.get(type_name)
        if existing is not None:
            raise ValueError(
                f"element type {type_name!r} is already registered to "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        RENDERERS[type_name] = func
        return func

    return decorator


def pixel_width(geometry: Geometry, width: float) -> int:
    """Convert a normalized stroke width to whole supersampled pixels.

    Stroke and line widths are fractions of the full panel size, so `Geometry.span`
    is what maps them. A ring's `thickness` is not one of these: like `r`, it is a
    fraction of the panel *radius* and goes through `Geometry.radial` instead, so
    a ring renderer rounds `radial(thickness)` itself rather than calling this.
    Shared rather than written out per call site because every stroked element
    needs the same two decisions, and both are easy to get quietly wrong:

    *Rounded, not truncated.* `RectElement.stroke_width` defaults to 0.004, which
    is 1.92 px on the 2x supersampled canvas. `int` cuts that to 1 px -- 0.5 px
    on the final panel -- so the default stroke comes out at half the weight the
    scene asked for, while `round` gives the 2 px (1.0 final px) it meant. The
    error is worst exactly where it is most visible, on sub-2px strokes; wide
    strokes barely notice either way (a ring thickness of 22.08 px differs by
    0.4%).

    *Floored at 1.* A width the scene asked for stays visible instead of rounding
    away to an invisible zero, which Pillow would draw as nothing.

    A *non-finite* width lands on that same floor. `RectElement.stroke_width`
    and `LineElement.width` are plain unbounded floats, so `{"type": "line",
    "width": Infinity}` is schema-valid scene JSON -- and `round` raises on it
    (``ValueError`` for NaN, ``OverflowError`` for either infinity), which took
    the whole screen down. 1 px rather than "draw nothing" because the number is
    what is broken, not the intent: the scene asked for a stroke, so it gets the
    thinnest honest one, and the element around it -- a rect's fill, the line
    itself -- still appears. Contrast `ors_render.elements.ring`, which skips a
    ring whose `thickness` is non-finite: there the thickness *is* the element,
    so there is nothing left to draw at any width. Here there is.
    """
    scaled = geometry.span(width)
    return max(1, round(scaled)) if math.isfinite(scaled) else 1


def resolve_color(
    color: str | None, palette: GradientPalette, data: Mapping[str, Any] | None = None
) -> tuple[int, int, int] | None:
    """Resolve a color field, which may be `#rrggbb`, `@palette`, or a binding.

    ``None`` resolves to ``None``, meaning *no colour at all*. The schema models
    a genuinely absent colour that way -- `RectElement.fill` and `.stroke`,
    `RingElement.track` -- so the absence travels from scene JSON to the renderer
    untouched. Handling it here rather than at each call site keeps every caller
    of a nullable colour field on one rule; the alternative, an ``if
    element.fill`` guard repeated per call, is a line each new element family
    must remember to write and silently mis-handles any falsy-but-present value.

    What ``None`` does **not** do is travel on into Pillow as "skip this part".
    `ImageDraw._getink(outline, fill)` falls back to the draw object's *default*
    ink when both arguments are ``None`` -- white -- so ``rectangle(box,
    fill=None, outline=None)`` paints a white outline rather than nothing.
    Deciding that an element with no colours left has nothing to draw is the
    renderer's job, before the draw call; see `ors_render.elements.shapes`.
    """
    if color is None:
        return None
    if "{{" in color:
        color = resolve_text(color, data or {}) or "#ffffff"
    if color == "@palette":
        return gradient_color(palette, 1.0)
    return parse_hex_color(color, (255, 255, 255))
