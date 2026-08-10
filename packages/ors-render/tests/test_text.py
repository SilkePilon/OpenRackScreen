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


def test_palette_token_resolves_against_the_text_elements_own_palette():
    # A title written `"color": "@palette"` is how a template makes a label
    # track its gauge's colour; the text element carries the palette itself, so
    # a template sets the same one on the ring and on the title rather than the
    # text inheriting a sibling's.
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "text",
                    "size": 52,
                    "text": "X",
                    "color": "@palette",
                    "palette": "amber",
                }
            ]
        }
    )
    colors = {color for _count, color in render_scene(scene, _ctx()).getcolors(maxcolors=1 << 16)}
    assert (255, 145, 0) in colors, "amber's accent, `gradient_color(amber, 1.0)`"
    assert (158, 158, 158) not in colors, "not the grey accent of the `mono` fallback"


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


def test_text_with_an_unusable_position_or_size_is_skipped():
    # `cx`, `cy` and `size` are plain unbounded floats in the schema, so every
    # one of these is valid scene JSON reachable by authoring it directly --
    # and each one used to come straight out of `render_scene` as a traceback.
    # Measured before the guard: NaN raised ValueError and an infinity
    # OverflowError out of the int conversion; `cy: 1e17` reached Pillow's C
    # rasteriser and raised SystemError; `size: 1e4` tripped Pillow's own
    # decompression-bomb guard and `size: 1e5` freetype's "invalid pixel size".
    blank = render_scene(Scene(), _ctx())
    for field, value in (
        ("cx", float("nan")),
        ("cy", float("nan")),
        ("cx", float("inf")),
        ("cy", float("-inf")),
        ("cx", 1e308),
        ("cy", 1e308),
        ("cy", 1e17),
        ("cy", 1e30),
        ("size", float("nan")),
        ("size", float("inf")),
    ):
        image = render_scene(
            Scene.model_validate({"elements": [{"type": "text", "text": "X", field: value}]}),
            _ctx(),
        )
        assert image.tobytes() == blank.tobytes(), (field, value)


def test_an_absurdly_large_size_is_clamped_rather_than_skipped():
    # A size is still a size, however silly: the element keeps its position and
    # its string, so it renders at the largest size the panel can show rather
    # than vanishing. Only a size that is not a number at all is skipped.
    for size in (1e4, 1e5, 1e9, 1e300):
        image = render_scene(
            Scene.model_validate({"elements": [{"type": "text", "text": "X", "size": size}]}),
            _ctx(),
        )
        assert image.getbbox() is not None, size


def test_the_size_clamp_is_two_sided():
    # `Geometry.font_px` floors a negative size at one pixel, but it gets there
    # through a `round` that raises on the -inf that scaling -1e308 produces,
    # so the clamp has to happen first. A hugely negative size therefore
    # renders exactly as a merely negative one always did.
    def _render(size: float) -> object:
        return render_scene(
            Scene.model_validate({"elements": [{"type": "text", "text": "X", "size": size}]}),
            _ctx(),
        ).tobytes()

    assert _render(-1e308) == _render(-5.0) == _render(0.0)


def test_text_placed_off_the_panel_draws_nothing():
    blank = render_scene(Scene(), _ctx())
    for element in ({"cx": -2.0}, {"cx": 3.0}, {"cy": -2.0}, {"cy": 3.0}):
        image = render_scene(
            Scene.model_validate(
                {"elements": [{"type": "text", "text": "X", "size": 20, **element}]}
            ),
            _ctx(),
        )
        assert image.tobytes() == blank.tobytes(), element


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
