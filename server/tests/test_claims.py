from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import pytest
from ors_server.claims import (
    CLAIM_LIFETIME_S,
    DENY_SUPPRESSION_S,
    MAX_PENDING,
    approve,
    count_pending,
    deny,
    file_claim,
    get_claim,
    list_pending,
)
from ors_server.db import Database
from ors_server.pairing import authenticate_key


def database(tmp_path) -> Database:
    made = Database(tmp_path / "ors.db")
    made.initialise()
    return made


def daemon_row(made: Database, daemon_id: int):
    with closing(made.connect()) as connection:
        return connection.execute("SELECT * FROM daemon WHERE id = ?", (daemon_id,)).fetchone()


def claim_row(made: Database, claim_id: str):
    with closing(made.connect()) as connection:
        return connection.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()


def file_one(made: Database, *, n: int = 1, now: float = 0.0):
    """One claim, fields chosen so no two coincide and none matches `n` itself."""
    return file_claim(
        made,
        hostname=f"rack-host-{n + 900}",
        address=f"10.4.{(n * 7) % 256}.{(n * 13) % 256}",
        fingerprint=f"fp-{n * 31 + 5:06x}",
        short_code=f"CODE{n:02d}",
        version=f"0.{n}.3",
        public_key=f"pk-{n * 17 + 3:06x}",
        now=now,
    )


def test_a_filed_claim_appears_in_the_pending_list(tmp_path):
    made = database(tmp_path)

    filed = file_one(made, n=1, now=100.0)

    assert filed is not None
    pending = list_pending(made, now=100.0)
    assert [c.id for c in pending] == [filed.id]
    assert filed.hostname == "rack-host-901"
    assert filed.address == "10.4.7.13"
    assert filed.fingerprint == "fp-000024"
    assert filed.short_code == "CODE01"
    assert filed.version == "0.1.3"
    assert filed.public_key == "pk-000014"
    assert filed.first_seen == 100.0


def test_the_claim_id_is_a_256_bit_urlsafe_token_not_a_uuid(tmp_path):
    """Design spec S6.3 step 4: the claim id itself is the bearer credential a
    daemon's poll authenticates with, and pins the generator -- a 256-bit
    `secrets.token_urlsafe(32)` -- exactly because a `uuid4` reads as a
    harmless, non-secret row id to any future maintainer and is only 122 bits
    with 6 of them fixed, nowhere near what a bearer credential needs."""
    made = database(tmp_path)

    filed = file_one(made, n=1, now=0.0)

    assert filed is not None
    # `token_urlsafe(32)` base64url-encodes 32 random bytes with no padding:
    # 43 characters from the URL-safe alphabet. A `uuid4().hex` would be
    # exactly 32 lowercase-hex characters -- both the length and the shape
    # below rule that out.
    assert len(filed.id) == 43
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", filed.id)
    assert not re.fullmatch(r"[0-9a-f]{32}", filed.id)


def test_the_same_fingerprint_filed_twice_yields_one_pending_claim_and_the_newer_wins(tmp_path):
    """Address, version and public key -- what changes when a rack moves,
    upgrades, or regenerates its ephemeral keypair on a retry -- win.

    Hostname and short code are tied to the rack's stable identity and are
    left as they were first filed; `first_seen` does not move either, since
    it answers "how long has this rack been trying" and a retry that reset
    its own clock would make an ignored rack look brand new forever.
    """
    made = database(tmp_path)
    first = file_claim(
        made,
        hostname="original-host",
        address="10.0.0.5",
        fingerprint="shared-fp",
        short_code="AAAAAA",
        version="1.0.0",
        public_key="original-key",
        now=50.0,
    )
    assert first is not None

    second = file_claim(
        made,
        hostname="renamed-host",
        address="10.0.0.99",
        fingerprint="shared-fp",
        short_code="BBBBBB",
        version="1.2.0",
        public_key="new-ephemeral-key",
        now=75.0,
    )

    assert second is not None
    assert second.id == first.id
    pending = list_pending(made, now=75.0)
    assert len(pending) == 1
    assert pending[0].address == "10.0.0.99"
    assert pending[0].version == "1.2.0"
    assert pending[0].hostname == "original-host"
    assert pending[0].short_code == "AAAAAA"
    assert pending[0].public_key == "new-ephemeral-key"
    assert pending[0].first_seen == 50.0


def test_max_pending_is_enforced_and_refuses_the_newest_leaving_the_existing_untouched(tmp_path):
    """A cap that evicted the oldest would let a flood hide a real rack behind it.

    32 distinct claims fill the queue; the 33rd -- a new fingerprint -- is
    refused, and every one of the first 32 is still there afterwards, in the
    same order, none of them dropped to make room.
    """
    made = database(tmp_path)
    filed = [file_one(made, n=i, now=float(i)) for i in range(1, MAX_PENDING + 1)]
    assert all(c is not None for c in filed)
    assert count_pending(made, now=1000.0) == MAX_PENDING

    refused = file_one(made, n=999, now=1000.0)

    assert refused is None
    assert count_pending(made, now=1000.0) == MAX_PENDING
    still_pending = [c.id for c in list_pending(made, now=1000.0)]
    assert still_pending == [c.id for c in filed], "the admin's queue view renders this order"


def test_a_claim_older_than_its_lifetime_is_absent_from_list_pending(tmp_path):
    made = database(tmp_path)
    filed = file_one(made, n=2, now=0.0)
    assert filed is not None

    just_under = CLAIM_LIFETIME_S - 0.001
    assert list_pending(made, now=just_under) != []

    at_lifetime = CLAIM_LIFETIME_S
    assert list_pending(made, now=at_lifetime) == []


def test_a_claim_older_than_its_lifetime_is_absent_from_get_claim(tmp_path):
    """No prior `list_pending` call in this test: `get_claim` must expire the
    row itself, not ride on some other function's `_expire` having already
    swept it -- `get_claim` is exactly the function a daemon's poll calls, so
    this is the property that makes an expired claim actually unpollable."""
    made = database(tmp_path)
    filed = file_one(made, n=2, now=0.0)
    assert filed is not None

    just_under = CLAIM_LIFETIME_S - 0.001
    assert get_claim(made, filed.id, now=just_under) is not None

    at_lifetime = CLAIM_LIFETIME_S
    assert get_claim(made, filed.id, now=at_lifetime) is None


def test_a_claim_older_than_its_lifetime_is_absent_from_count_pending(tmp_path):
    """No prior `list_pending` or `get_claim` call: same reasoning as the
    `get_claim`-only test above, applied to `count_pending`."""
    made = database(tmp_path)
    filed = file_one(made, n=2, now=0.0)
    assert filed is not None

    assert count_pending(made, now=CLAIM_LIFETIME_S - 0.001) == 1
    assert count_pending(made, now=CLAIM_LIFETIME_S) == 0


def test_expiry_frees_a_slot_for_a_new_claim(tmp_path):
    made = database(tmp_path)
    for i in range(1, MAX_PENDING + 1):
        assert file_one(made, n=i, now=0.0) is not None
    assert count_pending(made, now=0.0) == MAX_PENDING

    past_lifetime = CLAIM_LIFETIME_S + 1.0
    accepted = file_one(made, n=999, now=past_lifetime)

    assert accepted is not None
    assert count_pending(made, now=past_lifetime) == 1
    assert [c.id for c in list_pending(made, now=past_lifetime)] == [accepted.id]


def test_a_granted_claim_frees_its_slot_for_the_cap_and_is_hidden_from_the_queue(tmp_path):
    """`MAX_PENDING` bounds the *pending* queue (design spec S6.5), not the
    total row count -- a granted row awaiting its daemon's poll must neither
    block a new filing at the cap nor show up in `list_pending`/
    `count_pending`, both of which the admin's queue view relies on."""
    made = database(tmp_path)
    filed = [file_one(made, n=i, now=0.0) for i in range(1, MAX_PENDING + 1)]
    assert all(c is not None for c in filed)
    approved = approve(made, filed[0].id, now=0.0)
    assert approved is not None
    assert count_pending(made, now=0.0) == MAX_PENDING - 1

    accepted = file_one(made, n=999, now=0.0)

    assert accepted is not None
    assert count_pending(made, now=0.0) == MAX_PENDING
    pending_ids = {c.id for c in list_pending(made, now=0.0)}
    assert filed[0].id not in pending_ids
    assert accepted.id in pending_ids
    assert len(pending_ids) == MAX_PENDING


def test_approve_returns_a_daemon_id_and_a_key_and_creates_the_daemon_row(tmp_path):
    made = database(tmp_path)
    filed = file_one(made, n=3, now=10.0)
    assert filed is not None

    result = approve(made, filed.id, now=10.0)

    assert result is not None
    daemon_id, key = result
    assert key, "a daemon with no key could only ever reconnect by name"
    row = daemon_row(made, daemon_id)
    assert row["name"] == filed.hostname
    assert row["status"] == "paired"
    assert row["version"] == filed.version
    assert row["paired_at"]
    assert authenticate_key(made, key) == daemon_id


def test_approve_twice_hands_the_key_over_only_once(tmp_path):
    """The second `approve` is refused by the guard, not by the row being gone
    -- the row now survives (see the protocol test below), so what proves
    "only once" here is that `daemon` gains exactly one row, not two."""
    made = database(tmp_path)
    filed = file_one(made, n=4, now=10.0)
    assert filed is not None

    first = approve(made, filed.id, now=10.0)
    second = approve(made, filed.id, now=11.0)

    assert first is not None
    assert second is None
    row = claim_row(made, filed.id)
    assert row is not None, "the row survives approval for a later poll to collect from"
    assert row["daemon_id"] == first[0]
    with closing(made.connect()) as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM daemon WHERE name = ?", (filed.hostname,)
        ).fetchone()["n"]
    assert count == 1, "a second approve must not mint a second daemon"


def test_approve_then_two_polls_and_a_second_approve(tmp_path):
    """The protocol end to end (design spec S6.3 steps 3-5): file, approve
    once, poll (`get_claim`, exactly what `GET /api/racks/claims/{id}` reads)
    twice and see the same grant both times, and a second approve finds
    nothing left to guard because the first already won the race.
    """
    made = database(tmp_path)
    filed = file_one(made, n=20, now=10.0)
    assert filed is not None

    approved = approve(made, filed.id, now=10.0)
    assert approved is not None
    daemon_id, key = approved

    first_poll = get_claim(made, filed.id, now=11.0)
    second_poll = get_claim(made, filed.id, now=12.0)
    assert first_poll is not None
    assert second_poll is not None
    assert first_poll.id == second_poll.id == filed.id
    assert first_poll.daemon_id == second_poll.daemon_id == daemon_id
    assert first_poll.granted_at == second_poll.granted_at == 10.0
    # The one key ever minted for this claim is still the daemon's key, both
    # times -- nothing about a repeat poll invalidates or rotates it, which is
    # what makes re-sending the same (future: encrypted) value on a repeat
    # poll a safe thing to do.
    assert authenticate_key(made, key) == daemon_id
    assert authenticate_key(made, key) == daemon_id

    again = approve(made, filed.id, now=13.0)
    assert again is None


def test_a_refiled_claim_after_grant_gets_a_fresh_row_not_a_collision(tmp_path):
    """A fingerprint whose only row was already granted is not a retry in
    flight -- the protocol already finished for it once -- so filing again
    must not collide with the granted row on `claim_pending_fingerprint`."""
    made = database(tmp_path)
    filed = file_one(made, n=21, now=0.0)
    assert filed is not None
    approved = approve(made, filed.id, now=0.0)
    assert approved is not None

    refiled = file_one(made, n=21, now=100.0)

    assert refiled is not None
    assert refiled.id != filed.id
    assert refiled.first_seen == 100.0
    assert [c.id for c in list_pending(made, now=100.0)] == [refiled.id]
    # The granted row is untouched by the re-file.
    granted_row = claim_row(made, filed.id)
    assert granted_row is not None
    assert granted_row["daemon_id"] == approved[0]


def test_a_granted_claim_is_swept_a_lifetime_after_it_was_granted(tmp_path):
    """Retention for a granted row awaiting its daemon's poll: the same
    `CLAIM_LIFETIME_S` window as a pending claim, counted from `granted_at`
    rather than `first_seen` -- chosen over "delete on ack" because there is
    no ack channel in this store layer yet (that is the poll route's job, a
    later task), and an un-swept granted row would silently reopen the exact
    unbounded-growth leak `_expire` exists to close.
    """
    made = database(tmp_path)
    filed = file_one(made, n=22, now=0.0)
    assert filed is not None
    approved = approve(made, filed.id, now=0.0)
    assert approved is not None

    just_under = CLAIM_LIFETIME_S - 0.001
    assert get_claim(made, filed.id, now=just_under) is not None

    at_lifetime = CLAIM_LIFETIME_S
    assert get_claim(made, filed.id, now=at_lifetime) is None
    assert claim_row(made, filed.id) is None


def test_a_claim_approved_late_survives_its_own_filing_deadline(tmp_path):
    """An approval made 31 minutes after filing must not be swept by the
    pending-claim expiry the moment it crosses `first_seen + CLAIM_LIFETIME_S`
    -- that clock stops applying to a row the instant it is granted, and the
    granted row gets its own, later deadline counted from `granted_at`."""
    made = database(tmp_path)
    filed = file_one(made, n=23, now=0.0)
    assert filed is not None

    just_before_pending_deadline = CLAIM_LIFETIME_S - 1.0
    approved = approve(made, filed.id, now=just_before_pending_deadline)
    assert approved is not None

    # Past the *filing's* deadline, but well inside the grant's own deadline.
    past_filing_deadline = CLAIM_LIFETIME_S + 1.0
    assert get_claim(made, filed.id, now=past_filing_deadline) is not None


def test_only_the_hash_of_the_granted_key_is_stored(tmp_path):
    """The plaintext key must not survive on disk once handed over, exactly as
    `pairing.claim_token`'s key never does."""
    made = database(tmp_path)
    filed = file_one(made, n=5, now=0.0)
    assert filed is not None

    _, key = approve(made, filed.id, now=0.0)

    assert key.encode() not in (tmp_path / "ors.db").read_bytes()


def test_deny_removes_the_claim_and_records_the_fingerprint(tmp_path):
    """Filed and denied at two different non-zero times, so a mix-up between
    `first_seen`, the deny clock and `denied_at` -- three timestamps this
    project has been bitten by coinciding before -- cannot hide behind all
    three landing on the same value."""
    made = database(tmp_path)
    filed = file_one(made, n=6, now=100.0)
    assert filed is not None

    denied = deny(made, filed.id, now=250.0)

    assert denied is True
    assert get_claim(made, filed.id, now=250.0) is None
    with closing(made.connect()) as connection:
        row = connection.execute(
            "SELECT * FROM denied_fingerprint WHERE fingerprint = ?", (filed.fingerprint,)
        ).fetchone()
    assert row is not None
    assert row["denied_at"] == 250.0


def test_deny_of_an_unknown_claim_returns_false(tmp_path):
    made = database(tmp_path)

    assert deny(made, "no-such-claim", now=0.0) is False


def test_deny_of_an_already_granted_claim_returns_false_and_leaves_it_alone(tmp_path):
    """A granted claim id is not `deny`'s to remove: an admin cannot un-grant
    through this path, and a stray deny of one must not delete a row a
    daemon's poll might still be about to collect from, nor suppress a
    fingerprint that was just legitimately approved."""
    made = database(tmp_path)
    filed = file_one(made, n=12, now=0.0)
    assert filed is not None
    approved = approve(made, filed.id, now=0.0)
    assert approved is not None

    assert deny(made, filed.id, now=1.0) is False

    row = claim_row(made, filed.id)
    assert row is not None
    assert row["daemon_id"] == approved[0]
    with closing(made.connect()) as connection:
        suppressed = connection.execute(
            "SELECT 1 FROM denied_fingerprint WHERE fingerprint = ?", (filed.fingerprint,)
        ).fetchone()
    assert suppressed is None


def test_a_denied_fingerprint_filing_again_within_the_suppression_window_gets_nothing(tmp_path):
    made = database(tmp_path)
    filed = file_one(made, n=7, now=100.0)
    assert filed is not None
    assert deny(made, filed.id, now=250.0) is True

    just_under = 250.0 + DENY_SUPPRESSION_S - 0.001
    retried = file_one(made, n=7, now=just_under)

    assert retried is None
    assert list_pending(made, now=just_under) == []


def test_after_the_suppression_window_a_denied_fingerprint_may_file_again(tmp_path):
    made = database(tmp_path)
    filed = file_one(made, n=8, now=100.0)
    assert filed is not None
    assert deny(made, filed.id, now=250.0) is True

    at_window = 250.0 + DENY_SUPPRESSION_S
    retried = file_one(made, n=8, now=at_window)

    assert retried is not None
    assert [c.id for c in list_pending(made, now=at_window)] == [retried.id]


def test_approve_of_an_expired_claim_returns_none(tmp_path):
    """A 30-minute-old claim is gone (design spec S6.5); an admin clicking a
    stale list entry must not be able to resurrect and approve it."""
    made = database(tmp_path)
    filed = file_one(made, n=9, now=0.0)
    assert filed is not None

    assert approve(made, filed.id, now=CLAIM_LIFETIME_S) is None
    with closing(made.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) AS n FROM daemon").fetchone()["n"]
    assert count == 0, "an expired claim must not be able to mint a daemon"


def test_deny_of_an_expired_claim_returns_false_and_records_nothing(tmp_path):
    """The chatty-rack case design spec S6.5 names -- the rack whose claim just
    aged out is the one an admin is most likely to click deny on -- buys no
    suppression here. That is a deliberate, documented choice (see `deny`'s
    docstring), not an oversight: pinned so a future change to it is made on
    purpose."""
    made = database(tmp_path)
    filed = file_one(made, n=10, now=0.0)
    assert filed is not None

    assert deny(made, filed.id, now=CLAIM_LIFETIME_S) is False
    with closing(made.connect()) as connection:
        row = connection.execute(
            "SELECT * FROM denied_fingerprint WHERE fingerprint = ?", (filed.fingerprint,)
        ).fetchone()
    assert row is None


def test_the_schema_refuses_two_claims_with_the_same_fingerprint(tmp_path):
    """Whitebox: `claim_pending_fingerprint` is the backstop, not just
    `file_claim`'s own pre-check. Reaching in with raw SQL is the only way to
    see it bite if that pre-check were ever removed or written wrong."""
    made = database(tmp_path)
    with closing(made.connect()) as connection:
        connection.execute(
            "INSERT INTO claim"
            " (id, hostname, address, fingerprint, short_code, version, public_key, first_seen)"
            " VALUES ('claim-a', 'host-a', '10.0.0.1', 'dup-fp', 'AAAAAA', '1.0', 'key-a', 0.0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO claim"
                " (id, hostname, address, fingerprint, short_code, version, public_key, first_seen)"
                " VALUES ('claim-b', 'host-b', '10.0.0.2', 'dup-fp', 'BBBBBB', '2.0', 'key-b', 1.0)"
            )


def test_get_claim_returns_none_for_an_id_nobody_filed(tmp_path):
    made = database(tmp_path)

    assert get_claim(made, "never-filed", now=0.0) is None


def test_count_pending_matches_the_length_of_list_pending(tmp_path):
    made = database(tmp_path)
    for i in range(1, 5):
        assert file_one(made, n=i, now=0.0) is not None

    assert count_pending(made, now=0.0) == len(list_pending(made, now=0.0)) == 4


def test_the_three_constants_are_pinned_by_value(tmp_path):
    """Each is a decision, not an accident, and a future edit to any of them
    must be a deliberate one that updates this assertion along with it:

    - `MAX_PENDING = 32` -- the size of the pending-claims queue before the
      unauthenticated filing endpoint starts refusing new ones.
    - `CLAIM_LIFETIME_S = 1800.0` -- thirty minutes before an unapproved claim
      is treated as though it were never filed.
    - `DENY_SUPPRESSION_S = 86400.0` -- twenty-four hours before a denied
      fingerprint may file again.
    """
    assert (MAX_PENDING, CLAIM_LIFETIME_S, DENY_SUPPRESSION_S) == (32, 1800.0, 86400.0)
