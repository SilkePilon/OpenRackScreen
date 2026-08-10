"""The rect and line primitive elements, and the registry helpers they share."""

from __future__ import annotations

from typing import Any

import pytest
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS, register, resolve_color
from ors_render.elements.shapes import render_rect
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
    # The box spans x 60..180 inclusive. Pillow draws the outline inward from
    # each edge, so a 0.02 stroke (9 supersampled px, 4.5 after downsampling)
    # gives two bands *inside* those edges and nothing between them. The >128
    # threshold ignores the sub-pixel LANCZOS bleed either side of each band.
    lit = [x for x in range(240) if image.getpixel((x, 120))[0] > 128]
    assert lit == [60, 61, 62, 63, 176, 177, 178, 179]


def test_radius_larger_than_half_the_box_renders_a_stadium():
    # Pillow limits the radius to half the box's smallest dimension, so anything
    # past that is the same fully-rounded shape rather than a distorted box.
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
    # the shared helper as absence, which is what Pillow reads as "do not draw".
    palette = resolve_palette("mono")
    assert resolve_color(None, palette) is None
    assert resolve_color("#ff0000", palette) == (255, 0, 0)
