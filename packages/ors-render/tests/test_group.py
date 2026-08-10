"""The group element: nesting, `when`, `repeat`, `step` and palette cycling."""

from __future__ import annotations

from typing import Any

from ors_render.context import RenderContext
from ors_render.elements.group import _stepped
from ors_render.render import render_scene
from ors_schema.scene import RectElement, RingElement, Scene, TextElement
from PIL import Image

CTX = RenderContext(
    data={
        "qbit": {
            "active": [
                {"name": "alpha", "progress": 91.0},
                {"name": "beta", "progress": 55.0},
                {"name": "gamma", "progress": 20.0},
                {"name": "delta", "progress": 5.0},
            ]
        }
    }
)


def _render(*elements: dict[str, Any]) -> Image.Image:
    return render_scene(Scene.model_validate({"elements": list(elements)}), CTX)


def test_group_renders_nested_elements():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (255, 0, 0)


def test_group_when_false_skips_all_children():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "when": "len(qbit.active) > 99",
                    "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (0, 0, 0)


def test_repeat_honours_limit_and_exposes_alias_and_index(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
                    "step": {"r": -0.125, "thickness": -0.017},
                    "palettes": ["blue", "orange", "violet"],
                    "elements": [
                        {
                            "type": "ring",
                            "r": 0.858,
                            "thickness": 0.083,
                            "value": "{{t.progress}}",
                            "track": "#16181e",
                        }
                    ],
                }
            ]
        }
    )
    assert_golden(render_scene(scene, CTX), "group_repeat_rings")


def test_repeat_over_empty_list_draws_nothing():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.nothing}}", "as": "t"},
                    "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (0, 0, 0)


def test_index_is_available_inside_the_repeat():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 2},
                    "elements": [
                        {
                            "type": "rect",
                            "cy": 0.5,
                            "w": 0.5,
                            "h": 0.5,
                            "fill": "#ff0000",
                            "when": "index == 1",
                        }
                    ],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (255, 0, 0)


def test_repeat_stops_at_limit_and_runs_the_whole_list_when_shorter():
    # `qbit.active` holds four items, so a limit of 3 must never reach index 3
    # while a limit of 4 -- exactly the list length -- and a limit of 8 both
    # must. Drawn-or-not on the last index is the cheapest observable that
    # separates a correct slice from one that is off by one at either end.
    child = {"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000", "when": "index == 3"}

    def group(limit: int) -> dict[str, Any]:
        return {
            "type": "group",
            "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": limit},
            "elements": [child],
        }

    assert _render(group(3)).getpixel((120, 120)) == (0, 0, 0)
    assert _render(group(4)).getpixel((120, 120)) == (255, 0, 0)
    assert _render(group(8)).getpixel((120, 120)) == (255, 0, 0)


def test_repeat_over_something_that_is_not_a_list_draws_nothing():
    # `resolve_list` answers `[]` for a mapping, a number, a bare string and a
    # malformed binding alike, so none of these may draw -- and none may raise.
    child = {"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}
    for over in ("{{qbit}}", "{{qbit.active.0.progress}}", "not a binding", "{{ nope"):
        element = {"type": "group", "repeat": {"over": over, "as": "t"}, "elements": [child]}
        assert _render(element).getpixel((120, 120)) == (0, 0, 0), over


def test_alias_and_index_do_not_leak_out_of_the_group():
    # `RenderContext.child` layers a *new* context over the parent's data rather
    # than mutating it, so a sibling drawn after the repeat sees neither name.
    group = {
        "type": "group",
        "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 2},
        "elements": [{"type": "rect", "w": 0.1, "h": 0.1, "cy": 0.1, "fill": "#00ff00"}],
    }
    for condition in ("index == 0", "t.progress > 0"):
        sibling = {"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000", "when": condition}
        assert _render(group, sibling).getpixel((120, 120)) == (0, 0, 0), condition


def test_nested_groups_compose_repeat_step_and_when():
    # The inner group is itself a child of the outer repeat, so its own children
    # see *both* aliases. `step` reaches direct children only, so the inner
    # `cy` step is what moves the rect -- an outer step on the inner group's own
    # `cx` would go nowhere, a group having no geometry of its own.
    scene = {
        "type": "group",
        "repeat": {"over": "{{qbit.active}}", "as": "outer", "limit": 2},
        "elements": [
            {
                "type": "group",
                "repeat": {"over": "{{qbit.active}}", "as": "inner", "limit": 2},
                "step": {"cy": 0.5},
                "elements": [
                    {
                        "type": "rect",
                        "cy": 0.25,
                        "w": 0.2,
                        "h": 0.2,
                        "fill": "#ff0000",
                        "when": "outer.progress > 50 and inner.progress > 50",
                    }
                ],
            }
        ],
    }
    image = _render(scene)
    assert image.getpixel((120, 60)) == (255, 0, 0)
    assert image.getpixel((120, 180)) == (255, 0, 0)
    assert image.getpixel((60, 60)) == (0, 0, 0)


def test_deeply_nested_groups_render_without_blowing_the_stack():
    # A scene is untrusted JSON from a web UI, so nesting depth is an input.
    # pydantic-core's own recursion guard refuses to build anything deeper than
    # this, which is what bounds the renderer's recursion -- see the note in
    # `ors_render.elements.group`.
    node: dict[str, Any] = {"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}
    for _ in range(254):
        node = {"type": "group", "elements": [node]}
    assert _render(node).getpixel((120, 120)) == (255, 0, 0)


def test_palette_cycling_wraps_and_replaces_the_child_palette():
    # Two palettes over three iterations: blue, orange, blue. Each ring is drawn
    # empty so only its `@palette` track shows, and each step shrinks it enough
    # that the three tracks can be sampled independently.
    scene = {
        "type": "group",
        "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
        "step": {"r": -0.2},
        "palettes": ["blue", "orange"],
        "elements": [{"type": "ring", "r": 0.9, "thickness": 0.1, "value": 0, "track": "@palette"}],
    }
    image = _render(scene)
    blue, orange = (0, 145, 234), (230, 81, 0)
    # Radii 0.9/0.7/0.5 of the panel radius are 108/84/60 px, each stroked 12 px
    # inward, so these sample the middle of each track along the horizontal.
    assert image.getpixel((222, 120)) == blue
    assert image.getpixel((198, 120)) == orange
    assert image.getpixel((174, 120)) == blue


def test_palette_cycling_leaves_children_without_a_palette_alone():
    # A rect has no `palette` field, so the cycle must not graft one on:
    # `draw_element` reads `getattr(element, "palette", None)`, so an injected
    # attribute would silently repaint every `@palette` fill in the group.
    scene = {
        "type": "group",
        "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 1},
        "palettes": ["red"],
        "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "@palette"}],
    }
    # `mono`'s accent, not `red`'s.
    assert _render(scene).getpixel((120, 120)) == (158, 158, 158)


def test_step_offsets_numeric_fields_by_delta_times_index():
    child = RingElement(r=0.858, thickness=0.083)
    assert _stepped(child, {"r": -0.125}, 0, None) == child
    stepped = _stepped(child, {"r": -0.125}, 2, None)
    assert stepped.r == 0.858 - 0.25
    assert stepped.thickness == 0.083


def test_step_never_mutates_the_element_it_was_given():
    # Children come from the parsed scene and are re-rendered several times a
    # second: an in-place `+=` would shrink the ring a little on every frame.
    child = RingElement(r=0.858)
    for index in range(1, 4):
        _stepped(child, {"r": -0.125}, index, "blue")
    assert child.r == 0.858
    assert child.palette == "cyan"


def test_step_ignores_fields_the_child_does_not_have_or_cannot_offset():
    # `bool` is a subclass of `int`, so an unguarded `isinstance(..., int)`
    # would turn `ellipsis: true` into `2` -- or, at a delta of -1, into a falsy
    # `0` that silently changes how the label truncates.
    child = TextElement(text="hello", size=13, ellipsis=True)
    stepped = _stepped(child, {"ellipsis": -1, "text": 1, "nonesuch": 1, "size": 2}, 1, None)
    assert stepped.ellipsis is True
    assert stepped.text == "hello"
    assert stepped.size == 15
    assert not hasattr(stepped, "nonesuch")


def test_step_drops_an_offset_that_comes_out_non_finite():
    # `step` is a `dict[str, float]`, so a NaN delta is schema-valid -- and even
    # two finite ones overflow. A group is the one thing here that can invent a
    # number no scene wrote, and `elements/text.py` converts `cx` to an int
    # without a guard, so an unchecked offset is a crashed screen.
    child = TextElement(text="hi", cx=0.5)
    assert _stepped(child, {"cx": float("nan")}, 1, None).cx == 0.5
    assert _stepped(child, {"cx": float("inf")}, 1, None).cx == 0.5
    assert _stepped(child, {"cx": 1e308}, 2, None).cx == 0.5
    # An index of 0 zeroes any delta, so even a NaN one leaves a usable value.
    assert _stepped(child, {"cx": float("nan")}, 0, None).cx == 0.5


def test_a_step_that_would_go_non_finite_does_not_crash_the_render():
    scene = {
        "type": "group",
        "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
        "step": {"cx": float("nan"), "cy": float("inf"), "size": float("-inf")},
        "elements": [{"type": "text", "text": "{{t.name}}", "size": 20}],
    }
    # Every label stays where the scene put it, so they stack up as one blob in
    # the middle of the panel -- ugly, but on screen and not a traceback. (A
    # *finite* but absurd coordinate, `cy: 1e308`, still overflows inside
    # `elements/text.py`; that is reachable by authoring one directly, with no
    # group involved, and is not something `step` can be made to prevent.)
    assert _render(scene).getbbox() is not None


def test_a_child_with_nothing_to_apply_is_passed_straight_through():
    child = RectElement(fill="#ff0000")
    assert _stepped(child, {}, 3, None) is child
    # A rect has no palette, so an override has nothing to apply either and the
    # child is reused rather than needlessly copied once per item per frame.
    assert _stepped(child, {}, 3, "blue") is child


def test_repeating_the_same_scene_renders_identically_every_frame():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
                    "step": {"r": -0.125, "thickness": -0.017},
                    "palettes": ["blue", "orange", "violet"],
                    "elements": [{"type": "ring", "r": 0.858, "value": "{{t.progress}}"}],
                }
            ]
        }
    )
    first = render_scene(scene, CTX)
    for _ in range(3):
        assert render_scene(scene, CTX).tobytes() == first.tobytes()
