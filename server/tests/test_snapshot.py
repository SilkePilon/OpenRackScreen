import json
import threading
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
    "INSERT INTO screen (daemon_id, position, name, display, rotation, hflip, template, params)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
ADD_INTEGRATION = (
    "INSERT INTO integration (daemon_id, type, name, config, secret_id, poll_interval, enabled)"
    " VALUES (?, 'prometheus', ?, ?, ?, 5.0, ?)"
)
ADD_TEMPLATE = (
    "INSERT INTO template (name, builtin, category, scenes, params_schema)"
    " VALUES (?, 0, 'general', ?, '{}')"
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
    hflip: bool = False,
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
            int(hflip),
            template,
            json.dumps(params or {}),
        ),
    )
    return int(cursor.lastrowid)


def add_template(connection, name: str, *, scene_name: str = "mine") -> int:
    """A template the editor made, not one `ors-render` ships.

    `builtin = 0` is the whole difference, and it is what the rack reads to tell
    an override of its own built-in from the server's copy of one.
    """
    cursor = connection.execute(ADD_TEMPLATE, (name, scenes_json([Scene(name=scene_name)])))
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

    # The position travels as well as decides the order, and it is not the index
    # or the row id: it is which physical panel in the rack, so a rack whose
    # panel 3 is empty draws NET on the fourth one. Getting it wrong puts the CPU
    # gauge on the network panel, which no ordering assertion can see.
    assert [(screen.name, screen.position) for screen in snapshot.screens] == [
        ("CPU", 1),
        ("MEM", 2),
        ("NET", 4),
    ]


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
    assert [screen.position for screen in snapshot.screens] == [1, 1, 1], "the tie is real"


def test_a_disabled_screen_still_travels_so_the_daemon_can_dark_it(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("UPDATE screen SET enabled = 0 WHERE name = 'CPU'")

    assert build_snapshot(database, secrets, daemon_id).screens[0].enabled is False


def test_a_mirrored_panel_travels_mirrored_and_an_upright_one_does_not(tmp_path):
    """`hflip` is how a panel mounted behind glass, or wired with its ribbon on
    the far side, is corrected. Nothing else in the snapshot implies it, and a
    screen that ships with the wrong one renders every frame back to front."""
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_screen(connection, daemon_id, position=2, name="MIRROR", hflip=True)

    screens = {
        screen.name: screen for screen in build_snapshot(database, secrets, daemon_id).screens
    }

    assert screens["MIRROR"].hflip is True
    assert screens["CPU"].hflip is False, "an enabled panel is not thereby a mirrored one"


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


def test_the_columns_win_over_a_stale_copy_inside_the_config_json(tmp_path):
    """`config` is a blob the API writes whole, while `name`, `type` and
    `poll_interval` are columns the list view and the editor's forms edit one at
    a time. When the two disagree the columns are the truth -- an overlay that
    deferred to the blob would push a rack an integration under a name nobody
    can see, at an interval nobody set, of a type that does not validate."""
    stale = {**PROMETHEUS, "name": "old-name", "type": "qbittorrent", "poll_interval": 99.0}
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute(ADD_INTEGRATION, (daemon_id, "prom", json.dumps(stale), None, 1))

    integration = build_snapshot(database, secrets, daemon_id).integrations[0]

    assert integration.name == "prom"
    assert integration.type == "prometheus"
    assert integration.poll_interval == 5.0


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


def test_a_credential_the_wire_format_cannot_carry_is_refused_not_dropped(tmp_path, monkeypatch):
    database, secrets, daemon_id = fixtures(tmp_path)
    secret_id = secrets.put("s3cret")
    with closing(database.connect()) as connection:
        connection.execute(
            ADD_INTEGRATION, (daemon_id, "prom", json.dumps(PROMETHEUS), secret_id, 1)
        )
    # The plaintext cannot reach the message -- it is built from two columns --
    # so asserting its absence asserts nothing. What can go wrong is the
    # decryption happening at all: there is no use for the plaintext here, and a
    # credential nobody asked for is one more place it can be logged or held.
    monkeypatch.setattr(
        SecretStore, "get", lambda *args: pytest.fail("no snapshot needs a plaintext credential")
    )

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "prom" in str(error.value), "the message has to say which integration"
    assert "disab" in str(error.value), "and how to get the rack's other edits moving again"


def test_a_disabled_integration_may_hold_a_credential(tmp_path):
    """The escape hatch, on purpose: an enabled row stops every push for this
    daemon, so there has to be an edit that clears the block without deleting
    the credential -- and it has to be one the snapshot itself does not refuse."""
    database, secrets, daemon_id = fixtures(tmp_path)
    secret_id = secrets.put("s3cret")
    with closing(database.connect()) as connection:
        connection.execute(
            ADD_INTEGRATION, (daemon_id, "prom", json.dumps(PROMETHEUS), secret_id, 0)
        )

    assert build_snapshot(database, secrets, daemon_id).integrations == []


def test_disabling_the_bad_integration_unblocks_this_daemons_pushes(tmp_path):
    """The recovery sequence end to end. `Database.connect` is autocommit, so the
    route's write has landed by the time it assembles the snapshot that follows
    it: the edit that unblocks the rack is not itself blocked."""
    database, secrets, daemon_id = fixtures(tmp_path)
    secret_id = secrets.put("s3cret")
    with closing(database.connect()) as connection:
        connection.execute(
            ADD_INTEGRATION, (daemon_id, "prom", json.dumps(PROMETHEUS), secret_id, 1)
        )
        with pytest.raises(SnapshotError):
            build_snapshot(database, secrets, daemon_id)

        connection.execute("UPDATE integration SET enabled = 0 WHERE name = 'prom'")

    assert [s.name for s in build_snapshot(database, secrets, daemon_id).screens] == ["CPU"]


# --- a column holding something that is not JSON -----------------------------
#
# `json.loads` raises `JSONDecodeError`, which says `Expecting value: line 1
# column 1 (char 0)` and names no table, no column and no row -- and which
# subclasses `ValueError`, so a route catching `SnapshotError` gets a traceback
# and a route catching `ValueError` reports it as a config mistake. A column
# nothing can parse is exactly "the database holds something no daemon could be
# given", and it has to arrive saying which one.


@pytest.mark.parametrize("column", ["display", "params", "sleep_override"])
def test_a_screen_column_that_is_not_json_names_the_row(tmp_path, column):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        screen_id = connection.execute("SELECT id FROM screen WHERE name = 'CPU'").fetchone()[0]
        connection.execute(f"UPDATE screen SET {column} = 'not json' WHERE id = ?", (screen_id,))

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert not isinstance(error.value, json.JSONDecodeError), "a caller catching ValueError guesses"
    assert "screen" in str(error.value) and column in str(error.value)
    assert str(screen_id) in str(error.value), "one corrupt row among forty has to be findable"


def test_an_integration_config_that_is_not_json_names_the_row(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        row_id = connection.execute(
            ADD_INTEGRATION, (daemon_id, "prom", "{oops", None, 1)
        ).lastrowid

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "integration" in str(error.value) and "config" in str(error.value)
    assert str(row_id) in str(error.value)


def test_a_template_column_that_is_not_json_names_the_row(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("UPDATE template SET scenes = 'not json' WHERE name = 'ring-gauge'")

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "template" in str(error.value) and "scenes" in str(error.value)


def test_a_setting_that_is_not_json_names_the_key(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('night', 'not json')")

    with pytest.raises(SnapshotError) as error:
        build_snapshot(database, secrets, daemon_id)

    assert "setting" in str(error.value) and "night" in str(error.value)


def test_builtin_templates_are_seeded_and_travel_in_the_snapshot(tmp_path):
    database, secrets, daemon_id = fixtures(tmp_path)

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert "ring-gauge" in snapshot.templates
    assert "system" in snapshot.templates, "the daemon needs the system scenes to show connecting"
    assert set(snapshot.templates) == set(load_builtin_templates())


def test_a_user_defined_template_travels_with_the_builtins(tmp_path):
    """The editor exists to make these, and the rack cannot draw one it was never
    sent. `builtin` travels with it because that flag is what the daemon reads to
    tell a rack owner's override of `ring-gauge` from the server's own copy."""
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_template(connection, "mine")

    templates = build_snapshot(database, secrets, daemon_id).templates

    assert "mine" in templates, "a template only the server holds is one the rack cannot draw"
    assert templates["mine"].builtin is False
    assert [scene.name for scene in templates["mine"].scenes] == ["mine"]
    assert "ring-gauge" in templates, "and the built-ins still travel beside it"


def test_a_screen_naming_a_user_defined_template_resolves(tmp_path):
    """The check that a screen's template exists runs against the built-ins *and*
    the snapshot's own templates. Against the built-ins alone, every screen the
    editor pointed at a user template is refused and the rack gets no push at
    all."""
    database, secrets, daemon_id = fixtures(tmp_path)
    with closing(database.connect()) as connection:
        add_template(connection, "mine")
        connection.execute("UPDATE screen SET template = 'mine' WHERE name = 'CPU'")

    snapshot = build_snapshot(database, secrets, daemon_id)

    assert snapshot.screens[0].template == "mine"


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


def test_an_interrupted_seed_leaves_no_half_seeded_template_table(tmp_path, monkeypatch):
    """Seven inserts on an autocommit connection are seven transactions, and a
    server killed part-way through the first start leaves some of the built-ins
    in the table. The rack still draws -- the daemon has its own copies -- but the
    editor lists four templates and the previews it renders are drawn from rows
    the panels are not using, which is the exact divergence shipping every row
    exists to prevent. Either all seven are there or the next start seeds again.
    """
    database = Database(tmp_path / "ors.db")
    database.initialise()
    seeded: list[str] = []
    real = scenes_json

    def die_part_way(scenes):
        seeded.append("one")
        if len(seeded) > 3:
            raise RuntimeError("killed mid-seed")
        return real(scenes)

    monkeypatch.setattr("ors_server.snapshot.scenes_json", die_part_way)

    with pytest.raises(RuntimeError):
        seed_builtin_templates(database)

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT count(*) FROM template").fetchone()[0]
    assert count == 0, "a partial set of built-ins is worse than none: none seeds again"


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

    # The whole point of catching the `ValidationError` is to say more than it
    # did, not less: the audience is whoever just saved a screen, and "a
    # configuration no daemon can run" tells them nothing about which one.
    assert str(daemon_id) in str(error.value)
    assert "screens.0.rotation" in str(error.value), "the message has to name the field"
    assert "0, 90, 180 or 270" in str(error.value), "and what would have been acceptable"


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


def test_two_edits_landing_together_never_mint_the_same_version(tmp_path):
    """The property the single `RETURNING` statement exists for.

    An UPDATE followed by a SELECT reads back whatever the *last* writer left,
    because `Database.connect` is autocommit and the other edit commits between
    the two -- so two racing saves come away holding one number, push two
    different snapshots under it, and the daemon drops the second as one it has
    already applied. The rack then runs an edit nobody can see in the UI.

    Every round starts at a barrier so the writers pile up rather than queue,
    which is what makes this fail every time rather than occasionally.
    """
    database, _, daemon_id = fixtures(tmp_path)
    writers, rounds = 8, 25
    barrier = threading.Barrier(writers)
    lock = threading.Lock()
    minted: list[int] = []
    failures: list[BaseException] = []

    def bump_repeatedly() -> None:
        try:
            for _ in range(rounds):
                barrier.wait(timeout=30)
                version = bump_config_version(database, daemon_id)
                with lock:
                    minted.append(version)
        except BaseException as error:  # noqa: BLE001 - reported, not handled
            barrier.abort()
            failures.append(error)

    threads = [threading.Thread(target=bump_repeatedly) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert sorted(minted) == list(range(1, writers * rounds + 1)), (
        "every bump has to come away with its own version, and none may be skipped"
    )


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
