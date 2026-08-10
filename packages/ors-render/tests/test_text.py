"""Canvas, the element registry entry point, and the text element."""

from __future__ import annotations

from ors_render.context import RenderContext
from ors_render.render import render_scene
from ors_schema.scene import Scene


def _ctx() -> RenderContext:
    return RenderContext(data={"prom": {"cpu": 42.4}, "params": {"title": "CPU"}})


def test_render_scene_returns_a_240_rgb_image():
    image = render_scene(Scene(), _ctx())
    assert image.size == (240, 240)
    assert image.mode == "RGB"


def test_background_is_honoured():
    image = render_scene(Scene(background="#101010"), _ctx())
    assert image.getpixel((0, 0)) == (16, 16, 16)


def test_palette_token_background_degrades_to_black():
    # `@palette` is schema-valid for any Color, including the scene background,
    # where there is no element palette to resolve it against. It must fall back,
    # not blow up `hex_to_rgb`.
    image = render_scene(Scene(background="@palette"), _ctx())
    assert image.getpixel((0, 0)) == (0, 0, 0)


def test_text_element_renders_centered(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "text", "cy": 0.5, "size": 52, "text": "{{prom.cpu | round:0}}%"},
                {
                    "type": "text",
                    "cy": 0.28,
                    "size": 15,
                    "text": "{{params.title}}",
                    "color": "#00e5ff",
                },
            ]
        }
    )
    assert_golden(render_scene(scene, _ctx()), "text_basic")


def test_element_when_false_is_skipped():
    shown = render_scene(
        Scene.model_validate({"elements": [{"type": "text", "size": 52, "text": "X"}]}), _ctx()
    )
    hidden = render_scene(
        Scene.model_validate(
            {"elements": [{"type": "text", "size": 52, "text": "X", "when": "prom.cpu > 100"}]}
        ),
        _ctx(),
    )
    assert shown.tobytes() != hidden.tobytes()
    assert hidden.getpixel((120, 120)) == (0, 0, 0)


def test_element_with_malformed_when_is_skipped():
    blank = render_scene(Scene(), _ctx())
    image = render_scene(
        Scene.model_validate(
            {"elements": [{"type": "text", "size": 52, "text": "X", "when": "prom.cpu >>> 100"}]}
        ),
        _ctx(),
    )
    assert image.tobytes() == blank.tobytes()


def test_text_truncates_to_max_width(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "text",
                    "size": 20,
                    "text": "an extremely long torrent name",
                    "max_width": 0.8,
                }
            ]
        }
    )
    assert_golden(render_scene(scene, _ctx()), "text_truncated")


def test_text_that_already_fits_is_left_alone():
    limited = Scene.model_validate(
        {"elements": [{"type": "text", "size": 20, "text": "ok", "max_width": 0.8}]}
    )
    unlimited = Scene.model_validate({"elements": [{"type": "text", "size": 20, "text": "ok"}]})
    assert render_scene(limited, _ctx()).tobytes() == render_scene(unlimited, _ctx()).tobytes()


def test_max_width_narrower_than_one_glyph_renders_nothing():
    blank = render_scene(Scene(), _ctx())
    image = render_scene(
        Scene.model_validate(
            {"elements": [{"type": "text", "size": 20, "text": "wide", "max_width": 0.001}]}
        ),
        _ctx(),
    )
    assert image.tobytes() == blank.tobytes()


def test_multiline_text_with_max_width_does_not_crash():
    # Pillow's `textlength` raises ValueError on text containing a newline, so
    # the truncation loop must not be handed one.
    image = render_scene(
        Scene.model_validate(
            {"elements": [{"type": "text", "size": 20, "text": "one\ntwo", "max_width": 0.5}]}
        ),
        _ctx(),
    )
    assert image.size == (240, 240)


def test_empty_text_draws_nothing():
    blank = render_scene(Scene(), _ctx())
    image = render_scene(
        Scene.model_validate({"elements": [{"type": "text", "size": 52, "text": "{{missing}}"}]}),
        _ctx(),
    )
    assert image.tobytes() == blank.tobytes()


def test_left_and_right_aligned_text_land_on_opposite_sides():
    def _render(align: str) -> object:
        return render_scene(
            Scene.model_validate(
                {
                    "elements": [
                        {"type": "text", "cx": 0.5, "size": 30, "text": "AB", "align": align}
                    ]
                }
            ),
            _ctx(),
        )

    left, right = _render("left"), _render("right")
    assert left.getpixel((130, 120)) != (0, 0, 0)
    assert left.getpixel((105, 120)) == (0, 0, 0)
    assert right.getpixel((110, 120)) != (0, 0, 0)
    assert right.getpixel((135, 120)) == (0, 0, 0)
