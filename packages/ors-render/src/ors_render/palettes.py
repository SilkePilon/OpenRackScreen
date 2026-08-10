from __future__ import annotations

from ors_schema.palette import GradientPalette, PaletteRef, ThresholdPalette


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
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def gradient_color(palette: GradientPalette, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    stops = palette.stops
    if len(stops) == 1 or t <= stops[0].at:
        return hex_to_rgb(stops[0].color)
    for first, second in zip(stops, stops[1:], strict=False):
        if first.at <= t <= second.at:
            span = (second.at - first.at) or 1.0
            f = (t - first.at) / span
            a, b = hex_to_rgb(first.color), hex_to_rgb(second.color)
            return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))  # type: ignore[return-value]
    return hex_to_rgb(stops[-1].color)


def resolve_palette(ref: PaletteRef, value_pct: float = 0.0) -> GradientPalette:
    if isinstance(ref, str):
        return NAMED_PALETTES.get(ref, NAMED_PALETTES["mono"])
    if isinstance(ref, ThresholdPalette):
        chosen = ref.thresholds[0].palette
        for entry in ref.thresholds:
            if value_pct >= entry.at:
                chosen = entry.palette
        return resolve_palette(chosen, value_pct)
    return ref
