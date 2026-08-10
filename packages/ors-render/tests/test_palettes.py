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
#
# The rule both functions obey (see the module docstring of `palettes.py`):
# +inf is a reading off the *top* of the scale and paints the high end of the
# gradient / the top threshold band, exactly like any finite over-range value.
# -inf is off the bottom, and NaN is no reading at all; both paint the low end
# / the first band.


_LADDER = ThresholdPalette.model_validate(
    {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
)


@pytest.mark.parametrize("value", [math.nan, -math.inf])
def test_nan_and_negative_infinity_render_the_low_end_of_the_gradient(value):
    # NaN used to clamp to 1.0 and render "full"; a sensor that reported nothing
    # must not paint a full/critical gauge. -inf is genuinely below the scale.
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, value) == hex_to_rgb(palette.stops[0].color)


def test_positive_infinity_renders_the_high_end_of_the_gradient():
    # +inf is not a missing reading, it is one off the top of the scale, so it
    # renders like any other over-range value rather than like an empty gauge.
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, math.inf) == hex_to_rgb(palette.stops[-1].color)
    assert gradient_color(palette, math.inf) == gradient_color(palette, 200.0)


@pytest.mark.parametrize("value", [math.nan, -math.inf])
def test_nan_and_negative_infinity_do_not_pick_the_top_threshold(value):
    assert resolve_palette(_STEPS, value) == NAMED_PALETTES["green"]


def test_positive_infinity_picks_the_top_threshold():
    assert resolve_palette(_STEPS, math.inf) == NAMED_PALETTES["red"]
    assert resolve_palette(_STEPS, math.inf) == resolve_palette(_STEPS, 200.0)


@pytest.mark.parametrize(
    ("value", "expected_palette", "expected_stop"),
    [
        (math.nan, "green", 0),
        (-math.inf, "green", 0),
        (math.inf, "red", -1),
        (200.0, "red", -1),
        (50.0, "green", -1),
    ],
)
def test_composed_lookup_is_consistent_for_non_finite_values(
    value, expected_palette, expected_stop
):
    # The two functions are always used together: pick a band by value, then
    # pick a colour inside it by the same value. Composing them must never
    # produce the mixed state where the *top* band renders its *low* stop.
    palette = resolve_palette(_LADDER, value)
    assert palette == NAMED_PALETTES[expected_palette]
    assert gradient_color(palette, value) == hex_to_rgb(palette.stops[expected_stop].color)


# --- stops that are not hex literals -----------------------------------------

# `ors_schema.palette.Color` admits `@palette` and `{{binding}}` as well as
# `#rrggbb`, and `Stop.color` is that type, so both forms reach `gradient_color`
# out of schema-valid scene JSON. Neither can be resolved *here* -- a stop has no
# data to resolve a binding against, and `@palette` inside a palette is circular
# -- so both degrade to the same visible white `resolve_color` falls back to,
# rather than raising out of the render.


@pytest.mark.parametrize("color", ["{{params.c}}", "@palette"])
def test_a_stop_that_is_not_a_hex_literal_degrades_instead_of_raising(color):
    palette = GradientPalette(stops=[{"at": 0.0, "color": color}, {"at": 1.0, "color": "#000000"}])
    assert gradient_color(palette, 0.0) == (255, 255, 255)
    assert gradient_color(palette, 1.0) == (0, 0, 0)
    # ...and it interpolates from the fallback rather than raising on the way.
    assert gradient_color(palette, 0.5) == (127, 127, 127)


@pytest.mark.parametrize("color", ["{{params.c}}", "@palette"])
def test_a_lone_unreadable_stop_degrades_instead_of_raising(color):
    # The single-stop short circuit and the past-the-last-stop tail are separate
    # returns from the interpolating path above, and each had its own raise.
    assert gradient_color(GradientPalette(stops=[{"at": 0.0, "color": color}]), 0.5) == (
        255,
        255,
        255,
    )
    tail = GradientPalette(stops=[{"at": 0.0, "color": "#000000"}, {"at": 0.5, "color": color}])
    assert gradient_color(tail, 1.0) == (255, 255, 255)
