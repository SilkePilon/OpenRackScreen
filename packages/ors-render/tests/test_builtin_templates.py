"""The built-in templates, and the four rack screens they have to reproduce.

These goldens are M1's acceptance test: every screen the Python script on the Pi
draws today has to come back out of JSON that ships in the wheel.
"""

import pytest
from ors_render import RenderContext, render_screen, select_scene
from ors_render.templates import load_builtin_templates
from ors_schema.scene import Scene

EXPECTED = {
    "ring-gauge",
    "big-number",
    "multi-ring",
    "node-health",
    "torrent",
    "text-only",
    "system",
}

PROM = {
    "cpu": 42.4,
    "mem": 61.2,
    "mem_used_gb": 19.4,
    "mem_total_gb": 32.0,
    "cpu_hot": {"node": ".5", "value": 71.2},
    "mem_hot": {"node": ".7", "value": 78.0},
    "pods_run": 38,
    "pods_tot": 41,
    "pods_pend": 1,
    "pods_fail": 0,
    "nodes_ready": 3,
    "nodes_total": 3,
    "alerts": 0,
}
QBIT = {
    "active": [
        {"name": "alpha-release-2160p", "progress": 91.2, "eta": 1112, "speed": 4613734},
        {"name": "beta", "progress": 55.0, "eta": 3300, "speed": 1200000},
        {"name": "gamma", "progress": 20.0, "eta": 9000, "speed": 400000},
    ],
    "min_eta": 1112,
    "total_speed": 6213734,
    "count": 3,
}


def test_all_builtin_templates_load_and_validate():
    templates = load_builtin_templates()
    assert set(templates) == EXPECTED


def test_every_declared_param_has_a_label():
    for template in load_builtin_templates().values():
        for name, spec in template.params_schema.items():
            assert spec.label, f"{template.name}.{name} has no label"


@pytest.mark.parametrize(
    ("golden", "template_name", "params", "data"),
    [
        (
            "screen_cpu",
            "ring-gauge",
            {
                "title": "CPU",
                "value": "{{prom.cpu}}",
                "big": "{{prom.cpu | round:0}}%",
                "subtitle": "cluster avg",
                "palette": "cyan",
                "hint": "peak: {{prom.cpu_hot.node}} {{prom.cpu_hot.value | round:0}}%",
            },
            {"prom": PROM},
        ),
        (
            "screen_mem",
            "ring-gauge",
            {
                "title": "MEM",
                "value": "{{prom.mem}}",
                "big": "{{prom.mem | round:0}}%",
                "subtitle": "{{prom.mem_used_gb | round:1}} / {{prom.mem_total_gb | round:0}} G",
                "palette": "green",
                "hint": "peak: {{prom.mem_hot.node}} {{prom.mem_hot.value | round:0}}%",
            },
            {"prom": PROM},
        ),
        (
            "screen_pods",
            "big-number",
            {
                "title": "PODS",
                "value": "{{prom.pods_run / prom.pods_tot * 100}}",
                "big": "{{prom.pods_run}}",
                "subtitle": "/ {{prom.pods_tot}} total",
                "palette": "lime",
                "hint": "",
            },
            {"prom": PROM},
        ),
        ("screen_nodes", "node-health", {"title": "NODES"}, {"prom": PROM}),
        # The torrent view is gated on a *healthy* cluster, so its data carries
        # a healthy `prom` as well as the downloads it draws: the scene reads
        # nothing out of that namespace, but it refuses to take the screen
        # without one. See `when` in templates/builtin/torrent.json.
        ("screen_torrent", "torrent", {"title": "TORRENT"}, {"prom": PROM, "qbit": QBIT}),
    ],
)
def test_builtin_templates_reproduce_the_original_screens(
    assert_golden, golden, template_name, params, data
):
    template = load_builtin_templates()[template_name]
    ctx = RenderContext(data={**data, "params": params})
    assert_golden(render_screen(template.scenes, ctx), golden)


def test_health_template_switches_to_downloads_when_healthy_and_downloading():
    templates = load_builtin_templates()
    scenes = templates["node-health"].scenes + templates["torrent"].scenes

    healthy = RenderContext(data={"prom": PROM, "qbit": QBIT, "params": {}})
    degraded = RenderContext(
        data={"prom": {**PROM, "alerts": 2}, "qbit": {"active": [], "count": 0}, "params": {}}
    )
    assert select_scene(scenes[::-1], healthy).name == "downloads"
    assert select_scene(scenes[::-1], degraded).name == "nodes"


def test_downloads_does_not_hide_an_unhealthy_cluster():
    # The screen this pair reproduces only *becomes* the torrent view when the
    # cluster is healthy: an alert firing while something downloads has to keep
    # the node readout on the panel, or the switch silently swallows the alarm.
    templates = load_builtin_templates()
    scenes = (templates["node-health"].scenes + templates["torrent"].scenes)[::-1]
    for unhealthy in ({**PROM, "alerts": 2}, {**PROM, "nodes_ready": 2}):
        ctx = RenderContext(data={"prom": unhealthy, "qbit": QBIT, "params": {}})
        assert select_scene(scenes, ctx).name == "nodes"


@pytest.mark.parametrize(
    ("label", "data"),
    [
        # Prometheus is down or was never configured: no namespace at all.
        ("absent", {"qbit": QBIT, "params": {}}),
        # The namespace exists but every query failed.
        ("empty", {"prom": {}, "qbit": QBIT, "params": {}}),
        # The node queries failed while the alerts query answered -- the case
        # that matters, because the panel would otherwise show torrents with no
        # node readout at all, exactly when the cluster needs watching.
        ("nodes missing", {"prom": {"alerts": 0}, "qbit": QBIT, "params": {}}),
        # ...and the mirror image: nodes answered, alerts did not, so "no alerts
        # firing" is an assumption rather than a reading.
        ("alerts missing", {"prom": {"nodes_ready": 3, "nodes_total": 3}, "qbit": QBIT}),
    ],
)
def test_unknown_health_is_not_healthy_enough_for_the_torrent_view(label, data):
    """A missing health reading must not read as a healthy cluster.

    `not prom.alerts` is true for `None`, and `prom.nodes_ready ==
    prom.nodes_total` is true when *both* are `None`, so a partial or absent
    `prom` used to satisfy the gate on the strength of data nobody supplied.
    For a rack monitor the safe direction is the other one.
    """
    templates = load_builtin_templates()
    scenes = (templates["node-health"].scenes + templates["torrent"].scenes)[::-1]
    assert select_scene(scenes, RenderContext(data=data)).name == "nodes", label


@pytest.mark.parametrize(
    ("count", "probe_y"),
    [
        # The second ring's mid-radius: absent with one download, drawn with two.
        (1, 120 - 83),
        # The fourth ring's would-be mid-radius, inside the third: `limit` caps
        # the repeat at three, so a busier client never draws over the centre.
        (4, 120 - 53),
    ],
)
def test_torrent_draws_exactly_three_rings_at_most(count, probe_y):
    template = load_builtin_templates()["torrent"]
    item = {"name": "x", "progress": 50.0, "eta": 60, "speed": 1000}
    ctx = RenderContext(
        data={
            # A healthy cluster, or the scene declines the screen and the probe
            # below passes against a blank panel without proving anything.
            "prom": PROM,
            "qbit": {"active": [item] * count, "min_eta": 60, "total_speed": 1000, "count": count},
            "params": {"title": "TORRENT"},
        }
    )
    image = render_screen(template.scenes, ctx)
    assert image.convert("L").getextrema()[1] > 0, "the torrent scene did not take the screen"
    assert image.getpixel((120, probe_y)) == (0, 0, 0)


@pytest.mark.parametrize("name", sorted(EXPECTED - {"system"}))
def test_a_data_driven_template_draws_no_text_at_all_without_data(name):
    """No reading, no words: an unresolved binding must not print its own source.

    Every ring a built-in draws sits outside radius 63 of the 120 px panel, so
    nothing but text can light up the box below -- and with neither integration
    data nor parameters, every one of these templates is bindings all the way
    down. `system` is excluded because its scenes are the ones that exist to say
    something when there *is* no data, in literal text.
    """
    template = load_builtin_templates()[name]
    image = render_screen(template.scenes, RenderContext(data={"params": {}}))
    assert image.crop((80, 80, 160, 160)).convert("L").getextrema()[1] == 0


def test_a_template_left_on_its_default_params_still_renders():
    for template in load_builtin_templates().values():
        defaults = {name: spec.default for name, spec in template.params_schema.items()}
        image = render_screen(template.scenes, RenderContext(data={"params": defaults}))
        assert image.size == (240, 240)


@pytest.mark.parametrize("scene_name", ["stale", "connecting", "error", "identify"])
def test_system_scenes_render(assert_golden, scene_name):
    system = load_builtin_templates()["system"]
    scene = next(s for s in system.scenes if s.name == scene_name)
    ctx = RenderContext(data={"params": {"message": "prometheus timeout", "ordinal": "2"}})
    assert_golden(
        render_screen([scene.model_copy(update={"when": None})], ctx), f"system_{scene_name}"
    )


def test_palette_can_come_from_a_binding():
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "ring", "value": 100, "palette": "{{params.palette}}", "track": None}
            ]
        }
    )
    cyan = render_screen([scene], RenderContext(data={"params": {"palette": "cyan"}}))
    red = render_screen([scene], RenderContext(data={"params": {"palette": "red"}}))
    # 20 px down the vertical centre line is inside the default ring's stroke,
    # which spans radius 94..105 of the 120 px panel.
    assert cyan.getpixel((120, 20)) != red.getpixel((120, 20))


def test_palette_binding_resolving_to_a_non_name_degrades_to_mono():
    # A whole-string binding resolves to the *raw* value, so a screen whose
    # params hold an inline palette dict hands `resolve_palette` something it
    # cannot read. That has to render the fallback, not reach `gradient_color`
    # as a dict.
    bound = Scene.model_validate(
        {"elements": [{"type": "ring", "value": 100, "palette": "{{params.palette}}"}]}
    )
    mono = Scene.model_validate({"elements": [{"type": "ring", "value": 100, "palette": "mono"}]})
    inline = {"kind": "gradient", "stops": [{"at": 0.0, "color": "#ff0000"}]}
    image = render_screen([bound], RenderContext(data={"params": {"palette": inline}}))
    assert image.tobytes() == render_screen([mono], RenderContext()).tobytes()
