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
    def decorator(func: ElementRenderer) -> ElementRenderer:
        RENDERERS[type_name] = func
        return func

    return decorator


def resolve_color(
    color: str, palette: GradientPalette, data: Mapping[str, Any] | None = None
) -> tuple[int, int, int]:
    """Resolve a color field, which may be `#rrggbb`, `@palette`, or a binding."""
    if "{{" in color:
        color = resolve_text(color, data or {}) or "#ffffff"
    if color == "@palette":
        return gradient_color(palette, 1.0)
    return parse_hex_color(color, (255, 255, 255))
