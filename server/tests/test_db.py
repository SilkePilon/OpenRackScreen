import json
import sqlite3

from ors_server.db import SCHEMA_VERSION, Database

TABLES = {"daemon", "screen", "template", "integration", "secret", "setting", "daemon_event"}
TABLE_NAMES = "SELECT name FROM sqlite_master WHERE type='table'"
SET_TIMEZONE = "INSERT INTO setting (key, value) VALUES ('timezone', 'Europe/Amsterdam')"
GET_TIMEZONE = "SELECT value FROM setting WHERE key='timezone'"


def test_initialise_creates_every_table_and_records_the_version(tmp_path):
    database = Database(tmp_path / "ors.db")
    assert database.initialise() is None, "a fresh database has nothing to export"

    with database.connect() as connection:
        names = {row[0] for row in connection.execute(TABLE_NAMES)}
        assert TABLES <= names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_initialise_is_idempotent_and_keeps_data(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    with database.connect() as connection:
        connection.execute(SET_TIMEZONE)

    assert database.initialise() is None
    with database.connect() as connection:
        assert connection.execute(GET_TIMEZONE).fetchone()[0] == "Europe/Amsterdam"


def test_a_stale_schema_is_exported_then_rebuilt(tmp_path):
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute(SET_TIMEZONE)
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()

    assert export is not None and export.exists()
    dumped = json.loads(export.read_text())
    assert {"key": "timezone", "value": "Europe/Amsterdam"} in dumped["setting"]
    with Database(path).connect() as connection:
        assert connection.execute("SELECT count(*) FROM setting").fetchone()[0] == 0


def test_the_export_redacts_secrets(tmp_path):
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO secret (ciphertext) VALUES ('gAAAAAB-not-a-real-token')")
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()
    text = export.read_text()

    assert "gAAAAAB" not in text, "an export is a file on disk; it does not carry ciphertext"
    assert json.loads(text)["secret"] == [{"id": 1, "ciphertext": "<redacted>"}]


def test_the_export_redacts_a_daemon_token_hash(tmp_path):
    """A hash is credential-derived, and useless here: a rebuild means re-pairing."""
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO daemon (name, token_hash, created_at) VALUES (?, ?, ?)",
            ("rack-pi", "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", "2026-08-12T09:00:00Z"),
        )
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()
    text = export.read_text()

    assert "argon2id" not in text, "an export is a file on disk; it does not carry a token hash"
    assert json.loads(text)["daemon"][0]["token_hash"] == "<redacted>"
    assert json.loads(text)["daemon"][0]["name"] == "rack-pi", "the rest of the row still exports"


def test_foreign_keys_are_enforced(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()

    with database.connect() as connection:
        try:
            connection.execute(
                "INSERT INTO screen (daemon_id, position, name, display, template, params)"
                " VALUES (999, 1, 'X', '{}', 'ring-gauge', '{}')"
            )
        except sqlite3.IntegrityError:
            return
    raise AssertionError("a screen may not belong to a daemon that does not exist")


def test_rows_come_back_as_mappings(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'UTC')")
        row = connection.execute("SELECT key, value FROM setting").fetchone()

    assert row["key"] == "timezone", "callers read columns by name, not by index"
