"""The group element: a container, and the `repeat` that makes a list a screen.

A group draws its children in order and otherwise contributes no ink of its own.
Everything a group does beyond that is about *one list becoming several
elements*, which is what the torrent screen is: three concentric rings, one per
active download, each a little smaller and a different colour.

Three knobs, all keyed off the iteration index, all scoped to the group:

* ``repeat`` -- draws the children once per item, layering the alias (``t``) and
  ``index`` onto the context via `RenderContext.child`. That is a *new* context,
  so neither name is visible to anything outside the group; nested repeats
  shadow an outer ``index`` with their own, the innermost winning, and an alias
  literally named ``index`` loses to the counter for the same reason.
* ``step`` -- a per-iteration delta on numeric fields of the *direct* children:
  iteration ``i`` renders ``field + delta * i``. Direct children only, because a
  group has no geometry of its own for a delta to land on -- stepping a nested
  group's ``cx`` would move nothing.
* ``palettes`` -- cycles a palette per iteration, replacing the child's own.

All three are properties of the *iteration*, so a group with no ``repeat`` is a
plain container: ``step`` would have no index to multiply and ``palettes`` no
cycle to run, and both are ignored rather than silently applied at index 0.

`draw_element` is what recurses, so a child's ``when``, its bindings and its own
palette resolution all behave inside a group exactly as they do at the top
level, and a group nested in a group needs no special case.

Recursion depth is bounded by the schema rather than guarded here: pydantic-core
refuses to build a model nested past its own recursion limit, so the deepest
scene that ever reaches this module is ~254 groups (measured), which costs a few
hundred stack frames. A scene is untrusted JSON from a web UI, so that bound is
load-bearing -- but it belongs to the parse, and duplicating it as a depth
counter here would be a second number to keep in step with pydantic's.

*Total work* is a different bound, and the schema does not supply it. `limit`
caps one repeat at 32 items, but nesting multiplies: three levels is 32 768 leaf
draws (measured at 0.45 s), four is 1 048 576 (14.1 s), five about seven and a
half minutes. Nothing raises, so "degrades, never crashes" is upheld to the
letter while a panel refreshing several times a second simply stops. Hence
`work_budget`: see `_MAX_CHILD_DRAWS`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ors_schema.palette import GradientPalette, PaletteRef
from ors_schema.scene import Element, GroupElement

from ors_render.bindings import resolve_list
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register

_MAX_CHILD_DRAWS = 2048
"""Ceiling on the elements one `render_scene` call may draw from inside groups.

The number a *real* scene needs is tiny: the torrent screen repeats three rings,
and a busy dashboard a few dozen elements. A 240x240 panel cannot show a
thousand distinct things, so 2048 leaves every honest scene two to three orders
of magnitude of headroom while cutting the four-level pathological nest from
1 048 576 draws to 0.2% of that.

It counts *every* child a group draws, not just the ones inside a `repeat`, and
that is what makes it a bound rather than a suggestion: a repeat whose child is
a plain container group would otherwise multiply that container's contents
freely while spending one unit per iteration.

Same purpose as `ors_render.elements.ring`'s `_MAX_SEGMENTS` -- a ceiling the
scene cannot see, so untrusted JSON cannot buy unbounded work -- and the same
failure mode: past it, drawing stops. It does not raise. A scene over the
ceiling is already nothing a person authored on purpose, and half a panel is a
better answer than a traceback.
"""


class _Budget:
    """How many more child elements this render may draw."""

    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self) -> bool:
        """Claim one draw, or answer ``False`` when there are none left."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


_BUDGET: ContextVar[_Budget | None] = ContextVar("ors_render_group_budget", default=None)
"""The budget the current render is spending, if one has been opened.

A `ContextVar` rather than an argument because the recursion runs through
`ors_render.render.draw_element`, whose signature is the element-renderer
contract every family shares -- threading a counter through it would put a
parameter on every renderer for the benefit of exactly one. A plain module
global would do the same job for a single-threaded render and then silently
share one counter between two panels rendered concurrently; a `ContextVar` is
per-thread and per-task without the caller having to know.
"""


@contextmanager
def work_budget() -> Iterator[_Budget]:
    """Open a fresh work budget for the duration of one render.

    `ors_render.render.render_scene` wraps a whole scene in one of these, so
    every group in it shares a single ceiling: a scene holds as many sibling
    groups as its JSON has room for, and a budget per group would multiply the
    ceiling by that count. The token is reset on the way out, so the budget
    belongs to the call rather than to the process -- a scene that spends all of
    it renders identically on the next frame.
    """
    budget = _Budget(_MAX_CHILD_DRAWS)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def _stepped(
    child: Element, step: dict[str, float], index: int, palette_override: PaletteRef | None
) -> Element:
    """`child` with its stepped fields offset and its palette overridden.

    Returns a *new* element rather than editing the one it was given. The
    children here come straight from the parsed scene and are re-rendered
    several times a second, so an in-place ``+=`` would not step the ring once
    -- it would shrink it a little on every frame until it vanished.
    `model_copy` is the cheap way to say that: it shallow-copies, so the child's
    own nested models (a group's `elements`, an inline palette) are shared with
    the original rather than deep-copied per item per frame, which is safe
    precisely because nothing here mutates them either.

    `model_copy(update=...)` does **not** validate what it is handed -- it writes
    straight into the new model's ``__dict__`` -- so every value put in `updates`
    has to be valid by construction. Both are: a stepped field is a float built
    from a field that already held a number, and a palette override is a
    `PaletteRef` the schema already validated on the group.

    Two kinds of field are deliberately left alone. One the child does not have
    at all, which `getattr` reports as ``None`` -- an unvalidated update would
    otherwise graft a stray attribute onto the model. And one that is not a
    number, ``bool`` included: ``bool`` is a subclass of ``int``, so an
    unguarded `isinstance` check would read ``ellipsis: true`` as ``1`` and step
    it to ``2`` -- or, on a negative delta, to a falsy ``0`` that silently
    changes how a label truncates.

    An offset that comes out *non-finite* is dropped too, and the field keeps
    the value the scene authored. This is the one way a group can manufacture a
    number no scene ever wrote: `step` is a ``dict[str, float]``, so a NaN delta
    is schema-valid, and even two finite ones overflow (``1e308 + 1e308 * 2`` is
    ``inf``). A non-finite coordinate is not a position -- it is arithmetic that
    got away -- so the honest degrade is the element where the scene put it, not
    a NaN handed down to a renderer.
    """
    if not step and palette_override is None:
        return child
    updates: dict[str, object] = {}
    for field, delta in step.items():
        current = getattr(child, field, None)
        if not isinstance(current, int | float) or isinstance(current, bool):
            continue
        offset = current + delta * index
        if math.isfinite(offset):
            updates[field] = offset
    if palette_override is not None and hasattr(child, "palette"):
        # Only children that *have* a palette: `draw_element` reads the field
        # with `getattr(element, "palette", None)`, so grafting one onto a rect
        # would quietly repaint its `@palette` fill.
        updates["palette"] = palette_override
    return child.model_copy(update=updates) if updates else child


@register("group")
def render_group(
    canvas: Canvas, element: GroupElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    budget = _BUDGET.get()
    if budget is not None:
        _draw_children(canvas, element, ctx, budget)
        return
    # A group reached without a budget open -- a caller driving `draw_element`
    # itself rather than going through `render_scene` -- still gets a ceiling,
    # just one scoped to this subtree instead of to a whole scene.
    with work_budget() as fresh:
        _draw_children(canvas, element, ctx, fresh)


def _draw_children(
    canvas: Canvas, element: GroupElement, ctx: RenderContext, budget: _Budget
) -> None:
    """Draw the group's children once, or once per `repeat` item.

    Every child costs one unit of `budget`, and an exhausted budget ends the
    render's group drawing outright rather than skipping one child and carrying
    on: nothing further can be drawn anyway, and returning here is what stops
    the enclosing levels from spinning through their own iterations.
    """
    # Imported here rather than at module scope: `render` imports this module
    # for its registration side effect, so a top-level import would be a cycle.
    from ors_render.render import draw_element

    if element.repeat is None:
        for child in element.elements:
            if not budget.spend():
                return
            draw_element(canvas, child, ctx)
        return

    # `resolve_list` answers `[]` for anything that is not a list, so a binding
    # pointing at a mapping, a number or nothing at all draws nothing.
    items = resolve_list(element.repeat.over, ctx.data)[: element.repeat.limit]
    for index, item in enumerate(items):
        child_ctx = ctx.child({element.repeat.as_: item, "index": index})
        override = element.palettes[index % len(element.palettes)] if element.palettes else None
        for child in element.elements:
            if not budget.spend():
                return
            draw_element(canvas, _stepped(child, element.step, index, override), child_ctx)
