import pytest
from ors_schema.scene import (
    GroupElement,
    RingElement,
    Scene,
    Template,
    TextElement,
)
from pydantic import ValidationError


def test_ring_element_defaults_match_spec():
    ring = RingElement(type="ring")
    assert (ring.cx, ring.cy) == (0.5, 0.5)
    assert ring.r == 0.875
    assert ring.min == 0 and ring.max == 100
    assert ring.cap == "none"
    assert ring.start_angle == -90
    assert ring.direction == "cw"


def test_scene_parses_a_mixed_element_list_into_typed_models():
    scene = Scene.model_validate(
        {
            "name": "cpu",
            "elements": [
                {"type": "ring", "value": "{{prom.cpu}}", "palette": "cyan"},
                {"type": "text", "cy": 0.517, "size": 52, "text": "{{prom.cpu | round:0}}%"},
            ],
        }
    )
    assert isinstance(scene.elements[0], RingElement)
    assert isinstance(scene.elements[1], TextElement)
    assert scene.elements[1].size == 52
    assert scene.background == "#000000"


def test_group_nests_elements_and_carries_repeat():
    group = GroupElement.model_validate(
        {
            "type": "group",
            "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
            "step": {"r": -0.125, "thickness": -0.017},
            "palettes": ["blue", "amber", "violet"],
            "elements": [{"type": "ring", "r": 0.858, "value": "{{t.progress}}"}],
        }
    )
    assert group.repeat is not None
    assert group.repeat.as_ == "t"
    assert group.repeat.limit == 3
    assert group.step["r"] == -0.125
    assert isinstance(group.elements[0], RingElement)


def test_text_carries_its_own_palette_for_the_palette_token():
    # `"color": "@palette"` on a title has to resolve against *something*, and a
    # text element's palette is its own rather than a sibling ring's: a template
    # sets the same palette on both, which keeps the reference local.
    assert TextElement(type="text").palette == "mono"
    title = TextElement.model_validate({"type": "text", "color": "@palette", "palette": "amber"})
    assert title.palette == "amber"


def test_unknown_element_type_is_rejected():
    with pytest.raises(ValidationError):
        Scene.model_validate({"elements": [{"type": "hologram"}]})


def test_scene_round_trips_through_json_with_repeat_alias():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t"},
                    "elements": [{"type": "ring", "value": "{{t.progress}}"}],
                }
            ]
        }
    )
    dumped = scene.model_dump(by_alias=True, exclude_none=True)
    assert dumped["elements"][0]["repeat"]["as"] == "t"
    assert Scene.model_validate(dumped) == scene


def _params_template() -> Template:
    return Template.model_validate(
        {
            "name": "t",
            "params_schema": {
                "title": {"type": "string", "default": "CPU"},
                "palette": {"type": "palette", "default": "cyan"},
                # A declared parameter with no default at all: `ParamSpec.default`
                # is `Any`, so `None` is what "nothing supplied" looks like.
                "hint": {"type": "string"},
            },
            "scenes": [],
        }
    )


def test_bind_params_returns_the_declared_defaults():
    assert _params_template().bind_params() == {
        "title": "CPU",
        "palette": "cyan",
        "hint": None,
    }


def test_bind_params_lets_an_override_win():
    bound = _params_template().bind_params({"title": "MEM", "hint": "peak .5"})
    assert bound["title"] == "MEM"
    assert bound["hint"] == "peak .5"
    assert bound["palette"] == "cyan", "an unmentioned param keeps its default"


def test_bind_params_keeps_an_override_the_template_never_declared():
    # Deliberate, and documented on the method: `params_schema` is what the
    # editor offers, not a filter on what a scene may read. A user-edited
    # template that draws `{{params.extra}}` without declaring it still renders,
    # and dropping the value would blank a field that works today.
    assert _params_template().bind_params({"extra": "x"})["extra"] == "x"


def test_bind_params_does_not_second_guess_an_overrides_type():
    # `ParamSpec.type` is editor metadata. Every field the renderer reads
    # degrades on its own -- an unusable number falls back, an unusable colour
    # renders white -- so coercing or dropping here would only move the decision
    # somewhere with less context.
    assert _params_template().bind_params({"title": 42, "palette": None}) == {
        "title": 42,
        "palette": None,
        "hint": None,
    }


def test_bind_params_hands_back_a_fresh_dict_every_time():
    # `load_builtin_templates` caches, so every caller shares one `Template`.
    # A result that aliased anything on it would let one screen's params leak
    # into the next frame's.
    template = _params_template()
    first = template.bind_params({"title": "MEM"})
    first["title"] = "MUTATED"
    assert template.bind_params()["title"] == "CPU"
    assert template.params_schema["title"].default == "CPU"


def test_template_declares_params_and_scenes():
    tpl = Template.model_validate(
        {
            "name": "ring-gauge",
            "builtin": True,
            "params_schema": {"title": {"type": "string", "default": "CPU"}},
            "scenes": [{"elements": [{"type": "text", "text": "{{params.title}}"}]}],
        }
    )
    assert tpl.params_schema["title"].default == "CPU"
    assert tpl.scenes[0].elements[0].text == "{{params.title}}"
