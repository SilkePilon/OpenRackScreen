from __future__ import annotations

from contextlib import closing

from ors_server.db import Database
from ors_server.pairing import (
    _fingerprint,
    _spend_token,
    authenticate_key,
    claim_token,
    mint_token,
)


def database(tmp_path) -> Database:
    made = Database(tmp_path / "ors.db")
    made.initialise()
    return made


def row(made: Database, daemon_id: int):
    with closing(made.connect()) as connection:
        return connection.execute("SELECT * FROM daemon WHERE id = ?", (daemon_id,)).fetchone()


def test_minting_a_token_creates_an_unpaired_daemon(tmp_path):
    made = database(tmp_path)

    daemon_id, token = mint_token(made, "pi-rack")

    assert row(made, daemon_id)["name"] == "pi-rack"
    assert row(made, daemon_id)["status"] == "unpaired"
    assert token


def test_only_the_hash_of_a_token_is_stored(tmp_path):
    """The interface shows the token once. The database must not be able to.

    Anyone who can read the file -- a backup, an export, a stolen volume -- can
    otherwise pair a daemon of their own against this rack.
    """
    made = database(tmp_path)

    daemon_id, token = mint_token(made, "pi-rack")

    assert row(made, daemon_id)["token_hash"] != token
    assert token.encode() not in (tmp_path / "ors.db").read_bytes()


def test_two_tokens_are_never_the_same(tmp_path):
    made = database(tmp_path)

    _, first = mint_token(made, "one")
    _, second = mint_token(made, "two")

    assert first != second
    assert len(first) >= 32, "a guessable token is no token at all"


def test_claiming_a_token_names_the_daemon_and_hands_back_a_key(tmp_path):
    made = database(tmp_path)
    daemon_id, token = mint_token(made, "pi-rack")

    claimed = claim_token(made, token)

    assert claimed is not None
    assert claimed[0] == daemon_id
    assert claimed[1], "a daemon with no key could only ever reconnect by name"


def test_claiming_marks_the_daemon_paired_and_spends_the_token(tmp_path):
    made = database(tmp_path)
    daemon_id, token = mint_token(made, "pi-rack")

    claim_token(made, token)

    assert row(made, daemon_id)["status"] == "paired"
    assert row(made, daemon_id)["paired_at"]
    assert row(made, daemon_id)["token_hash"] is None, "a token good twice is a shared identity"


def test_a_spent_token_claims_nothing(tmp_path):
    made = database(tmp_path)
    _, token = mint_token(made, "pi-rack")
    claim_token(made, token)

    assert claim_token(made, token) is None


def test_a_token_nobody_minted_claims_nothing(tmp_path):
    made = database(tmp_path)
    mint_token(made, "pi-rack")

    assert claim_token(made, "not-the-token") is None


def test_no_credential_at_all_claims_nothing(tmp_path):
    """An empty string is a daemon that was never configured, not a wildcard.

    Worth pinning: `token_hash` is NULL on a paired row, and a comparison that
    treated "no credential" as "matches the row with no token" would hand out
    every identity on the rack.
    """
    made = database(tmp_path)
    daemon_id, token = mint_token(made, "pi-rack")
    claim_token(made, token)

    assert claim_token(made, "") is None
    assert authenticate_key(made, "") is None
    assert row(made, daemon_id)["status"] == "paired", "and it did not disturb the row"


def test_no_credential_is_never_a_credential_whatever_the_row_holds(tmp_path):
    """The empty string is turned away before anything is compared to it.

    Without that, "no credential" is only safe as long as no row happens to hold
    the fingerprint of the empty string -- and a row is written by whatever calls
    `mint_token`, which is an HTTP endpoint another task has yet to write. This
    is the difference between a rack that cannot be entered without a credential
    and one that cannot be entered because of what is currently in a table.
    """
    made = database(tmp_path)
    # Two rows, because one holding both hashes is a state the schema now
    # refuses -- see `test_a_daemon_row_may_hold_a_token_or_a_key_but_never_both`.
    with closing(made.connect()) as connection:
        connection.execute(
            "INSERT INTO daemon (name, token_hash, created_at) VALUES (?, ?, ?)",
            ("pi-unpaired", _fingerprint(""), "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO daemon (name, key_hash, created_at) VALUES (?, ?, ?)",
            ("pi-paired", _fingerprint(""), "2026-01-01"),
        )

    assert claim_token(made, "") is None
    assert authenticate_key(made, "") is None


def test_the_key_gets_that_daemon_back_in_and_nothing_else_does(tmp_path):
    made = database(tmp_path)
    first_id, first_token = mint_token(made, "pi-one")
    second_id, second_token = mint_token(made, "pi-two")
    _, first_key = claim_token(made, first_token)
    _, second_key = claim_token(made, second_token)

    assert authenticate_key(made, first_key) == first_id
    assert authenticate_key(made, second_key) == second_id
    assert authenticate_key(made, "made-up") is None


def test_only_the_hash_of_the_key_is_stored(tmp_path):
    """The key outlives the token, so a database that leaks it leaks the rack."""
    made = database(tmp_path)
    daemon_id, token = mint_token(made, "pi-rack")

    _, key = claim_token(made, token)

    assert row(made, daemon_id)["key_hash"] != key
    assert key.encode() not in (tmp_path / "ors.db").read_bytes()


def test_a_spent_token_does_not_come_back_as_a_key(tmp_path):
    """The two credentials share a field on the wire; they must not share a life."""
    made = database(tmp_path)
    _, token = mint_token(made, "pi-rack")
    claim_token(made, token)

    assert authenticate_key(made, token) is None


def test_the_second_racer_cannot_spend_a_token_the_first_already_spent(tmp_path):
    """Whitebox, because the interleaving it guards is not reachable from outside.

    Two sockets can present the same token at once: both find the row, both
    believe they claimed it, and the rack ends up with two daemons under one
    identity -- each overwriting the other's key. The refusal has to be the
    write itself, which is one statement and therefore one SQLite transaction,
    exactly as `claim_password` refuses a second admin password.

    Calling `_spend_token` twice *is* the second racer: it stands where a
    caller stands having already read the row and decided the token matched.
    """
    made = database(tmp_path)
    daemon_id, token = mint_token(made, "pi-rack")
    stored = _fingerprint(token)

    with closing(made.connect()) as connection:
        assert _spend_token(connection, daemon_id, stored, _fingerprint("first-key")) is True
        assert _spend_token(connection, daemon_id, stored, _fingerprint("second-key")) is False

    assert authenticate_key(made, "first-key") == daemon_id
    assert authenticate_key(made, "second-key") is None
