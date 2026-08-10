"""Palette lookup for gauges: pick a band by value, then a colour inside it.

`resolve_palette` and `gradient_color` are always used together and are keyed
off the same reading, so they share one rule for non-finite values:

* ``+inf`` is a **reading off the top of the scale**, not a missing one -- a
  Prometheus counter ratio genuinely produces it -- so it behaves exactly like
  any finite over-range value: the top threshold band, and the high end of that
  band's gradient.
* ``-inf`` is off the bottom of the scale: the first band, low end.
* ``NaN`` is **no reading at all** and must never paint a full or critical
  gauge, so it also reads as empty: the first band, low end.

Colour is therefore monotone in the reading across the whole extended real
line, and the two functions can never disagree -- in particular they can never
produce the mixed state where the most severe band renders its least severe
stop. The alternative (all non-finite values read as "no reading") was
rejected because it makes an off-the-scale reading render green: a gauge that
looks healthy is the one failure direction that is never recoverable by eye,
and the ring's *sweep* is computed separately from its colour, so colour is the
only channel that carries severity here.
"""

from __future__ import annotations

import math
import re

from ors_schema.palette import GradientPalette, PaletteRef, ThresholdPalette

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _grad(*colors: str) -> GradientPalette:
    step = 1.0 / (len(colors) - 1)
    return GradientPalette(stops=[{"at": i * step, "color": c} for i, c in enumerate(colors)])


NAMED_PALETTES: dict[str, GradientPalette] = {
    "cyan": _grad("#00e5ff", "#2979ff"),
    "green": _grad("#69f0ae", "#00c853"),
    "lime": _grad("#76ff03", "#00c853"),
    "amber": _grad("#ffeb3b", "#ff9100"),
    "red": _grad("#ff5252", "#b71c1c"),
    "blue": _grad("#00b0ff", "#0091ea"),
    "orange": _grad("#ff9100", "#e65100"),
    "violet": _grad("#d500f9", "#7b1fa2"),
    "mono": _grad("#ffffff", "#9e9e9e"),
    "idle": _grad("#0d0d1a", "#1a1a2e"),
}


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Parse a `#rrggbb` literal, raising on anything else.

    The unchecked primitive. Nothing on the render path may call it directly on
    a colour that came out of scene JSON -- use `parse_hex_color`, which is the
    same parse with a fallback.
    """
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def parse_hex_color(color: str, fallback: tuple[int, int, int] = (0, 0, 0)) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` literal, degrading to ``fallback`` for anything else.

    The single place that decides what counts as a colour *literal*, shared by
    the canvas background (which re-exports it), `ors_render.elements.resolve_color`
    and `gradient_color`. `hex_to_rgb` alone is not enough: the schema's
    ``Color`` type also admits ``@palette`` and ``{{binding}}``, either of which
    would make `hex_to_rgb` raise. Rendering degrades rather than crashing, so a
    value this function cannot read becomes ``fallback``.

    It lives here rather than in `ors_render.canvas` because a *gradient stop*
    is a `Color` too, and a palette module that imported the canvas to say so
    would close an import cycle.
    """
    if not _HEX_COLOR.fullmatch(color):
        return fallback
    return hex_to_rgb(color)


def _stop_rgb(color: str) -> tuple[int, int, int]:
    """One gradient stop, degrading to white. See `gradient_color`."""
    return parse_hex_color(color, (255, 255, 255))


def gradient_color(palette: GradientPalette, t: float) -> tuple[int, int, int]:
    """Colour at position ``t`` of ``palette``, clamped to the 0..1 stop range.

    ``t`` is clamped on the *extended* real line: ``+inf`` renders the high end
    and ``-inf`` the low end, the limits of the finite clamp. Only ``NaN`` is
    special-cased, to the low end, because it is not a position at all -- it is
    a sensor that reported nothing, and must not paint a full gauge. See the
    module docstring; `resolve_palette` follows the same rule.

    A *stop* is read through `parse_hex_color`, not `hex_to_rgb`. `Stop.color`
    is the schema's ``Color``, so ``@palette`` and ``{{params.c}}`` are both
    schema-valid there and neither is resolvable from inside a palette: a stop
    carries no data to resolve a binding against, and ``@palette`` naming the
    palette it sits in is circular. So they degrade to white, the same fallback
    `ors_render.elements.resolve_color` uses for an unreadable colour -- white
    rather than the parser's default black because a black arc on the black
    panel is an *invisible* one, and a gauge that looks empty is the failure
    direction this module exists to avoid.
    """
    if math.isnan(t):
        # NaN would otherwise survive `max(0.0, min(1.0, t))` as 1.0 and render
        # the top of the gradient -- a red "critical" alarm on the threshold
        # palettes -- for a reading that never arrived.
        t = 0.0
    t = max(0.0, min(1.0, t))
    stops = palette.stops
    if len(stops) == 1 or t <= stops[0].at:
        return _stop_rgb(stops[0].color)
    for first, second in zip(stops, stops[1:], strict=False):
        if first.at <= t <= second.at:
            span = (second.at - first.at) or 1.0
            f = (t - first.at) / span
            a, b = _stop_rgb(first.color), _stop_rgb(second.color)
            return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))  # type: ignore[return-value]
    return _stop_rgb(stops[-1].color)


def resolve_palette(ref: PaletteRef, value_pct: float = 0.0) -> GradientPalette:
    """Resolve a palette reference, picking a threshold band by ``value_pct``.

    The last entry whose ``at`` the value reaches wins, so ``+inf`` reaches
    every entry and selects the top (most severe) band, matching what
    `gradient_color` does with it. ``NaN`` compares false against every ``at``
    and ``-inf`` reaches none of them, so both fall through to the seeded first
    band -- the same "no reading / below the scale" answer `gradient_color`
    gives. See the module docstring.
    """
    if isinstance(ref, str):
        return NAMED_PALETTES.get(ref, NAMED_PALETTES["mono"])
    if isinstance(ref, ThresholdPalette):
        chosen = ref.thresholds[0].palette
        for entry in ref.thresholds:
            if value_pct >= entry.at:
                chosen = entry.palette
        return resolve_palette(chosen, value_pct)
    return ref
