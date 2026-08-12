import logging

import pytest
import yaml
from ors_daemon.config import (
    ConfigError,
    config_fingerprint,
    load_config,
    resolve_screens,
    system_scenes,
)

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


def test_a_scene_when_expression_names_a_dependency(tmp_path):
    # `torrent`'s health gate is a bare expression -- `len(qbit.active) > 0 and
    # prom.alerts == 0 and ...` -- and `prom` appears nowhere else in that
    # template. A screen that cannot even decide whether to draw without
    # Prometheus depends on Prometheus.
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
    assert screen.depends_on == frozenset({"prom", "qbit"})


def test_an_element_when_expression_names_a_dependency(tmp_path):
    # The element's only reference to `prom` is its condition; its text is a
    # literal.
    inline = {
        **BASE,
        "screens": [{**BASE["screens"][0], "template": "gated", "params": {}}],
        "templates": {
            "gated": {
                "name": "gated",
                "scenes": [
                    {
                        "name": "default",
                        "elements": [
                            {"type": "text", "text": "UP", "when": "prom.cpu > 0"},
                        ],
                    }
                ],
            }
        },
    }
    screen = resolve_screens(load_config(write(tmp_path, inline)))[0]
    assert screen.depends_on == frozenset({"prom"})


def test_the_example_racks_arithmetic_binding_names_its_namespace(tmp_path):
    # The PODS screen of `examples/rack.yaml`, pinned because it is the config
    # the author's rack runs.
    screen = one_screen(tmp_path, params={"value": "{{prom.pods_run / prom.pods_tot * 100}}"})
    assert screen.depends_on == frozenset({"prom"})


def test_an_operand_after_a_literal_or_a_call_is_a_dependency(tmp_path):
    # An inverted gauge and a count: in neither is the namespace the first token
    # of the expression.
    screen = one_screen(
        tmp_path, params={"value": "{{100 - prom.cpu}}", "big": "{{len(prom.hosts)}}"}
    )
    assert screen.depends_on == frozenset({"prom"})


def test_every_namespace_in_one_expression_is_a_dependency(tmp_path):
    config = {
        **BASE,
        "integrations": [
            *BASE["integrations"],
            {
                "name": "qbit",
                "type": "prometheus",
                "url": "http://q:9090",
                "fields": {"speed": {"query": "up"}},
            },
        ],
        "screens": [{**BASE["screens"][0], "params": {"big": "{{prom.cpu + qbit.speed}}"}}],
    }
    screen = resolve_screens(load_config(write(tmp_path, config)))[0]
    assert screen.depends_on == frozenset({"prom", "qbit"})


def test_a_configured_name_that_is_a_prefix_of_the_reference_is_not_a_dependency(tmp_path):
    screen = one_screen(tmp_path, params={"big": "{{prometheus_extra.cpu}}"})
    assert screen.depends_on == frozenset()


def test_a_configured_name_that_extends_the_reference_is_not_a_dependency(tmp_path):
    config = {
        **BASE,
        "integrations": [
            {
                "name": "prometheus_extra",
                "type": "prometheus",
                "url": "http://p:9090",
                "fields": {"cpu": {"query": "up"}},
            }
        ],
        "screens": [{**BASE["screens"][0], "params": {"big": "{{prom.cpu}}"}}],
    }
    screen = resolve_screens(load_config(write(tmp_path, config)))[0]
    assert screen.depends_on == frozenset()


def test_an_attribute_is_not_read_as_a_namespace(tmp_path):
    # `active` is a field *of* `prom` here and also, awkwardly, the name of
    # another integration. Only the head of the chain is a namespace.
    config = {
        **BASE,
        "integrations": [
            *BASE["integrations"],
            {
                "name": "active",
                "type": "prometheus",
                "url": "http://a:9090",
                "fields": {"x": {"query": "up"}},
            },
        ],
        "screens": [{**BASE["screens"][0], "params": {"big": "{{prom.active[0].name}}"}}],
    }
    screen = resolve_screens(load_config(write(tmp_path, config)))[0]
    assert screen.depends_on == frozenset({"prom"})


def test_params_is_never_a_dependency(tmp_path):
    # Every built-in binds `{{params.*}}`, and `ring-gauge` also reads
    # `params.title` and `params.palette`. `params` is the renderer's own
    # namespace, not an integration.
    screen = one_screen(tmp_path)
    assert "params" not in screen.depends_on
    assert screen.depends_on == frozenset({"prom"})


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


# --- the fingerprint: which config, not which schema -------------------------


def test_the_fingerprint_changes_when_anything_in_the_config_does(tmp_path):
    """The field `DaemonConfig.version` cannot be: it is a `Literal[1]`, the
    version of the *schema*, so it reads the same on every rack that has ever
    validated. The status file M3 reads verbatim needs something that moves when
    the config does, or "did the Pi apply the snapshot I pushed?" has no answer.
    """
    original = load_config(write(tmp_path, BASE))
    edited = {**BASE, "screens": [{**BASE["screens"][0], "rotation": 90}]}

    assert config_fingerprint(original) != config_fingerprint(load_config(write(tmp_path, edited)))
    assert original.version == load_config(write(tmp_path, edited)).version == 1


def test_the_fingerprint_is_the_same_for_the_same_config_read_twice(tmp_path):
    """A restart that changed nothing must not read as a reconfiguration, so
    this cannot depend on dict iteration order, on a per-process hash seed, or
    on anything else that is not in the document."""
    first = load_config(write(tmp_path, BASE))
    second = load_config(write(tmp_path, BASE))

    assert config_fingerprint(first) == config_fingerprint(second)


def test_reordering_the_keys_of_a_config_is_not_a_new_config(tmp_path):
    """It is taken over the validated model in a canonical form, not over the
    file: the server holds a document and the Pi holds what it parsed, and the
    two have to agree. A comment, a reformat or a mapping written in another
    order is the same rack."""
    reordered = {
        **BASE,
        "integrations": [
            {
                "fields": {"cpu": {"query": "up"}},
                "url": "http://p:9090",
                "type": "prometheus",
                "name": "prom",
            }
        ],
    }

    assert config_fingerprint(load_config(write(tmp_path, BASE))) == config_fingerprint(
        load_config(write(tmp_path, reordered))
    )


def test_reordering_the_screens_of_a_config_is_a_new_config(tmp_path):
    """A list is not a mapping: two panels swapping places is a different rack,
    and someone who has just swapped them needs the server to notice."""
    second = {**BASE["screens"][0], "name": "MEM", "position": 2}
    forwards = {**BASE, "screens": [BASE["screens"][0], second]}
    backwards = {**BASE, "screens": [second, BASE["screens"][0]]}

    assert config_fingerprint(load_config(write(tmp_path, forwards))) != config_fingerprint(
        load_config(write(tmp_path, backwards))
    )


def test_a_fingerprint_is_short_enough_to_read_out_loud(tmp_path):
    fingerprint = config_fingerprint(load_config(write(tmp_path, BASE)))

    assert len(fingerprint) == 12
    assert all(character in "0123456789abcdef" for character in fingerprint)
