import pytest
from ors_schema.daemon import (
    DaemonConfig,
    DisplayConfig,
    FieldSpec,
    NightWindow,
    PrometheusConfig,
    ScreenConfig,
)
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


def test_virtual_display_requires_an_out_dir():
    with pytest.raises(ValidationError):
        DisplayConfig.model_validate({"backend": "virtual"})


def test_config_round_trips_through_json():
    config = DaemonConfig.model_validate(MINIMAL)
    assert DaemonConfig.model_validate(config.model_dump(exclude_none=True)) == config
