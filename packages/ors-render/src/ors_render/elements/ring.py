"""The ring and arc elements: a gradient sweep over a dark track.

The ring is the signature element -- every screen on the rack is one -- so the
angle conventions are worth stating once, here, rather than being rediscovered
per call site:

* Pillow measures angles **from 3 o'clock, increasing clockwise** (the y axis
  points down, so "clockwise" is the direction of increasing angle in screen
  space). The schema's ``start_angle`` default of -90 is therefore 12 o'clock,
  and a ``cw`` ring adds to its angle.
* ``ImageDraw.arc`` draws its ``width`` **inward** from the bounding box, so the
  box is the ring's *outer* edge and the stroke's centre line sits at
  ``radius - width / 2``. That is where the dot cap has to go to be centred on
  the stroke rather than straddling its outer edge.
* Pillow normalizes an ``end`` below ``start`` by adding 360 until it is not, so
  ``arc(box, 90, 0)`` draws 270 degrees rather than nothing. A counter-clockwise
  sweep must therefore be handed over as ``(lower, higher)``, not as the
  ``(from, to)`` pair the scene asked for, or it renders as its own complement.
  Only an exactly equal pair draws nothing.
"""

from __future__ import annotations

import math

from ors_schema.palette import GradientPalette
from ors_schema.scene import ArcElement, RingElement

from ors_render.bindings import resolve_number
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color
from ors_render.palettes import gradient_color

_SEGMENT_DEGREES = 2.0
"""Angular length of one flat-coloured step of a gradient sweep.

A full ring is 180 steps, so a two-stop gradient advances by well under one
colour level per step: the sweep reads as continuous, and the cost is 180 cheap
arc calls rather than a per-pixel gradient.
"""

_MAX_SEGMENTS = 720
"""Ceiling on the step count, so a wild ``arc`` angle cannot hang the render.

``from_angle``/``to_angle`` are unbounded floats straight from scene JSON, and
the step count is proportional to the sweep: 1e9 degrees would be half a billion
draw calls. Past a full circle the extra sweep only paints over what is already
there, so capping costs nothing a scene can see.
"""

_MAX_RADIUS_CANVASES = 2.0
"""Ceiling on a ring's radius, as a multiple of the canvas size.

`r` is an unbounded float and `ImageDraw.arc` costs time proportional to the
perimeter of its bounding box, so a radius the panel cannot contain buys
unbounded work for a picture nobody sees: measured, ``{"type": "ring", "r":
100}`` takes 0.68 s and ``r: 1000`` 6.7 s against 0.01 s for an ordinary ring,
and ``r: 5000`` was killed after 20 s. Nothing raises, so the "never crashes"
rule held while the panel simply stopped refreshing -- which on a screen that
redraws several times a second is the same outage.

Two canvases is the bound because the farthest apart two points on the panel can
be is its diagonal, ``sqrt(2)`` canvases: a circle of any larger radius, centred
anywhere on the panel, passes entirely outside it, and 2 leaves margin for a
centre placed somewhat off the edge. At the bound one arc costs 0.04 s, still
inside a refresh.

Past it the ring is *skipped*, not clamped, for the reason `_circle` skips a
non-finite thickness: the radius **is** the ring. A clamped one would come back
as a perfectly ordinary-looking gauge at a size the scene never asked for --
indistinguishable from a correct render, and so a lie about the reading --
whereas a missing element is visibly missing. Same purpose as `_MAX_SEGMENTS`:
a bound the scene cannot see, so untrusted input cannot buy unbounded work.
"""

_MAX_THICKNESS_DIAMETERS = 2.0
"""Ceiling on a ring's stroke width, as a multiple of its own radius.

`thickness` is a plain unbounded float in the schema, so ``{"type": "ring",
"thickness": 1e9}`` is valid scene JSON -- and `ImageDraw.arc` converts the
width to a C integer: measured, 1e9 raised ``OverflowError: signed integer is
greater than maximum`` and 1e17 and 1e300 ``Python int too large to convert to C
long``, out of both the ring and the arc renderer. The `math.isfinite` guard in
`_circle` caught the infinities and missed every finite giant behind them.

This is the ceiling `ors_render.elements.pixel_width` applies to a rect or line
stroke, at the scale this element works in. Its *value* is deliberately not that
helper's `Geometry.px`, and the difference is load-bearing rather than cosmetic:
a stroked rect is a shape on the canvas, so a canvas-wide stroke covers it from
any point on it, whereas a ring's box may be up to `_MAX_RADIUS_CANVASES`
canvases across and Pillow draws the stroke **inward** from that box. A
canvas-wide cap would therefore erase the middle of a ring larger than the panel
-- the filled disc `test_thickness_beyond_the_radius_fills_the_disc` pins would
come back as a blank panel for ``r: 4.0``.

Two radii is where the stroke, running inward from the outer edge, has reached
the centre and continued out the far side, so no wider one can add a pixel
anywhere. Measured: every thickness from 1.1 radii to 1e300 renders
byte-identically to this cap at ``r`` 0.05, 0.2, 0.875, 1.0, 1.7, 2.0, 3.0 and
4.0. Same purpose as `_MAX_SEGMENTS` and `_MAX_RADIUS_CANVASES`: a bound the
scene cannot see, so untrusted input cannot buy a traceback or unbounded work.
"""

_SEAM_OVERLAP_DEGREES = 1.2
"""How far each step is extended into its successor, which then paints over it.

Pillow 12.3 leaves no gap between arcs that share an endpoint (measured: a
180-step ring is pixel-identical to one ``arc(0, 360)`` call at every width from
1 to 60 px), so this is insurance for the older Pillows the package still
allows, not a fix for a defect seen here. It is deliberately *not* applied to
the final step, which has no successor to paint over it and would simply
overshoot the angle the scene asked for -- measured at 93 supersampled pixels
past the end of a default-thickness ring.
"""


_DOT_CAP_RADIUS = 0.55
"""Dot-cap radius as a multiple of the stroke width, so the dot is 1.1x as wide.

Slightly proud of the stroke rather than flush with it: at the default thickness
the cap is 12 px across a 11 px stroke, which reads as a bead riding the ring --
the look the rack's screens already have -- while a flush 1.0 would be a plain
rounded end, and much more would bulge.
"""

Box = tuple[float, float, float, float]
"""A Pillow bounding box, ``(x0, y0, x1, y1)``."""


def _circle(canvas: Canvas, element: ArcElement | RingElement) -> tuple[Box, float, int] | None:
    """The outer bounding box, radius and whole-pixel stroke width of a ring.

    One place for the units decision this element family is easiest to get
    wrong: `r` and `thickness` are fractions of the panel *radius*, so they go
    through `Geometry.radial`, not the `Geometry.span` that
    `ors_render.elements.pixel_width` applies to a rect or line stroke -- half
    the scale, and a ring exactly twice as thick as the scene asked for if the
    two are confused. Only the *units* differ: the width this returns makes all
    three of `pixel_width`'s decisions, and for the same reasons it gives.
    Rounded to the nearest whole pixel, never away to nothing, and **capped** --
    see `_MAX_THICKNESS_DIAMETERS` for why the ceiling here is the ring's own
    diameter rather than the canvas that helper uses.

    ``None`` means there is nothing drawable here. Neither field is bounded by
    the schema, so every bound is this renderer's to make: an inverted or empty
    bounding box has no ring, `round` raises on a non-finite thickness, and a
    radius past `_MAX_RADIUS_CANVASES` is off the panel and costs unbounded time
    to draw. The radius test is written as a positive range rather than as
    ``radius <= 0 or radius > limit`` so that a NaN -- for which *both* of those
    comparisons are false -- is skipped here instead of reaching Pillow as a NaN
    bounding box.
    """
    geometry = canvas.geometry
    radius = geometry.radial(element.r)
    thickness = geometry.radial(element.thickness)
    if not 0.0 < radius <= geometry.px * _MAX_RADIUS_CANVASES or not math.isfinite(thickness):
        return None
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    return box, radius, max(1, round(min(thickness, radius * _MAX_THICKNESS_DIAMETERS)))


def _sweep(
    canvas: Canvas,
    box: Box,
    start: float,
    sweep: float,
    width: int,
    palette: GradientPalette,
) -> None:
    """Draw ``sweep`` degrees from ``start``, stepping the palette along it."""
    if not math.isfinite(start) or not math.isfinite(sweep) or sweep == 0.0:
        # `int()` raises on a non-finite sweep, and a zero sweep has no arc.
        return
    steps = max(1, min(_MAX_SEGMENTS, int(abs(sweep) / _SEGMENT_DEGREES)))
    for index in range(steps):
        first = start + sweep * index / steps
        last = start + sweep * (index + 1) / steps
        overlap = _SEAM_OVERLAP_DEGREES if index < steps - 1 else 0.0
        low, high = (first, last + overlap) if sweep > 0 else (last - overlap, first)
        canvas.draw.arc(box, low, high, fill=gradient_color(palette, index / steps), width=width)


@register("ring")
def render_ring(
    canvas: Canvas, element: RingElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    circle = _circle(canvas, element)
    if circle is None:
        return
    box, radius, width = circle

    track = resolve_color(element.track, palette, ctx.data)
    if track is not None:
        canvas.draw.arc(box, 0, 360, fill=track, width=width)

    # A reading that never arrived reads as `min` -- an empty gauge -- rather
    # than as a bare 0.0, which on a scale that starts below zero (a temperature
    # ring, say) would be a plausible-looking mid-gauge value instead.
    value = resolve_number(element.value, ctx.data, default=element.min)
    span = (element.max - element.min) or 1.0
    fraction = (value - element.min) / span
    if not math.isfinite(fraction):
        # `min(1.0, nan)` returns 1.0, so an unguarded clamp would turn a scale
        # that is not a scale (`min`/`max` are unbounded floats from scene JSON,
        # and NaN - NaN is NaN) into a *saturated* gauge at the accent colour.
        # `ors_render.palettes` is explicit that a non-reading must never paint a
        # full or critical gauge, so a fraction that is not a number renders as
        # the empty gauge it is.
        return
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0.0:
        return

    sweep = 360.0 * fraction * (1.0 if element.direction == "cw" else -1.0)
    _sweep(canvas, box, element.start_angle, sweep, width, palette)

    if element.cap == "dot" and math.isfinite(element.start_angle):
        # `math.cos` raises on an infinite angle, hence the guard.
        end = math.radians(element.start_angle + sweep)
        # Pillow strokes an arc *inward* from the bounding box, so the stroke's
        # centre line -- where a cap has to sit to look centred rather than
        # perched on the outer edge -- is half a width in from the radius.
        stroke_radius = radius - width / 2
        cx, cy = canvas.geometry.x(element.cx), canvas.geometry.y(element.cy)
        tx = cx + stroke_radius * math.cos(end)
        ty = cy + stroke_radius * math.sin(end)
        dot = width * _DOT_CAP_RADIUS
        # The top of the gradient: the palette's *accent*, the same colour
        # `@palette` resolves to, so the cap matches any label that tracks it.
        # On a nearly empty gauge that makes the bead visibly bluer than the
        # short cyan stub behind it, which is the honest reading -- the stub is
        # the start of the gradient because that is how far round it got.
        canvas.draw.ellipse(
            (tx - dot, ty - dot, tx + dot, ty + dot), fill=gradient_color(palette, 1.0)
        )


@register("arc")
def render_arc(
    canvas: Canvas, element: ArcElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    circle = _circle(canvas, element)
    if circle is None:
        return
    box, _radius, width = circle
    _sweep(canvas, box, element.from_angle, element.to_angle - element.from_angle, width, palette)
