import pytest
from ors_schema.daemon import (
    DaemonConfig,
    DisplayConfig,
    FieldSpec,
    NightWindow,
    PrometheusConfig,
    ScreenConfig,
    TunnelConfig,
)
from ors_schema.errors import first_error
from pydantic import ValidationError

MINIMAL = {
    "version": 1,
    "timezone": "Europe/Amsterdam",
    "integrations": [
        {
            "name": "prom",
            "type": "prometheus",
            "url": "http://localhost:19090",
            "fields": {"cpu": {"query": "up"}},
        }
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/panels"},
            "template": "ring-gauge",
            "params": {"title": "CPU"},
        }
    ],
}


def test_minimal_config_parses_with_documented_defaults():
    config = DaemonConfig.model_validate(MINIMAL)
    assert config.night.enabled is True
    assert (config.night.start, config.night.end) == ("23:00", "07:00")

    integration = config.integrations[0]
    assert isinstance(integration, PrometheusConfig)
    assert integration.poll_interval == 5.0
    assert integration.timeout == 4.0
    assert integration.tunnel is None
    assert integration.fields["cpu"].reduce == "scalar"

    screen = config.screens[0]
    assert screen.rotation == 0
    assert screen.hflip is False
    assert screen.enabled is True
    assert screen.sleep_override is None


def test_field_spec_carries_a_top_reduction():
    spec = FieldSpec.model_validate(
        {"query": "x", "reduce": "top", "label": "instance", "strip": "last_octet"}
    )
    assert (spec.reduce, spec.label, spec.strip) == ("top", "instance", "last_octet")


def test_tunnel_parses_and_defaults_service_to_auto():
    config = DaemonConfig.model_validate(
        {
            **MINIMAL,
            "integrations": [
                {
                    **MINIMAL["integrations"][0],
                    "tunnel": {
                        "kubeconfig": "~/k8s-monitor.yaml",
                        "namespace": "monitoring",
                        "remote_port": 9090,
                        "local_port": 19090,
                    },
                }
            ],
        }
    )
    tunnel = config.integrations[0].tunnel
    assert tunnel is not None
    assert tunnel.service == "auto"


@pytest.mark.parametrize(
    "bad",
    [
        "24:00",
        "7:00",
        "0700",
        "",
        "23:60",
        "midnight",
        # The hour is two digits or nothing: a single-digit one is not zero-padded
        # on the way in, so `9:00` has to fail rather than be read as 09:00.
        "9:00",
        # Anchoring, not searching. A pattern missing `^`/`$` -- or one whose `$`
        # means "end of line" rather than "end of string" -- accepts these.
        "23:599",
        "023:59",
        " 23:59",
        "23:59\n",
    ],
)
def test_night_window_rejects_a_time_that_is_not_hh_mm(bad):
    with pytest.raises(ValidationError):
        NightWindow(start=bad)


def test_night_window_accepts_the_boundaries_of_the_day():
    assert NightWindow(start="00:00", end="23:59").start == "00:00"


@pytest.mark.parametrize("good", ["00:00", "09:05", "19:59", "20:00", "23:59"])
def test_night_window_accepts_every_hour_band(good):
    # 19:59 and 20:00 straddle the seam between the pattern's two hour branches,
    # which is where an alternation typo hides: a pattern accepting 24:00 and one
    # rejecting 20:00 are the same off-by-one seen from either side.
    assert NightWindow(start=good).start == good


@pytest.mark.parametrize("rotation", [45, 360, -90, 1])
def test_screen_rejects_a_rotation_the_worker_cannot_apply(rotation):
    with pytest.raises(ValidationError):
        ScreenConfig.model_validate({**MINIMAL["screens"][0], "rotation": rotation})


def test_unknown_integration_type_is_rejected():
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate(
            {**MINIMAL, "integrations": [{"name": "x", "type": "influxdb", "url": "u"}]}
        )


def test_integrations_discriminate_on_type_rather_than_try_each_member():
    # `IntegrationConfig` has one member today and gains qBittorrent in M5, and a
    # one-member union is easy to write so that it only appears to discriminate:
    # a plain `PrometheusConfig` annotation would also reject an unknown `type`,
    # via the `Literal`, and pass the test above. These error tags are pydantic's
    # proof that a tagged union was built -- once there are two members, they are
    # also what keeps a malformed Prometheus block from being reported as a failed
    # attempt at every other integration in the union.
    with pytest.raises(ValidationError) as unknown:
        DaemonConfig.model_validate(
            {**MINIMAL, "integrations": [{"name": "x", "type": "influxdb", "url": "u"}]}
        )
    assert unknown.value.errors()[0]["type"] == "union_tag_invalid"

    with pytest.raises(ValidationError) as missing:
        DaemonConfig.model_validate({**MINIMAL, "integrations": [{"name": "x", "url": "u"}]})
    assert missing.value.errors()[0]["type"] == "union_tag_not_found"


def test_unknown_key_is_rejected_rather_than_ignored():
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate({**MINIMAL, "tiemzone": "UTC"})


def test_unknown_key_deep_inside_a_template_is_rejected_too():
    # The root's `extra="forbid"` says nothing about the models below it, and a
    # config's deepest reachable models are the palettes inside an inline
    # template's scenes. A typo there is exactly the one a hand-written YAML
    # makes -- `colour` for `color` -- and ignoring it drops a colour silently:
    # the panel renders, in the wrong colour, with nothing anywhere saying why.
    typo = {
        **MINIMAL,
        "templates": {
            "mine": {
                "name": "mine",
                "scenes": [
                    {
                        "elements": [
                            {
                                "type": "ring",
                                "palette": {
                                    "kind": "gradient",
                                    "stops": [{"at": 0.0, "color": "#000000", "colour": "#ffffff"}],
                                },
                            }
                        ]
                    }
                ],
            }
        },
    }
    with pytest.raises(ValidationError) as excinfo:
        DaemonConfig.model_validate(typo)
    assert any(error["type"] == "extra_forbidden" for error in excinfo.value.errors())


def test_virtual_display_requires_an_out_dir():
    with pytest.raises(ValidationError):
        DisplayConfig.model_validate({"backend": "virtual"})


GC9A01 = {"backend": "gc9a01", "spi_bus": 0, "spi_cs": 0, "dc": 6, "rst": 5, "hz": 40_000_000}


def test_a_fully_wired_gc9a01_display_needs_no_out_dir():
    # The other half of the `virtual`/`out_dir` rule: `out_dir` is the virtual
    # backend's field alone, and demanding it from a real panel would reject
    # every production screen in the spec's own example config.
    display = DisplayConfig.model_validate(GC9A01)
    assert display.out_dir is None
    assert (display.dc, display.rst) == (6, 5)


@pytest.mark.parametrize("missing", ["dc", "rst", None])
def test_gc9a01_display_requires_its_data_command_and_reset_pins(missing):
    # No board wires DC and RST to the same line, so a default of 0 for both
    # describes a panel that cannot exist -- and the mistake surfaces as an SPI
    # error on the Pi hours later instead of as a config error at load. Which
    # pins a board has is not this schema's business; that the document names
    # them at all is.
    pins = dict(GC9A01)
    for key in ["dc", "rst"] if missing is None else [missing]:
        del pins[key]
    with pytest.raises(ValidationError):
        DisplayConfig.model_validate(pins)


def test_an_integration_that_queries_nothing_is_rejected():
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(
            {"name": "prom", "url": "http://localhost:19090", "fields": {}}
        )


TUNNEL = {
    "kubeconfig": "~/k8s-monitor.yaml",
    "namespace": "monitoring",
    "remote_port": 9090,
    "local_port": 19090,
}


def test_kubeconfig_path_expands_a_home_relative_kubeconfig(monkeypatch):
    # `subprocess` does no shell expansion and client-go does not expand `~` for
    # an explicit `--kubeconfig`, so a literal `~/...` reaches kubectl, fails to
    # stat, and kills every launch forever. The spec's own example config, the
    # plan's `rack.yaml` and the test above all write it that way.
    monkeypatch.setenv("HOME", "/home/rack")
    tunnel = TunnelConfig.model_validate(TUNNEL)
    assert tunnel.kubeconfig_path == "/home/rack/k8s-monitor.yaml"


def test_kubeconfig_path_leaves_an_absolute_path_alone():
    absolute = TunnelConfig.model_validate({**TUNNEL, "kubeconfig": "/etc/rancher/k3s.yaml"})
    assert absolute.kubeconfig_path == "/etc/rancher/k3s.yaml"


def test_the_document_keeps_the_kubeconfig_as_it_was_written(monkeypatch):
    # Expansion is a fact about the machine that runs kubectl, not about the
    # document: M3's server produces the same structure it pushes down, and a
    # `~` baked into *its* home directory names a path that exists on neither
    # machine. So the field round-trips verbatim and only the derived path
    # resolves.
    monkeypatch.setenv("HOME", "/home/server")
    tunnel = TunnelConfig.model_validate(TUNNEL)
    assert tunnel.kubeconfig == "~/k8s-monitor.yaml"
    assert tunnel.model_dump()["kubeconfig"] == "~/k8s-monitor.yaml"
    assert TunnelConfig.model_validate(tunnel.model_dump()) == tunnel


def test_tunnel_rejects_an_empty_service_name():
    # `""` is neither `auto` nor a service: it discovers nothing and forwards
    # nothing, and the daemon can only report "no service to forward" forever
    # with no way to tell that from a namespace that really has none.
    with pytest.raises(ValidationError):
        TunnelConfig.model_validate({**TUNNEL, "service": ""})


@pytest.mark.parametrize("key", ["remote_port", "local_port"])
@pytest.mark.parametrize("bad", [0, -1, 65536, 70000])
def test_tunnel_rejects_a_port_outside_the_tcp_range(key, bad):
    tunnel = {
        "kubeconfig": "~/k8s-monitor.yaml",
        "namespace": "monitoring",
        "remote_port": 9090,
        "local_port": 19090,
        key: bad,
    }
    with pytest.raises(ValidationError):
        TunnelConfig.model_validate(tunnel)


@pytest.mark.parametrize("key", ["poll_interval", "timeout"])
@pytest.mark.parametrize("bad", [0, -1.5])
def test_polling_intervals_and_timeouts_must_be_positive(key, bad):
    # Zero is not a fast poll or a patient timeout -- it is a busy loop and an
    # instantly-failing request -- so it is a config mistake, not a limit case.
    with pytest.raises(ValidationError):
        PrometheusConfig.model_validate(
            {
                "name": "prom",
                "url": "http://localhost:19090",
                "fields": {"cpu": {"query": "up"}},
                key: bad,
            }
        )


@pytest.mark.parametrize("bad", [0, -3])
def test_screen_position_counts_panels_from_one(bad):
    with pytest.raises(ValidationError):
        ScreenConfig.model_validate({**MINIMAL["screens"][0], "position": bad})


@pytest.mark.parametrize("key", ["name", "template"])
def test_screen_rejects_an_empty_name_or_template(key):
    # An empty `template` resolves against no built-in and no entry of
    # `templates`, and an empty `name` labels nothing in a log or in M3's UI.
    with pytest.raises(ValidationError):
        ScreenConfig.model_validate({**MINIMAL["screens"][0], key: ""})


def test_config_round_trips_through_json():
    config = DaemonConfig.model_validate(MINIMAL)
    assert DaemonConfig.model_validate(config.model_dump(exclude_none=True)) == config


def test_first_error_names_the_field_path_and_the_reason():
    """What both ends put in front of a person: the daemon prefixes the file and
    the server prefixes the daemon, and neither knows anything about the field
    that the model did not already say."""
    with pytest.raises(ValidationError) as error:
        DaemonConfig.model_validate(
            {**MINIMAL, "screens": [{**MINIMAL["screens"][0], "rotation": 45}]}
        )

    assert first_error(error.value).startswith("screens.0.rotation: ")


def test_a_failure_about_the_whole_document_still_reads_as_a_message():
    """`loc` is empty when nothing narrower than the document is at fault, and a
    message opening with a bare colon reads as a bug in the formatting."""
    with pytest.raises(ValidationError) as error:
        DaemonConfig.model_validate([])

    assert first_error(error.value).startswith("(root): ")


def test_a_screen_carries_the_id_the_server_routes_frames_by():
    """The daemon has no other way to learn it, and frames are addressed by it.

    The server routes a frame to a browser by its own `screen.id` and refuses one
    naming a screen the sending daemon does not own, so a snapshot that did not
    carry the number would make the whole frame path unimplementable -- and
    matching by name or position is not available, because the schema makes
    neither unique.
    """
    screen = ScreenConfig.model_validate({**MINIMAL["screens"][0], "id": 42})

    assert screen.id == 42


def test_a_hand_written_screen_has_no_server_id():
    """A YAML file has never been through a server, so there is no row to name."""
    assert DaemonConfig.model_validate(MINIMAL).screens[0].id is None


def test_a_screen_id_is_a_row_id_rather_than_a_position():
    with pytest.raises(ValidationError):
        ScreenConfig.model_validate({**MINIMAL["screens"][0], "id": 0})


def test_two_screens_may_not_carry_the_same_server_id():
    """Unlike `name` and `position`, which the schema deliberately leaves alone.

    An id is what a frame is addressed by at both ends, so two screens sharing
    one is not an untidy document but a rack that cannot be watched: they
    compete for one entry in the daemon's rate-limiter table, the loser's offer
    is refused inside the interval the winner just claimed, and it is not even
    counted as a drop. The browser then shows two different panels alternating
    under one screen, and nothing anywhere says why.

    Checked here and not in the daemon because the daemon is not the only
    producer -- a hand-written YAML can set the field, which is the whole reason
    this is reachable -- and a rule about the *set* of screens has nowhere else
    to live.
    """
    with pytest.raises(ValidationError) as error:
        DaemonConfig.model_validate(
            {
                **MINIMAL,
                "screens": [
                    {**MINIMAL["screens"][0], "id": 5, "position": 1},
                    {**MINIMAL["screens"][0], "id": 5, "position": 2},
                ],
            }
        )

    assert "5" in first_error(error.value)


def test_screens_with_no_server_id_do_not_collide_with_each_other():
    """None is "no server has ever named this screen", not a value to be unique.

    Which is every screen of a hand-written rack, so reading the absence as a
    duplicate would refuse the ordinary M2 config outright.
    """
    config = DaemonConfig.model_validate(
        {
            **MINIMAL,
            "screens": [
                {**MINIMAL["screens"][0], "position": 1},
                {**MINIMAL["screens"][0], "position": 2},
            ],
        }
    )

    assert [screen.id for screen in config.screens] == [None, None]
