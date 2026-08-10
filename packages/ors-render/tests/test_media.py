"""The sparkline and image elements."""

from __future__ import annotations

import io
from typing import Any

from ors_render.context import RenderContext
from ors_render.render import render_scene
from ors_schema.scene import Scene
from PIL import Image, ImageDraw


def _asset(size: tuple[int, int] = (32, 32), color: Any = (255, 0, 0), mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _banded_asset() -> bytes:
    """A 40x10 red strip with a green band across its middle.

    Wide and short, so `cover` into a square box has to scale by the *height*
    and then keep only 10 of the 40 source columns. Those ten are the middle
    ones only if the crop is centred; the band is four columns wider on each
    side, which is enough margin that the resampler never reaches the red but
    far too little to hide a crop taken from anywhere else.
    """
    image = Image.new("RGB", (40, 10), (255, 0, 0))
    ImageDraw.Draw(image).rectangle((11, 0, 29, 9), fill=(0, 255, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render(element: dict[str, Any], ctx: RenderContext | None = None) -> Image.Image:
    return render_scene(
        Scene.model_validate({"elements": [element]}), ctx if ctx is not None else RenderContext()
    )


def test_sparkline_renders(assert_golden):
    ctx = RenderContext(data={"h": {"cpu": [10, 40, 20, 80, 35, 90, 60, 75, 30]}})
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "sparkline",
                    "values": "{{h.cpu}}",
                    "w": 0.6,
                    "h": 0.2,
                    "palette": "cyan",
                    "fill": True,
                }
            ]
        }
    )
    assert_golden(render_scene(scene, ctx), "sparkline_basic")


def test_sparkline_with_fewer_than_two_points_draws_nothing():
    ctx = RenderContext(data={"h": {"cpu": [10]}})
    scene = Scene.model_validate({"elements": [{"type": "sparkline", "values": "{{h.cpu}}"}]})
    assert render_scene(scene, ctx).getpixel((120, 120)) == (0, 0, 0)


def test_sparkline_over_something_that_is_not_a_list_draws_nothing():
    ctx = RenderContext(data={"h": {"cpu": [10, 90]}})
    blank = render_scene(Scene(), ctx)
    for values in ("{{h}}", "{{h.cpu.0}}", "not a binding", "{{ nope", ""):
        image = _render({"type": "sparkline", "values": values, "fill": True}, ctx)
        assert image.tobytes() == blank.tobytes(), values


def test_sparkline_keeps_only_usable_numbers():
    # `values` is live upstream data, so its entries are whatever arrived: text,
    # nulls, a Prometheus NaN. `bool` is a subclass of `int`, so an unguarded
    # `isinstance` check would plot `true` as 1 and drag the whole series down.
    usable = RenderContext(data={"h": {"cpu": [10, 90]}})
    mixed = RenderContext(
        data={"h": {"cpu": [10, "x", None, True, float("nan"), float("inf"), 90]}}
    )
    element = {"type": "sparkline", "values": "{{h.cpu}}", "fill": True}
    assert _render(element, mixed).tobytes() == _render(element, usable).tobytes()


def test_sparkline_with_one_usable_number_draws_nothing():
    ctx = RenderContext(data={"h": {"cpu": ["x", 42, None]}})
    assert _render({"type": "sparkline", "values": "{{h.cpu}}"}, ctx).getpixel((120, 120)) == (
        0,
        0,
        0,
    )


def test_sparkline_with_no_area_draws_nothing():
    ctx = RenderContext(data={"h": {"cpu": [10, 90]}})
    blank = render_scene(Scene(), ctx)
    for box in ({"w": 0.0}, {"h": 0.0}, {"w": -0.5}, {"h": -0.5}):
        image = _render({"type": "sparkline", "values": "{{h.cpu}}", "fill": True, **box}, ctx)
        assert image.tobytes() == blank.tobytes(), box


def test_sparkline_with_an_unusable_position_or_size_does_not_crash():
    # `cx`, `cy`, `w` and `h` are plain unbounded floats in the schema.
    ctx = RenderContext(data={"h": {"cpu": [10, 90], "wild": [1e308, -1e308]}})
    for element in (
        {"cx": float("nan")},
        {"cy": float("inf")},
        {"cx": 1e308},
        {"w": float("nan")},
        {"h": float("inf")},
        {"w": 1e308},
        {"values": "{{h.wild}}"},
    ):
        image = _render({"type": "sparkline", "values": "{{h.cpu}}", "fill": True, **element}, ctx)
        assert image.size == (240, 240), element


def test_image_draws_from_assets():
    ctx = RenderContext(data={}, assets={"logo": _asset()})
    scene = Scene.model_validate(
        {"elements": [{"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "stretch"}]}
    )
    assert render_scene(scene, ctx).getpixel((120, 120)) == (255, 0, 0)


def test_missing_asset_renders_nothing():
    ctx = RenderContext(data={}, assets={})
    scene = Scene.model_validate(
        {"elements": [{"type": "image", "src": "nope", "w": 0.5, "h": 0.5}]}
    )
    assert render_scene(scene, ctx).getpixel((120, 120)) == (0, 0, 0)


def test_image_src_is_a_binding():
    ctx = RenderContext(data={"params": {"logo": "brand"}}, assets={"brand": _asset()})
    element = {"type": "image", "src": "{{params.logo}}", "w": 0.5, "h": 0.5, "fit": "stretch"}
    assert _render(element, ctx).getpixel((120, 120)) == (255, 0, 0)


def test_stretch_fills_the_whole_box():
    # 0.5 of the panel, centred: final pixels 60..180 on both axes.
    ctx = RenderContext(assets={"logo": _asset()})
    image = _render({"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "stretch"}, ctx)
    assert image.getpixel((65, 65)) == (255, 0, 0)
    assert image.getpixel((175, 175)) == (255, 0, 0)
    assert image.getpixel((55, 55)) == (0, 0, 0)


def test_contain_keeps_the_aspect_ratio():
    # A 400x100 source into a square box shrinks to the box's width, so the
    # drawn strip is a quarter as tall as the box and leaves its top empty.
    ctx = RenderContext(assets={"logo": _asset((400, 100), (255, 0, 0))})
    image = _render({"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "contain"}, ctx)
    assert image.getpixel((120, 120)) == (255, 0, 0)
    assert image.getpixel((65, 120)) == (255, 0, 0)
    assert image.getpixel((120, 100)) == (0, 0, 0)


def test_cover_fills_the_box_with_a_centred_crop():
    # The 40x10 strip scaled to cover a square keeps only its middle ten
    # columns, which are green; an off-centre crop would bring in the red. The
    # box is final pixels 60..180, sampled a few pixels inside its corners
    # because the supersample downsample blends its outermost few with the
    # black around them.
    ctx = RenderContext(assets={"logo": _banded_asset()})
    image = _render({"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "cover"}, ctx)
    for point in ((120, 120), (65, 65), (175, 175), (65, 175)):
        assert image.getpixel(point) == (0, 255, 0), point
    assert image.getpixel((55, 55)) == (0, 0, 0)


def test_corrupt_or_truncated_asset_bytes_render_nothing():
    # Asset bytes are untrusted input from the web UI.
    whole = _asset((64, 64))
    blank = render_scene(Scene(), RenderContext())
    for blob in (b"", b"not an image at all", whole[: len(whole) // 2], whole[:8]):
        ctx = RenderContext(assets={"logo": blob})
        image = _render({"type": "image", "src": "logo", "w": 0.5, "h": 0.5}, ctx)
        assert image.tobytes() == blank.tobytes(), blob[:16]


def test_wildly_oversized_asset_renders_nothing():
    # 25 megapixels is under Pillow's own decompression-bomb threshold, so
    # nothing upstream refuses it: decoding it would cost 100 MB and a stall on
    # a panel that refreshes several times a second.
    ctx = RenderContext(assets={"logo": _asset((5000, 5000), 0, "L")})
    blank = render_scene(Scene(), RenderContext())
    image = _render({"type": "image", "src": "logo", "w": 0.5, "h": 0.5}, ctx)
    assert image.tobytes() == blank.tobytes()


def test_image_with_an_unusable_position_or_size_does_not_crash():
    ctx = RenderContext(assets={"logo": _asset()})
    for element in (
        {"cx": float("nan")},
        {"cy": float("inf")},
        {"cx": 1e308},
        {"cx": 1e30},
        {"w": float("nan")},
        {"h": float("inf")},
        {"w": 1e308},
        {"w": 0.0},
        {"h": -0.5},
        {"w": 1e5, "h": 1e5},
    ):
        for fit in ("contain", "cover", "stretch"):
            image = _render({"type": "image", "src": "logo", "fit": fit, **element}, ctx)
            assert image.size == (240, 240), (fit, element)


def test_an_extreme_aspect_ratio_does_not_blow_up_the_render():
    # `cover` scales by whichever axis needs the most, so a 4000x1 source has to
    # reach a 480 px tall box: scaling the whole source first would be a 1.9
    # gigapixel intermediate for a 240 px panel.
    ctx = RenderContext(assets={"logo": _asset((4000, 1), (255, 0, 0))})
    image = _render({"type": "image", "src": "logo", "w": 1.0, "h": 1.0, "fit": "cover"}, ctx)
    assert image.getpixel((120, 120)) == (255, 0, 0)


def test_a_transparent_asset_composites_over_the_background():
    ctx = RenderContext(assets={"logo": _asset((32, 32), (255, 0, 0, 0), "RGBA")})
    scene = Scene.model_validate(
        {
            "background": "#0000ff",
            "elements": [{"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "stretch"}],
        }
    )
    assert render_scene(scene, ctx).getpixel((120, 120)) == (0, 0, 255)
