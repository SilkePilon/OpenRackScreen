import math

import pytest
from ors_render.palettes import NAMED_PALETTES, gradient_color, hex_to_rgb, resolve_palette
from ors_schema.palette import GradientPalette, ThresholdPalette


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
    assert resolve_palette(ThresholdPalette.model_validate(ref), value) == NAMED_PALETTES[expected]


# --- threshold-selection boundaries (characterization: today's behaviour) -----


_STEPS = ThresholdPalette.model_validate(
    {
        "kind": "threshold",
        "thresholds": [
            {"at": 10, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
)


@pytest.mark.parametrize(("value", "expected"), [(10, "green"), (70, "amber"), (90, "red")])
def test_threshold_entry_is_selected_exactly_on_its_own_boundary(value, expected):
    # `value_pct >= entry.at`, so a value sitting exactly on a threshold takes
    # that threshold's palette rather than the one below it.
    assert resolve_palette(_STEPS, value) == NAMED_PALETTES[expected]


@pytest.mark.parametrize("value", [-1, 0, 9, 9.999])
def test_value_below_the_first_threshold_uses_the_first_entry(value):
    # No entry matches, so the seeded `chosen = thresholds[0].palette` stands.
    assert resolve_palette(_STEPS, value) == NAMED_PALETTES["green"]


def test_a_threshold_entry_may_itself_be_a_threshold_palette():
    nested = {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "lime"},
            {"at": 80, "palette": "violet"},
        ],
    }
    outer = ThresholdPalette.model_validate(
        {
            "kind": "threshold",
            "thresholds": [
                {"at": 0, "palette": "cyan"},
                {"at": 50, "palette": nested},
            ],
        }
    )
    assert resolve_palette(outer, 10) == NAMED_PALETTES["cyan"]
    assert resolve_palette(outer, 60) == NAMED_PALETTES["lime"]
    assert resolve_palette(outer, 85) == NAMED_PALETTES["violet"]


def test_a_threshold_entry_may_be_an_inline_gradient():
    inline = GradientPalette(stops=[{"at": 0.0, "color": "#010203"}])
    outer = ThresholdPalette(thresholds=[{"at": 0, "palette": inline}])
    assert resolve_palette(outer, 50) == inline


# --- non-finite gauge values --------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_value_renders_the_low_end_of_the_gradient(value):
    # NaN used to clamp to 1.0 and render "full"; a sensor that reported nothing
    # must not paint a full/critical gauge. All non-finite values read as empty.
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, value) == hex_to_rgb(palette.stops[0].color)


def test_non_finite_value_does_not_pick_the_top_threshold():
    assert resolve_palette(_STEPS, math.nan) == NAMED_PALETTES["green"]
