from __future__ import annotations

import subprocess
import sys

from ors_render import RenderContext, render_scene, render_screen, select_scene
from ors_render.render import expand_params
from ors_schema.scene import Scene

HEALTHY = RenderContext(
    data={
        "prom": {"nodes_ready": 3, "nodes_total": 3, "alerts": 0},
        "qbit": {"active": [{"progress": 50}]},
    }
)
DEGRADED = RenderContext(
    data={
        "prom": {"nodes_ready": 2, "nodes_total": 3, "alerts": 1},
        "qbit": {"active": []},
    }
)

SCENES = [
    Scene.model_validate(
        {
            "name": "downloads",
            "when": (
                "prom.nodes_ready == prom.nodes_total and prom.alerts == 0 and len(qbit.active) > 0"
            ),
            "elements": [{"type": "text", "size": 30, "text": "DL"}],
        }
    ),
    Scene.model_validate(
        {"name": "nodes", "elements": [{"type": "text", "size": 30, "text": "NODES"}]}
    ),
]


def test_first_matching_scene_wins() -> None:
    assert select_scene(SCENES, HEALTHY).name == "downloads"


def test_falls_through_to_the_unconditional_scene() -> None:
    assert select_scene(SCENES, DEGRADED).name == "nodes"


def test_no_match_returns_none() -> None:
    only_conditional = [Scene.model_validate({"name": "x", "when": "1 == 2", "elements": []})]
    assert select_scene(only_conditional, HEALTHY) is None


def test_broken_condition_is_treated_as_not_matching() -> None:
    scenes = [
        Scene.model_validate({"name": "broken", "when": "__import__('os')", "elements": []}),
        Scene.model_validate({"name": "ok", "elements": []}),
    ]
    assert select_scene(scenes, HEALTHY).name == "ok"


def test_an_unconditional_scene_shadows_every_scene_after_it() -> None:
    """The fallback scene must be authored last; nothing enforces that for you.

    Documented as behaviour rather than fixed, because "first match wins" is the
    whole selection rule and a template that puts its catch-all first has a bug
    in the template, not in the renderer.
    """
    scenes = [
        Scene.model_validate({"name": "fallback", "elements": []}),
        Scene.model_validate({"name": "downloads", "when": "prom.alerts == 0", "elements": []}),
    ]
    assert select_scene(scenes, HEALTHY).name == "fallback"


def test_render_screen_picks_the_right_scene() -> None:
    healthy = render_screen(SCENES, HEALTHY)
    degraded = render_screen(SCENES, DEGRADED)
    assert healthy.size == (240, 240)
    assert healthy.tobytes() != degraded.tobytes()


def test_render_screen_with_no_match_returns_a_blank_panel() -> None:
    only_conditional = [Scene.model_validate({"name": "x", "when": "1 == 2", "elements": []})]
    image = render_screen(only_conditional, HEALTHY)
    rendered = render_screen(SCENES, HEALTHY)
    assert image.size == (240, 240)
    assert image.getpixel((120, 120)) == (0, 0, 0)
    # A blank panel is handed to the same SPI push as any other frame, so it has
    # to be indistinguishable in shape from one that went through `render_scene`.
    assert (image.size, image.mode) == (rendered.size, rendered.mode)


def test_render_screen_with_no_scenes_at_all_returns_a_blank_panel() -> None:
    image = render_screen([], HEALTHY)
    assert image.size == (240, 240)
    assert image.getpixel((120, 120)) == (0, 0, 0)


def test_blank_panel_honours_size_and_supersample() -> None:
    assert render_screen([], HEALTHY, size=120, supersample=1).size == (120, 120)
    assert render_screen([], HEALTHY, size=120, supersample=4).size == (120, 120)


_REGISTRY_PROBE = """
import ors_render
from ors_schema.scene import Scene

scene = Scene.model_validate({"elements": [{"type": "text", "size": 60, "text": "X"}]})
image = ors_render.render_screen([scene], ors_render.RenderContext())
assert image.convert("L").getextrema()[1] > 0, "element registry was empty: nothing drew"
"""


def test_importing_the_package_alone_populates_the_element_registry() -> None:
    """`import ors_render` must be enough to draw.

    The registry is filled by side-effect imports in `render.py`. A public API
    that reached the element families some other way would import cleanly, then
    silently render every scene blank -- run in a subprocess because any earlier
    import in this process would have already filled the registry.
    """
    subprocess.run([sys.executable, "-c", _REGISTRY_PROBE], check=True)


def _param_ctx(params: dict[str, object]) -> RenderContext:
    return RenderContext(data={"prom": {"cpu": 42.4}, "params": params})


def _text_scene(text: str) -> Scene:
    return Scene.model_validate({"elements": [{"type": "text", "size": 40, "text": text}]})


def test_a_param_holding_a_binding_is_resolved_against_the_data() -> None:
    """`ParamSpec(type="binding")` means the *value* of the param is a binding.

    A screen supplies `big = "{{prom.cpu | round:0}}%"` for a template that draws
    `{{params.big}}`, so the field resolves to another binding and has to be
    resolved a second time. Without that, every gauge on the rack paints its own
    source text across the panel.
    """
    bound = render_screen([_text_scene("{{params.big}}")], _param_ctx({"big": "{{prom.cpu}}%"}))
    literal = render_screen([_text_scene("42.4%")], _param_ctx({}))
    assert bound.tobytes() == literal.tobytes()


def test_a_param_holding_a_binding_reaches_a_numeric_field() -> None:
    scene = Scene.model_validate(
        {"elements": [{"type": "ring", "value": "{{params.value}}", "track": None}]}
    )
    bound = render_screen([scene], _param_ctx({"value": "{{prom.cpu}}"}))
    literal = render_screen([scene], _param_ctx({"value": 42.4}))
    assert bound.tobytes() == literal.tobytes()


def test_param_expansion_runs_once_so_data_cannot_inject_a_binding() -> None:
    """Only the operator's own params are treated as bindings, never live data.

    A torrent name, a Kubernetes node label or a Prometheus label value can
    contain anything at all, `{{...}}` included. Expanding a param once means
    such a string is never evaluated; expanding repeatedly would let upstream
    data name a namespace and have it read.
    """
    ctx = RenderContext(
        data={"prom": {"cpu": 42.4, "note": "{{prom.cpu}}"}, "params": {"big": "{{prom.note}}"}}
    )
    injected = render_screen([_text_scene("{{params.big}}")], ctx)
    evaluated = render_screen([_text_scene("42.4")], _param_ctx({}))
    # The reading that binding named is never what the panel shows.
    assert injected.tobytes() != evaluated.tobytes()
    # It shows nothing at all: the param resolved to *another* binding's source,
    # and residue is blanked rather than painted across the panel. See
    # `expand_params` and `test_a_param_referring_to_another_param_draws_nothing`.
    assert injected.tobytes() == render_screen([_text_scene("")], _param_ctx({})).tobytes()
    # Data carrying braces is still drawn as the characters it is when a *scene
    # field* reads it directly -- that is one resolution, not a second pass, and
    # it is what keeps a torrent named `{{prom.cpu}}` visible instead of secretly
    # naming a reading.
    literal = render_screen([_text_scene("{{prom.note}}")], ctx)
    assert literal.convert("L").getextrema()[1] > 0
    assert literal.tobytes() != evaluated.tobytes()


def test_a_gradient_stop_holding_a_binding_renders_instead_of_raising() -> None:
    """A colour inside a *palette* is `Color` too, so it admits a binding.

    `resolve_color` sends every element colour through a parser that degrades,
    but a gradient stop is read by `gradient_color`, which never sees the data
    and used to hand its stop straight to `hex_to_rgb`. Schema-valid JSON must
    not raise out of the render path just because the colour is in a stop.
    """
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "ring",
                    "value": 100,
                    "palette": {
                        "kind": "gradient",
                        "stops": [
                            {"at": 0.0, "color": "{{params.c}}"},
                            {"at": 1.0, "color": "#ffffff"},
                        ],
                    },
                }
            ]
        }
    )
    image = render_scene(scene, RenderContext(data={"params": {}}))
    assert image.size == (240, 240)
    # The ring is still drawn -- degrading to an unreadable colour must not mean
    # degrading to nothing.
    assert image.convert("L").getextrema()[1] > 0


def test_a_gradient_stop_holding_the_palette_token_renders_instead_of_raising() -> None:
    # `@palette` predates the binding widening and was always admitted here; it
    # cannot resolve inside the palette it names, so it degrades the same way.
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "ring",
                    "value": 100,
                    "palette": {
                        "kind": "gradient",
                        "stops": [
                            {"at": 0.0, "color": "@palette"},
                            {"at": 1.0, "color": "#ffffff"},
                        ],
                    },
                }
            ]
        }
    )
    assert render_scene(scene, RenderContext()).convert("L").getextrema()[1] > 0


def test_a_param_referring_to_another_param_draws_nothing() -> None:
    """One pass, then a residue guard: never the raw source across the panel.

    Parameters are resolved against the *unexpanded* data, so a param whose
    value names another param lands on that param's own binding source. Drawing
    it is the exact failure `expand_params` exists to prevent -- 52 px of
    `{{prom.cpu}}` straight through the bezel -- so a value still holding a
    binding after its one pass is blanked instead.
    """
    ctx = RenderContext(
        data={"prom": {"cpu": 42.4}, "params": {"inner": "{{prom.cpu}}", "big": "{{params.inner}}"}}
    )
    chained = render_screen([_text_scene("{{params.big}}")], ctx)
    blank = render_screen([_text_scene("")], _param_ctx({}))
    assert chained.tobytes() == blank.tobytes()


def test_expanding_params_twice_renders_exactly_what_expanding_once_does() -> None:
    """`expand_params` is public, so a caller may pre-expand; that must be a no-op.

    Without the residue guard the second pass evaluates a binding that came out
    of *upstream data* -- here a qBittorrent torrent named `{{prom.cpu}}` -- and
    prints a reading nothing ever asked for. The guard blanks the residue after
    the first pass, so there is nothing left for a second one to evaluate.
    """
    data = {
        "prom": {"cpu": 42.4},
        "qbit": {"name": "{{prom.cpu}}"},
        "params": {"big": "{{qbit.name}}"},
    }
    ctx = RenderContext(data=data)
    scene = _text_scene("{{params.big}}")
    once = render_scene(scene, ctx)
    twice = render_scene(scene, expand_params(ctx))
    assert once.tobytes() == twice.tobytes()
    # And the reading the injected binding named is never what gets drawn.
    assert once.tobytes() != render_scene(_text_scene("42.4"), _param_ctx({})).tobytes()


def test_a_background_supplied_as_a_binding_paints_its_colour() -> None:
    # `Scene.background` is a `Color`, so the widening admits a binding there
    # too; unresolved it reached `parse_hex_color` and silently painted black.
    scene = Scene.model_validate({"background": "{{params.bg}}", "elements": []})
    literal = Scene.model_validate({"background": "#ff0000", "elements": []})
    bound = render_screen([scene], RenderContext(data={"params": {"bg": "#ff0000"}}))
    assert bound.getpixel((120, 120)) == (255, 0, 0)
    assert bound.tobytes() == render_screen([literal], RenderContext()).tobytes()


def test_an_unresolvable_background_binding_still_paints_black() -> None:
    scene = Scene.model_validate({"background": "{{params.bg}}", "elements": []})
    assert render_screen([scene], RenderContext(data={"params": {}})).getpixel((120, 120)) == (
        0,
        0,
        0,
    )
