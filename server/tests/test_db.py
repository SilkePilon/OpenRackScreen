import json
import sqlite3

import pytest
from ors_server.db import SCHEMA_VERSION, Database

TABLES = {
    "daemon",
    "screen",
    "template",
    "integration",
    "secret",
    "setting",
    "daemon_event",
    "claim",
    "denied_fingerprint",
}
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


def test_the_export_redacts_the_key_a_daemon_reconnects_with(tmp_path):
    """The longer-lived of the two credentials on that row, and the same rule.

    A pairing token is spent on first use; this hash is derived from the key the
    daemon presents on every connect for the life of the pairing, so an export
    carrying it would be a file on disk that lets its reader impersonate a rack.
    """
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO daemon (name, key_hash, created_at) VALUES (?, ?, ?)",
            ("rack-pi", "5eaf00d-not-a-real-hash", "2026-08-12T09:00:00Z"),
        )
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()
    text = export.read_text()

    assert "5eaf00d" not in text, "an export is a file on disk; it does not carry a key hash"


def test_the_export_redacts_the_claim_id_and_the_granted_key(tmp_path):
    """`claim.id` is not derived from a credential -- it *is* one (design spec
    S6.3 step 4, and `claims.py`'s own comment on the `INSERT` says so): a
    daemon's poll authenticates with it directly. `claim.granted_key` is
    `secret.ciphertext`'s case, not `token_hash`'s -- sealed to the claim's
    ephemeral public key rather than plaintext -- but the same rule applies:
    encrypted still counts, and a rebuild discards it exactly as it discards
    `key_hash`.
    """
    path = tmp_path / "ors.db"
    database = Database(path)
    database.initialise()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO claim"
            " (id, hostname, address, fingerprint, short_code, version, public_key,"
            "  first_seen, granted_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "claim-id-is-the-bearer-credential",
                "rack-pi",
                "10.0.0.5",
                "fp-1",
                "AAAAAA",
                "1.0.0",
                "pk-1",
                0.0,
                "sealed-blob-not-plaintext",
            ),
        )
        connection.execute("PRAGMA user_version = 0")

    export = Database(path).initialise()
    text = export.read_text()

    assert "claim-id-is-the-bearer-credential" not in text, (
        "an export is a file on disk; it does not carry the claim's bearer credential"
    )
    assert "sealed-blob-not-plaintext" not in text, (
        "encrypted still counts -- an export does not carry the sealed key either"
    )
    record = json.loads(text)["claim"][0]
    assert record["id"] == "<redacted>"
    assert record["granted_key"] == "<redacted>"
    assert record["hostname"] == "rack-pi", "the rest of the row still exports"


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


def test_a_daemon_row_may_hold_a_token_or_a_key_but_never_both(tmp_path):
    """The two credentials are unambiguous by convention. The schema now says so.

    `pairing.py` reasons that one string can match at most one of the two
    columns, because a token's hash is deleted the moment it is spent -- and
    everything downstream rests on that, including which of the two the daemon
    socket tries first. Nothing enforced it. A row hand-written with both, or
    written by a future re-pair endpoint that sets a token without clearing the
    key, authenticates by key today and has its live key silently revoked by the
    next token claim.
    """
    database = Database(tmp_path / "ors.db")
    database.initialise()

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO daemon (name, key_hash, created_at) VALUES ('pi-rack', 'k', 'now')"
        )
        for statement in (
            "INSERT INTO daemon (name, token_hash, key_hash, created_at)"
            " VALUES ('pi-two', 't', 'k', 'now')",
            "UPDATE daemon SET token_hash = 't' WHERE name = 'pi-rack'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

        # And the states the pairing really does move through are all still
        # writable: unpaired, paired, and the re-pair task 13 owes.
        connection.execute(
            "UPDATE daemon SET key_hash = NULL, token_hash = 't2' WHERE name = 'pi-rack'"
        )
        connection.execute(
            "UPDATE daemon SET token_hash = NULL, key_hash = 'k2' WHERE name = 'pi-rack'"
        )


def test_rows_come_back_as_mappings(tmp_path):
    database = Database(tmp_path / "ors.db")
    database.initialise()
    with database.connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'UTC')")
        row = connection.execute("SELECT key, value FROM setting").fetchone()

    assert row["key"] == "timezone", "callers read columns by name, not by index"


def test_the_export_says_which_schema_produced_it(tmp_path):
    """An export is a dump of the *old* schema, and a restorer has to know which.

    The filename carries a timestamp and nothing else, and the DDL is not in the
    artifact, so without this a later restore is reading columns it cannot date.
    It cannot be retrofitted onto exports already written.
    """
    database = Database(tmp_path / "ors.db")
    database.initialise()

    assert database.export()["_schema_version"] == SCHEMA_VERSION


def test_the_export_dates_the_rows_in_it_and_not_the_code_that_wrote_it(tmp_path):
    """The number a restorer reads has to describe the columns beside it.

    Found by running the rebuild for real rather than by simulating it. The
    export is written *before* the file is dropped, so its rows are the old
    schema's -- and stamping them with the constant compiled into the running
    build says the opposite: it dates a version-2 dump as version 3, and the
    restorer decides which columns exist from that number.
    """
    path = tmp_path / "ors.db"
    Database(path).initialise()
    with Database(path).connect() as connection:
        connection.execute("INSERT INTO setting (key, value) VALUES ('timezone', 'UTC')")
        connection.execute("PRAGMA user_version = 2")

    export = Database(path).initialise()

    assert export is not None
    assert json.loads(export.read_text())["_schema_version"] == 2


def test_redaction_does_not_invent_a_column_the_old_schema_lacked(tmp_path):
    path = tmp_path / "ors.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE daemon (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO daemon (name) VALUES ('old')")

    exported = Database(path).export()["daemon"][0]

    assert exported == {"id": 1, "name": "old"}, "an export describes the database that existed"
