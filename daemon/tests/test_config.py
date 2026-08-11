import logging

import pytest
import yaml
from ors_daemon.config import ConfigError, load_config, resolve_screens, system_scenes

BASE = {
    "version": 1,
    "timezone": "Europe/Amsterdam",
    "integrations": [
        {
            "name": "prom",
            "type": "prometheus",
            "url": "http://p:9090",
            "fields": {"cpu": {"query": "up"}},
        }
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "ring-gauge",
            "params": {"title": "CPU", "value": "{{prom.cpu}}", "big": "{{prom.cpu | round:0}}%"},
        }
    ],
}


def write(tmp_path, config):
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def one_screen(tmp_path, **overrides):
    """`BASE` with its single screen amended -- the shape most of these tests want."""
    config = {**BASE, "screens": [{**BASE["screens"][0], **overrides}]}
    return resolve_screens(load_config(write(tmp_path, config)))[0]


def test_a_valid_file_loads(tmp_path):
    config = load_config(write(tmp_path, BASE))
    assert config.timezone == "Europe/Amsterdam"


def test_a_missing_file_reports_its_path(tmp_path):
    with pytest.raises(ConfigError, match="rack.yaml"):
        load_config(tmp_path / "rack.yaml")


def test_malformed_yaml_reports_a_config_error(tmp_path):
    path = tmp_path / "rack.yaml"
    path.write_text("screens: [unclosed")
    with pytest.raises(ConfigError):
        load_config(path)


def test_an_invalid_field_is_named_in_the_message(tmp_path):
    broken = {**BASE, "night": {"start": "25:00"}}
    with pytest.raises(ConfigError, match="night"):
        load_config(write(tmp_path, broken))


def test_a_screen_naming_an_unknown_template_is_rejected(tmp_path):
    broken = {**BASE, "screens": [{**BASE["screens"][0], "template": "nope"}]}
    with pytest.raises(ConfigError, match="nope"):
        resolve_screens(load_config(write(tmp_path, broken)))


def test_a_resolved_screen_carries_its_template_scenes_and_bound_params(tmp_path):
    screens = resolve_screens(load_config(write(tmp_path, BASE)))

    assert len(screens) == 1
    assert screens[0].scenes, "a resolved screen must carry renderable scenes"
    assert screens[0].params["title"] == "CPU"
    assert "subtitle" in screens[0].params, "template defaults must be merged in"


def test_dependencies_are_derived_from_params_and_scenes(tmp_path):
    screens = resolve_screens(load_config(write(tmp_path, BASE)))
    assert screens[0].depends_on == frozenset({"prom"})


def test_a_screen_referencing_no_integration_depends_on_nothing(tmp_path):
    static = {
        **BASE,
        "screens": [
            {
                **BASE["screens"][0],
                "template": "text-only",
                "params": {"big": "HELLO"},
            }
        ],
    }
    screens = resolve_screens(load_config(write(tmp_path, static)))
    assert screens[0].depends_on == frozenset()


def test_a_binding_naming_an_unconfigured_namespace_is_not_a_dependency(tmp_path):
    stray = {
        **BASE,
        "screens": [{**BASE["screens"][0], "params": {"big": "{{qbit.speed}}"}}],
    }
    screens = resolve_screens(load_config(write(tmp_path, stray)))
    assert screens[0].depends_on == frozenset()


def test_disabled_screens_are_dropped_and_the_rest_ordered_by_position(tmp_path):
    many = {
        **BASE,
        "screens": [
            {**BASE["screens"][0], "name": "C", "position": 3},
            {**BASE["screens"][0], "name": "A", "position": 1},
            {**BASE["screens"][0], "name": "OFF", "position": 2, "enabled": False},
        ],
    }
    names = [screen.config.name for screen in resolve_screens(load_config(write(tmp_path, many)))]
    assert names == ["A", "C"]


def test_an_inline_template_overrides_a_builtin_of_the_same_name(tmp_path):
    inline = {
        **BASE,
        "templates": {
            "ring-gauge": {
                "name": "ring-gauge",
                "scenes": [{"name": "custom", "elements": [{"type": "text", "text": "X"}]}],
            }
        },
    }
    screens = resolve_screens(load_config(write(tmp_path, inline)))
    assert screens[0].scenes[0].name == "custom"


def test_system_scenes_are_available_by_name():
    scenes = system_scenes()
    assert {"connecting", "stale", "error", "identify"} <= set(scenes)


# --- the edges the dependency scan has to get right ------------------------


def test_whitespace_and_indexing_inside_a_binding_still_name_the_namespace(tmp_path):
    screen = one_screen(
        tmp_path,
        params={"big": "{{ prom.cpu }}", "subtitle": "{{prom.active[0].name}}"},
    )
    assert screen.depends_on == frozenset({"prom"})


def test_a_namespace_merely_starting_with_a_configured_name_is_not_a_dependency(tmp_path):
    screen = one_screen(tmp_path, params={"big": "{{prometheus.cpu}}"})
    assert screen.depends_on == frozenset()


def test_a_template_scene_contributes_a_dependency_the_params_never_mention(tmp_path):
    # `node-health`'s scenes bind `{{prom.nodes_ready}}` themselves; the screen's
    # own params name nothing at all.
    screen = one_screen(tmp_path, template="node-health", params={"title": "NODES"})
    assert screen.depends_on == frozenset({"prom"})


def test_a_binding_nested_in_a_group_is_found(tmp_path):
    # The only mention of `prom` is two levels down, inside a group's repeat --
    # nowhere a scan of the top-level element fields would reach.
    nested = {
        **BASE,
        "screens": [{**BASE["screens"][0], "template": "nested", "params": {}}],
        "templates": {
            "nested": {
                "name": "nested",
                "scenes": [
                    {
                        "name": "default",
                        "elements": [
                            {
                                "type": "group",
                                "repeat": {"over": "{{prom.active}}", "as": "row"},
                                "elements": [{"type": "text", "text": "{{row.name}}"}],
                            }
                        ],
                    }
                ],
            }
        },
    }
    screen = resolve_screens(load_config(write(tmp_path, nested)))[0]
    assert screen.depends_on == frozenset({"prom"})


def test_a_namespace_named_only_in_a_when_expression_is_not_a_dependency(tmp_path):
    # A known gap, pinned so it cannot change unnoticed: the scan looks for
    # `{{namespace.` prefixes, and `when` expressions carry no braces. `torrent`'s
    # scene selection reads `prom.alerts` and `prom.nodes_ready` in its `when` and
    # nowhere else, so `prom` does not become a dependency of a torrent screen.
    config = {
        **BASE,
        "integrations": [
            *BASE["integrations"],
            {
                "name": "qbit",
                "type": "prometheus",
                "url": "http://q:9090",
                "fields": {"active": {"query": "up"}},
            },
        ],
        "screens": [{**BASE["screens"][0], "template": "torrent", "params": {}}],
    }
    screen = resolve_screens(load_config(write(tmp_path, config)))[0]
    assert "prom" not in screen.depends_on


def test_screens_sharing_a_position_keep_the_order_they_were_written_in(tmp_path):
    tied = {
        **BASE,
        "screens": [
            {**BASE["screens"][0], "name": "FIRST", "position": 2},
            {**BASE["screens"][0], "name": "SECOND", "position": 2},
        ],
    }
    names = [screen.config.name for screen in resolve_screens(load_config(write(tmp_path, tied)))]
    assert names == ["FIRST", "SECOND"]


def test_an_inline_template_shadowing_a_builtin_is_logged(tmp_path, caplog):
    inline = {
        **BASE,
        "templates": {
            "ring-gauge": {
                "name": "ring-gauge",
                "scenes": [{"name": "custom", "elements": [{"type": "text", "text": "X"}]}],
            }
        },
    }
    config = load_config(write(tmp_path, inline))
    with caplog.at_level(logging.WARNING, logger="ors_daemon.config"):
        resolve_screens(config)
    assert "ring-gauge" in caplog.text
