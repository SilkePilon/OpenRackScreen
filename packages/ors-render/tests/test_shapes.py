"""The rect and line primitive elements, and the registry helpers they share."""

from __future__ import annotations

from typing import Any

import pytest
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS, pixel_width, register, resolve_color
from ors_render.elements.shapes import render_rect
from ors_render.geometry import Geometry
from ors_render.palettes import resolve_palette
from ors_render.render import render_scene
from ors_schema.scene import Scene
from PIL import Image


def _render(element: dict[str, Any]) -> Image.Image:
    return render_scene(Scene.model_validate({"elements": [element]}), RenderContext())


def test_rect_fills_expected_pixels():
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "rect", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.5, "fill": "#ff0000"}
            ]
        }
    )
    image = render_scene(scene, RenderContext())
    assert image.getpixel((120, 120)) == (255, 0, 0)
    assert image.getpixel((10, 10)) == (0, 0, 0)


def test_rounded_rect_and_line_render(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "rect",
                    "cy": 0.35,
                    "w": 0.6,
                    "h": 0.12,
                    "radius": 0.06,
                    "fill": "#2979ff",
                },
                {
                    "type": "rect",
                    "cy": 0.55,
                    "w": 0.6,
                    "h": 0.12,
                    "radius": 0.06,
                    "fill": None,
                    "stroke": "#69f0ae",
                    "stroke_width": 0.008,
                },
                {
                    "type": "line",
                    "x1": 0.2,
                    "y1": 0.75,
                    "x2": 0.8,
                    "y2": 0.75,
                    "color": "#ffffff",
                    "width": 0.01,
                },
            ]
        }
    )
    assert_golden(render_scene(scene, RenderContext()), "shapes_basic")


def test_stroke_only_rect_outlines_inside_the_box():
    image = _render(
        {
            "type": "rect",
            "w": 0.5,
            "h": 0.5,
            "fill": None,
            "stroke": "#ff0000",
            "stroke_width": 0.02,
        }
    )
    # The box spans supersampled x 120..360, i.e. final x 60..180. Pillow draws
    # the outline inward from each edge, so a 0.02 stroke (9.6 supersampled px,
    # rounded to 10 by `pixel_width`, 5 after downsampling) gives two bands
    # *inside* those edges and nothing between them. The bands are not mirror
    # images: Pillow's box is inclusive of x1, so the left band covers
    # supersampled 120..129 -- five whole final pixels -- while the right one
    # covers 351..360, straddling final pixels 175 and 180 by half each. The
    # >128 threshold keeps the whole left band and those two half-lit edges out,
    # along with the sub-pixel LANCZOS bleed either side of each band.
    lit = [x for x in range(240) if image.getpixel((x, 120))[0] > 128]
    assert lit == [60, 61, 62, 63, 64, 176, 177, 178, 179]


@pytest.mark.parametrize("shape", [{}, {"radius": 0.02}])
def test_rect_with_neither_fill_nor_stroke_draws_nothing(shape):
    # `fill` and `stroke` are both `Color | None`, so "no fill, no stroke" is
    # valid scene JSON -- and it must not paint anything. Passing both through as
    # `None` does paint: `ImageDraw._getink(outline, fill)` reads a `None` ink as
    # "use the draw object's *default* ink", which is white, not as "draw
    # nothing". Both the square and the rounded call are checked because they are
    # separate Pillow entry points.
    blank = render_scene(Scene(), RenderContext())
    image = _render({"type": "rect", "w": 0.3, "h": 0.1, "fill": None, **shape})
    assert image.tobytes() == blank.tobytes()


def test_radius_larger_than_half_the_box_renders_a_stadium():
    # `render_rect` clamps the radius to half the box's smallest dimension, so
    # anything past that is the same fully-rounded shape rather than a distorted
    # box. Pillow caps it too, but only its 12.3.0 form of the cap gives a result
    # byte-identical to the exactly-half radius, so the clamp is ours to make.
    bar = {"type": "rect", "w": 0.6, "h": 0.12, "fill": "#2979ff"}
    half = _render({**bar, "radius": 0.06})
    assert _render({**bar, "radius": 0.5}).tobytes() == half.tobytes()
    assert _render({**bar, "radius": 0.01}).tobytes() != half.tobytes()


@pytest.mark.parametrize(
    "size", [{"w": 0.0}, {"h": 0.0}, {"w": -0.5}, {"h": -0.5}, {"w": -0.5, "radius": 0.05}]
)
def test_rect_with_no_area_draws_nothing(size):
    # `w`/`h` carry no lower bound in the schema, and Pillow rejects a box whose
    # end coordinate precedes its start. A rect with no area has nothing to show,
    # so it is skipped rather than crashing the whole screen.
    blank = render_scene(Scene(), RenderContext())
    assert _render({"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000", **size}).tobytes() == (
        blank.tobytes()
    )


def test_line_with_identical_endpoints_draws_only_a_dot():
    # A zero-length line has no direction to give it width; Pillow collapses it
    # to a single point rather than dividing by its own length, so the element
    # degrades to a faint dot at its endpoint instead of raising.
    image = _render({"type": "line", "x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 0.5, "width": 0.05})
    lit = [(x, y) for y in range(240) for x in range(240) if image.getpixel((x, y)) != (0, 0, 0)]
    assert lit and all(abs(x - 120) <= 2 and abs(y - 120) <= 2 for x, y in lit)


def test_register_rejects_a_duplicate_type(monkeypatch):
    # Element families register by import side effect, so a second claim on a
    # name must fail loudly instead of silently unhooking the first renderer.
    # `setitem` restores the incumbent on teardown, so a regression here cannot
    # leak the stub below into the registry the rest of the suite renders with.
    monkeypatch.setitem(RENDERERS, "rect", render_rect)
    with pytest.raises(ValueError, match="already registered"):
        register("rect")(lambda canvas, element, ctx, palette: None)
    assert RENDERERS["rect"] is render_rect


def test_resolve_color_maps_none_to_no_colour():
    # `RectElement.fill` and `.stroke` are `Color | None`; absence has to survive
    # the shared helper as absence, so that each renderer can see it and decide
    # what to skip. (Pillow will *not* decide for it -- see
    # `test_rect_with_neither_fill_nor_stroke_draws_nothing`.)
    palette = resolve_palette("mono")
    assert resolve_color(None, palette) is None
    assert resolve_color("#ff0000", palette) == (255, 0, 0)


def test_pixel_width_rounds_rather_than_truncating():
    # Stroke widths are fractions of the panel, so the schema's default 0.004 is
    # 1.92 px on the 480 px supersampled canvas. Truncating gives 1 px, which is
    # half a pixel on the final 240 px panel -- a visible halving of every thin
    # stroke -- where rounding gives the 2 px (1.0 final px) the scene asked for.
    geometry = Geometry()
    assert pixel_width(geometry, 0.004) == 2
    assert pixel_width(geometry, 0.008) == 4
    assert pixel_width(geometry, 0.01) == 5
    assert pixel_width(geometry, 0.02) == 10
    # A positive width never rounds away to nothing, and a zero one still draws
    # the thinnest line Pillow can, exactly as `max(1, ...)` did before.
    assert pixel_width(geometry, 0.0001) == 1
    assert pixel_width(geometry, 0.0) == 1


@pytest.mark.parametrize("width", [float("nan"), float("inf"), float("-inf")])
def test_pixel_width_degrades_a_non_finite_width_to_the_thinnest_stroke(width: float):
    # `round` raises on a non-finite number -- ValueError for NaN, OverflowError
    # for either infinity -- and `stroke_width`/`width` are plain unbounded
    # floats in the schema, so `{"type": "line", "width": Infinity}` is valid
    # scene JSON that used to take the whole screen down.
    assert pixel_width(Geometry(), width) == 1


@pytest.mark.parametrize("width", [1.0, 2.0, 1e6, 1e9, 1e17])
def test_pixel_width_clamps_a_giant_width_to_the_canvas(width: float):
    # Guarding only the non-finite case left the *finite* giants through, and
    # `round` hands those to Pillow as a Python int wider than a C long. A
    # stroke as wide as the canvas already covers it end to end from any point
    # on the shape, so the canvas size is the ceiling past which no wider number
    # can add a pixel -- the same clamp `ors_render.elements.media._box_px`
    # applies to an over-large image box.
    #
    # 1e308 is deliberately not in this list: `span` overflows it to infinity,
    # so it is the *non-finite* case above and keeps landing on the 1 px floor.
    # Both are safe degradations; which one a number gets is decided after the
    # scaling, not before it.
    geometry = Geometry()
    assert pixel_width(geometry, width) == geometry.px
    # A wide-but-drawable width is still passed through untouched.
    assert pixel_width(geometry, 0.5) == geometry.px // 2


@pytest.mark.parametrize(
    "element",
    [
        {"type": "rect", "fill": None, "stroke": "#ff0000", "stroke_width": float("nan")},
        {"type": "rect", "fill": None, "stroke": "#ff0000", "stroke_width": float("inf")},
        {"type": "line", "width": float("nan")},
        {"type": "line", "width": float("-inf")},
    ],
)
def test_a_non_finite_stroke_width_still_draws_the_element(element: dict[str, Any]):
    # Each of these draws *only* a stroke, so a blank panel would mean the width
    # had degraded to nothing. A broken width is a broken number, not a request
    # for no stroke, so it lands on the same 1 px floor a zero width does --
    # and, either way, it must not take the screen down.
    assert _render(element).getbbox() is not None


@pytest.mark.parametrize("width", [1e9, 1e17, 1e308])
@pytest.mark.parametrize(
    "element",
    [
        {"type": "line"},
        {"type": "rect", "fill": None, "stroke": "#00ff00", "radius": 0.0},
        {"type": "rect", "fill": None, "stroke": "#00ff00", "radius": 0.1},
    ],
    ids=["line", "rectangle", "rounded_rectangle"],
)
def test_a_giant_stroke_width_still_draws_the_element(element: dict[str, Any], width: float):
    # `LineElement.width` and `RectElement.stroke_width` are unbounded floats, so
    # every one of these is schema-valid scene JSON -- and every one of them used
    # to raise out of `render_scene`: `OverflowError: Python int too large to
    # convert to C long` from `ImageDraw.line`, and `OverflowError: signed
    # integer is greater than maximum` from `rectangle` at only 1e9. Both rect
    # paths are covered because `rectangle` and `rounded_rectangle` are separate
    # Pillow entry points, and an earlier audit missed the rect case entirely by
    # never setting `stroke`, which defaults to `None` and leaves `stroke_width`
    # dead. A saturating stroke, not a blank panel, is the right degradation.
    key = "width" if element["type"] == "line" else "stroke_width"
    assert _render({**element, key: width}).getbbox() is not None
