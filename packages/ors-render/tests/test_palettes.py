import pytest
from ors_render.palettes import NAMED_PALETTES, gradient_color, hex_to_rgb, resolve_palette
from ors_schema.palette import GradientPalette


def test_hex_to_rgb():
    assert hex_to_rgb("#00e5ff") == (0, 229, 255)


def test_named_palettes_cover_the_builtin_set():
    for name in ("cyan", "green", "lime", "amber", "red", "violet", "blue", "mono"):
        assert name in NAMED_PALETTES


def test_gradient_interpolates_between_stops():
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, 0.0) == hex_to_rgb(palette.stops[0].color)
    assert gradient_color(palette, 1.0) == hex_to_rgb(palette.stops[-1].color)
    mid = gradient_color(palette, 0.5)
    assert mid != gradient_color(palette, 0.0)


def test_gradient_clamps_out_of_range():
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, -5) == gradient_color(palette, 0.0)
    assert gradient_color(palette, 99) == gradient_color(palette, 1.0)


def test_gradient_clamps_below_a_first_stop_that_is_not_at_zero():
    # scene JSON may declare stops that do not span the whole 0..1 range
    palette = GradientPalette(
        stops=[{"at": 0.5, "color": "#ff0000"}, {"at": 1.0, "color": "#0000ff"}]
    )
    assert gradient_color(palette, 0.0) == (255, 0, 0)
    assert gradient_color(palette, 0.25) == (255, 0, 0)
    assert gradient_color(palette, 1.0) == (0, 0, 255)


def test_unknown_palette_name_falls_back_to_mono():
    assert resolve_palette("does-not-exist", 50) == NAMED_PALETTES["mono"]


@pytest.mark.parametrize(("value", "expected"), [(10, "green"), (75, "amber"), (99, "red")])
def test_threshold_palette_selects_by_value(value, expected):
    ref = {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
    from ors_schema.palette import ThresholdPalette

    assert resolve_palette(ThresholdPalette.model_validate(ref), value) == NAMED_PALETTES[expected]
