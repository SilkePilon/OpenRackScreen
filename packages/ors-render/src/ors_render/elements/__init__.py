"""The element-renderer registry.

Each element family lives in its own module here and registers itself with
`register`, so `ors_render.render` dispatches on the element's ``type`` without
knowing which families exist.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ors_schema.palette import GradientPalette

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas, parse_hex_color
from ors_render.context import RenderContext
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


def resolve_color(
    color: str | None, palette: GradientPalette, data: Mapping[str, Any] | None = None
) -> tuple[int, int, int] | None:
    """Resolve a color field, which may be `#rrggbb`, `@palette`, or a binding.

    ``None`` resolves to ``None``, meaning *no colour at all*. The schema models
    a genuinely absent colour that way -- `RectElement.fill` and `.stroke`,
    `RingElement.track` -- and Pillow reads a ``None`` ink as "do not draw this
    part", so the absence travels from scene JSON to the draw call untouched.
    Handling it here rather than at each call site keeps every caller of a
    nullable colour field on one rule; the alternative, an ``if element.fill``
    guard repeated per call, is a line each new element family must remember to
    write and silently mis-handles any falsy-but-present value.
    """
    if color is None:
        return None
    if "{{" in color:
        color = resolve_text(color, data or {}) or "#ffffff"
    if color == "@palette":
        return gradient_color(palette, 1.0)
    return parse_hex_color(color, (255, 255, 255))
