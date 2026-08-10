import pytest
from ors_render.context import RenderContext
from ors_render.render import render_scene
from ors_schema.scene import Scene

CTX = RenderContext(data={"prom": {"cpu": 42.4}})

# `r` and `thickness` are fractions of the panel *radius*, so the default ring
# (r=0.875, thickness=0.092) has an outer edge 105 px from the centre of the
# 240 px panel and a stroke 11 px wide: a band between radius 95 and 105, which
# these probes sample down the middle of at radius 100. A point 114 px out --
# (120, 6), the panel's own edge -- is background for any correct ring.
TOP = (120, 20)
RIGHT = (220, 120)
BOTTOM = (120, 220)
LEFT = (20, 120)

TRACK = (22, 24, 30)
"""The default `#16181e` track, as it survives the supersampled downscale."""


def _ring(**overrides):
    element = {"type": "ring", "value": "{{prom.cpu}}", "palette": "cyan"}
    element.update(overrides)
    return Scene.model_validate({"elements": [element]})


def _arc(**overrides):
    element = {"type": "arc", "palette": "violet"}
    element.update(overrides)
    return Scene.model_validate({"elements": [element]})


def test_ring_draws_the_track_all_the_way_round():
    image = render_scene(_ring(value=0), CTX)
    # An empty gauge still shows its track, and shows it at every compass point
    # rather than only where the sweep would have started.
    assert [image.getpixel(p) for p in (TOP, RIGHT, BOTTOM, LEFT)] == [TRACK] * 4


def test_ring_starts_at_twelve_oclock_and_sweeps_clockwise():
    image = render_scene(_ring(value=25), CTX)
    right = image.getpixel(RIGHT)
    left = image.getpixel(LEFT)
    assert right != left, "a 25% clockwise sweep should reach 3 o'clock but not 9 o'clock"
    assert left == TRACK, "9 o'clock is still bare track"


@pytest.mark.parametrize("value", [0, 42, 100])
def test_ring_golden(assert_golden, value):
    assert_golden(render_scene(_ring(value=value, cap="dot"), CTX), f"ring_{value}")


def test_threshold_palette_changes_colour_with_value():
    palette = {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
    low = render_scene(_ring(value=10, palette=palette), CTX)
    high = render_scene(_ring(value=95, palette=palette), CTX)
    assert low.getpixel(TOP) != high.getpixel(TOP)


def test_palette_token_in_text_uses_the_elements_palette(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "ring", "value": 80, "palette": "amber", "cap": "dot"},
                {"type": "text", "cy": 0.28, "size": 15, "text": "MEM", "color": "@palette"},
            ]
        }
    )
    assert_golden(render_scene(scene, CTX), "ring_palette_token")


def test_arc_renders_explicit_angles(assert_golden):
    assert_golden(render_scene(_arc(from_angle=-90, to_angle=90), CTX), "arc_half")


def test_a_backwards_arc_is_not_drawn_as_its_complement():
    # Pillow normalizes an `end` below `start` by adding 360 until it is not, so
    # handing it (90, -90) verbatim would draw the *other* 180 degrees. Each step
    # is handed over lowest angle first instead, which makes this the same
    # right-hand half as (-90, 90) with the gradient running the other way.
    forward = render_scene(_arc(from_angle=-90, to_angle=90), CTX)
    backward = render_scene(_arc(from_angle=90, to_angle=-90), CTX)
    assert backward.getpixel(RIGHT) != (0, 0, 0)
    assert backward.getpixel(LEFT) == (0, 0, 0)
    assert backward.getpixel(TOP) != forward.getpixel(TOP)


def test_gradient_runs_along_the_sweep():
    # The whole point of the element: colour tracks position along the sweep, so
    # a full ring is a different colour a quarter of the way round than it is
    # three quarters. A flat fill -- the failure mode of resolving the palette
    # once instead of per step -- would make these equal.
    image = render_scene(_ring(value=100), CTX)
    assert image.getpixel(RIGHT) != image.getpixel(LEFT)


@pytest.mark.parametrize("value", [100, 100.5, 1e6])
def test_value_at_or_beyond_max_is_a_full_ring(value):
    # `value` is live upstream data on a scene-declared scale, so over-range is
    # normal input, not an error: it saturates at a full sweep rather than
    # wrapping past 12 o'clock or spilling out of the palette.
    full = render_scene(_ring(value=100, cap="dot"), CTX)
    assert render_scene(_ring(value=value, cap="dot"), CTX).tobytes() == full.tobytes()


@pytest.mark.parametrize("value", [0, -0.5, -1e6, "{{prom.absent}}", "{{prom.cpu | round:abc}}"])
def test_value_at_or_below_min_leaves_the_gauge_empty(value):
    # At `min` the gauge is empty, and below it stays empty. A binding that
    # resolves to nothing -- a missing field, a filter that could not run -- is
    # the same case and must never paint a reading that did not arrive; the dot
    # cap in particular must not appear parked at 12 o'clock.
    empty = render_scene(_ring(value=0, cap="dot"), CTX)
    assert render_scene(_ring(value=value, cap="dot"), CTX).tobytes() == empty.tobytes()
    assert empty.getpixel(TOP) == TRACK


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_reading_draws_no_sweep(value):
    # `resolve_number` scrubs every non-finite reading to its default before the
    # ring ever sees it, so +inf -- which `ors_render.palettes` would treat as a
    # reading off the *top* of the scale -- arrives here as "no reading" and
    # renders empty. Empty is the safe direction for the sweep (a gauge must not
    # invent a reading), but it does mean the palette's off-the-top rule cannot
    # be reached through a binding; see the task report.
    empty = render_scene(_ring(value=0, cap="dot"), CTX)
    assert render_scene(_ring(value=value, cap="dot"), CTX).tobytes() == empty.tobytes()


def test_min_and_max_set_the_scale():
    # The fraction is of the element's own range, not of 0..100.
    half = render_scene(_ring(value=50), CTX)
    assert render_scene(_ring(value=4, min=2, max=6), CTX).tobytes() == half.tobytes()


def test_counter_clockwise_sweeps_the_other_way():
    image = render_scene(_ring(value=25, direction="ccw"), CTX)
    assert image.getpixel(LEFT) != TRACK, "a ccw quarter sweep reaches 9 o'clock"
    assert image.getpixel(RIGHT) == TRACK, "and leaves 3 o'clock bare"


def test_start_angle_moves_where_the_sweep_begins():
    # Pillow measures 0 degrees at 3 o'clock, so `start_angle: 0` starts there
    # and a clockwise quarter runs down to 6 o'clock.
    image = render_scene(_ring(value=25, start_angle=0), CTX)
    assert image.getpixel(BOTTOM) != TRACK
    assert image.getpixel(TOP) == TRACK


def test_thickness_beyond_the_radius_fills_the_disc():
    # `thickness` is not bounded by the schema and Pillow draws a stroke inward
    # from the bounding box, so an over-thick ring is a filled disc rather than
    # an error or a stroke that spills outside `r`.
    image = render_scene(_ring(value=100, thickness=1.0), CTX)
    assert image.getpixel((120, 120)) != (0, 0, 0), "the stroke reaches the centre"
    assert image.getpixel((120, 8)) == (0, 0, 0), "and still stops at r"


@pytest.mark.parametrize(
    "element",
    [
        {"type": "ring", "r": 0.0},
        {"type": "ring", "r": -0.5},
        {"type": "ring", "value": 50, "min": float("nan"), "max": float("nan")},
        {"type": "ring", "value": 50, "start_angle": float("inf"), "cap": "dot"},
        {"type": "ring", "value": 50, "thickness": float("nan")},
        {"type": "ring", "value": 50, "track": "@palette"},
        {"type": "arc", "from_angle": 0, "to_angle": 0},
        {"type": "arc", "from_angle": 0, "to_angle": 1e9},
        {"type": "arc", "to_angle": float("inf")},
    ],
    ids=str,
)
def test_degenerate_rings_and_arcs_render_rather_than_raise(element):
    # Every one of these parses, so every one of them reaches the renderer: a
    # zero or negative radius (Pillow rejects an inverted box), a non-finite
    # number reaching `round` or `math.cos`, `@palette` where a hex track is
    # expected, and an angle span large enough to be a step count of its own.
    render_scene(Scene.model_validate({"elements": [element]}), CTX)
