import json
from contextlib import closing

import pytest
from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig
from ors_schema.scene import RectElement, Scene, Template
from ors_server.db import Database
from ors_server.secrets import SecretStore, load_or_create_key
from ors_server.snapshot import (
    SnapshotError,
    build_snapshot,
    bump_config_version,
    scenes_json,
    seed_builtin_templates,
)

DISPLAY = {"backend": "virtual", "out_dir": "/tmp/p"}
PROMETHEUS = {"url": "http://p:9090", "fields": {"cpu": {"query": "up"}}}

ADD_DAEMON = "INSERT INTO daemon (name, status, created_at) VALUES (?, 'paired', '2026-01-01')"
ADD_SCREEN = (
    "INSERT INTO screen (daemon_id, position, name, display, rotation, template, params)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
ADD_INTEGRATION = (
    "INSERT INTO integration (daemon_id, type, name, config, secret_id, poll_interval, enabled)"
    " VALUES (?, 'prometheus', ?, ?, ?, 5.0, ?)"
)


def add_daemon(connection, name: str) -> int:
    return int(connection.execute(ADD_DAEMON, (name,)).lastrowid)


def add_screen(
    connection,
    daemon_id: int,
    *,
    position: int,
    name: str,
    rotation: int = 0,
    template: str = "ring-gauge",
    params: dict | None = None,
) -> int:
    cursor = connection.execute(
        ADD_SCREEN,
        (
            daemon_id,
            position,
            name,
            json.dumps(DISPLAY),
            rotation,
            template,
            json.dumps(params or {}),
        ),
    )
    return int(cursor.lastrowid)


def fixtures(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    seed_builtin_templates(database)
    secrets = SecretStore(database, load_or_create_key(tmp_path, None))
    with closing(database.connect()) as connection:
        daemon_id = add_daemon(connection, "pi-rack")
        connection.execute(
            "INSERT INTO setting (key, value) VALUES ('timezone', 'Europe/Amsterdam')"
        )
        add_screen(connection, daemon_id, position=1, name="CPU", rotation=270, params={"n": "CPU"})
    return database, secrets, daemon_id


def test_a_snapshot_is_a_valid_daemon_config(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert isinstance(snapshot, DaemonConfig)
    assert snapshot.timezone == "Europe/Amsterdam"
    assert [screen.name for screen in snapshot.screens] == ["CPU"]
    assert snapshot.screens[0].rotation == 270
    assert snapshot.screens[0].params["n"] == "CPU"
    assert snapshot.screens[0].display.out_dir == "/tmp/p"


def test_a_setting_nobody_has_written_falls_back_to_the_schemas_default(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("DELETE FROM setting")

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert snapshot.timezone == "UTC"
    assert snapshot.night == DaemonConfig().night


def test_the_night_window_travels_when_one_is_configured(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute(
            "INSERT INTO setting (key, value) VALUES ('night', ?)",
            (json.dumps({"enabled": True, "start": "22:30", "end": "06:00"}),),
        )

    assert build_snapshot(database, secrets, daemon_id).night.start == "22:30"


def test_only_this_daemons_screens_are_included(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        other = add_daemon(connection, "other")
        add_screen(connection, other, position=1, name="THEIRS")

    assert [s.name for s in build_snapshot(database, secrets, daemon_id).screens] == ["CPU"]


def test_screens_come_back_in_panel_order_however_they_were_written(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_screen(connection, daemon_id, position=4, name="NET")
        add_screen(connection, daemon_id, position=2, name="MEM")

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert [screen.name for screen in snapshot.screens] == ["CPU", "MEM", "NET"]


def test_screens_sharing_a_position_keep_a_settled_order(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_screen(connection, daemon_id, position=1, name="SECOND")
        add_screen(connection, daemon_id, position=1, name="THIRD")
        # SQLite promises no order among rows with equal sort keys, and it means
        # it: whichever index covers `position` decides the ties. This one
        # stands in for whatever gets added to the schema later -- under it, an
        # `ORDER BY position` with no tiebreak hands back the three panels
        # reversed, so the rack rewires itself on a migration nobody connected
        # to the screens.
        connection.execute("CREATE INDEX ix_screen_position ON screen(position, name DESC)")

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert [screen.name for screen in snapshot.screens] == ["CPU", "SECOND", "THIRD"]


def test_a_disabled_screen_still_travels_so_the_daemon_can_dark_it(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("UPDATE screen SET enabled = 0 WHERE name = 'CPU'")

    assert build_snapshot(database, secrets, daemon_id).screens[0].enabled is False


def test_a_screens_own_night_window_travels(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute(
            "UPDATE screen SET sleep_override = ? WHERE name = 'CPU'",
            (json.dumps({"enabled": False}),),
        )

    override = build_snapshot(database, secrets, daemon_id).screens[0].sleep_override

    assert override is not None and override.enabled is False


def test_a_daemon_with_no_screens_is_still_a_snapshot(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("DELETE FROM screen")

    assert build_snapshot(database, secrets, daemon_id).screens == []


def test_an_integration_travels_with_its_columns_overlaid_on_its_config(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute(ADD_INTEGRATION, (daemon_id, "prom", json.dumps(PROMETHEUS), None, 1))

    integration = build_snapshot(database, secrets, daemon_id).integrations[0]

    assert integration.name == "prom"
    assert integration.url == "http://p:9090"
    assert integration.poll_interval == 5.0
    assert integration.fields["cpu"].query == "up"


def test_a_disabled_integration_is_left_out(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute(ADD_INTEGRATION, (daemon_id, "off", json.dumps(PROMETHEUS), None, 0))

    assert build_snapshot(database, secrets, daemon_id).integrations == []


def test_only_this_daemons_integrations_are_included(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        other = add_daemon(connection, "other")
        connection.execute(ADD_INTEGRATION, (other, "theirs", json.dumps(PROMETHEUS), None, 1))

    assert build_snapshot(database, secrets, daemon_id).integrations == []


def test_a_credential_the_wire_format_cannot_carry_is_refused_not_dropped(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    secret_id = secrets.put("s3cret")
    with closing(database.connect()) as connection:
        connection.execute(
            ADD_INTEGRATION, (daemon_id, "prom", json.dumps(PROMETHEUS), secret_id, 1)
        )

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "prom" in str(error.value), "the message has to say which integration"
    assert "s3cret" not in str(error.value), "a credential never reaches a log line"


def test_builtin_templates_are_seeded_and_travel_in_the_snapshot(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert "ring-gauge" in snapshot.templates
    assert "system" in snapshot.templates, "the daemon needs the system scenes to show connecting"
    assert set(snapshot.templates) == set(load_builtin_templates())


def test_a_seeded_template_comes_back_equal_to_the_one_that_was_shipped(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert snapshot.templates == load_builtin_templates()


def test_a_field_that_is_nulled_rather_than_unset_survives_the_round_trip(tmp_path):
    stroke_only = Template(
        name="stroke-only",
        scenes=[Scene(elements=[RectElement(fill=None, stroke="#ffffff")])],
    )

    reloaded = Template.model_validate(
        {"name": "stroke-only", "scenes": json.loads(scenes_json(stroke_only.scenes))}
    )

    assert reloaded.scenes[0].elements[0].fill is None, "a nulled fill is not an absent fill"


def test_a_stored_scene_uses_the_names_the_shipped_json_uses(tmp_path):
    database, _, _ = fixtures(tmp_path)

    with closing(database.connect()) as connection:
        stored = connection.execute(
            "SELECT scenes FROM template WHERE name='multi-ring'"
        ).fetchone()

    assert '"as":' in stored["scenes"], "the column is what the editor loads and hands back"
    assert '"as_":' not in stored["scenes"]


def test_seeding_twice_does_not_duplicate(tmp_path):
    database, _, _ = fixtures(tmp_path)
    seed_builtin_templates(database)

    with closing(database.connect()) as connection:
        count = connection.execute(
            "SELECT count(*) FROM template WHERE name='ring-gauge'"
        ).fetchone()[0]
    assert count == 1


def test_seeding_leaves_an_edited_template_alone(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    edited = scenes_json([Scene(name="mine")])
    with closing(database.connect()) as connection:
        connection.execute("UPDATE template SET scenes = ? WHERE name = 'ring-gauge'", (edited,))

    seed_builtin_templates(database)

    templates = build_snapshot(database, secrets, daemon_id).templates
    assert [scene.name for scene in templates["ring-gauge"].scenes] == ["mine"]


def test_a_screen_naming_a_template_nobody_defined_is_refused_here(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("UPDATE screen SET template = 'gone' WHERE name = 'CPU'")

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "gone" in str(error.value) and "CPU" in str(error.value)


def test_a_disabled_screen_naming_a_missing_template_does_not_stop_the_rack(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_screen(connection, daemon_id, position=2, name="OLD", template="gone")
        connection.execute("UPDATE screen SET enabled = 0 WHERE name = 'OLD'")

    assert [s.name for s in build_snapshot(database, secrets, daemon_id).screens] == ["CPU", "OLD"]


def test_a_row_the_wire_format_refuses_names_the_daemon(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("UPDATE screen SET rotation = 45 WHERE name = 'CPU'")

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert str(daemon_id) in str(error.value)
    assert "rotation" in str(error.value.__cause__)


def test_a_snapshot_for_an_unknown_daemon_is_an_error(tmp_path):
    database, secrets, _ = fixtures(tmp_path)

    with pytest.raises(KeyError):
        build_snapshot(database, secrets, 4242)


def test_the_config_version_increases(tmp_path):
    database, _, daemon_id = fixtures(tmp_path)

    first = bump_config_version(database, daemon_id)
    second = bump_config_version(database, daemon_id)

    assert first == 1, "a daemon that has never been pushed to is at 0"
    assert second == first + 1


def test_the_config_version_is_per_daemon(tmp_path):
    database, _, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        other = add_daemon(connection, "other")

    bump_config_version(database, daemon_id)
    bump_config_version(database, daemon_id)

    assert bump_config_version(database, other) == 1, "one rack's edits are not another's"


def test_bumping_an_unknown_daemon_is_an_error(tmp_path):
    database, _, _ = fixtures(tmp_path)

    with pytest.raises(KeyError):
        bump_config_version(database, 4242)


def test_a_bump_is_durable(tmp_path):
    database, _, daemon_id = fixtures(tmp_path)

    bump_config_version(database, daemon_id)

    with closing(Database(tmp_path / "ors.db").connect()) as connection:
        stored = connection.execute(
            "SELECT config_version FROM daemon WHERE id = ?", (daemon_id,)
        ).fetchone()[0]
    assert stored == 1, "an uncommitted bump is a version the daemon never sees"
