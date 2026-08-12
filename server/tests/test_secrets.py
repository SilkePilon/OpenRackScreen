import stat
from contextlib import closing

import pytest
from cryptography.fernet import InvalidToken
from ors_server.db import Database
from ors_server.secrets import SecretStore, load_or_create_key


def store(tmp_path) -> SecretStore:
    database = Database(tmp_path / "ors.db")
    database.initialise()
    return SecretStore(database, load_or_create_key(tmp_path, None))


def test_a_secret_round_trips(tmp_path):
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")

    assert secrets.get(secret_id) == "hunter2"


def test_the_stored_form_is_not_the_plaintext(tmp_path):
    secrets = store(tmp_path)
    secrets.put("hunter2")

    blob = (tmp_path / "ors.db").read_bytes()
    assert b"hunter2" not in blob


def test_a_key_is_generated_once_and_reused(tmp_path):
    first = load_or_create_key(tmp_path, None)

    assert load_or_create_key(tmp_path, None) == first


def test_the_generated_key_file_is_not_world_readable(tmp_path):
    load_or_create_key(tmp_path, None)
    mode = (tmp_path / "secret.key").stat().st_mode

    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), "a key file readable by anyone is not a key"


def test_the_key_file_is_private_even_in_a_permissive_directory(tmp_path):
    """The mode comes from the open, not from the directory it lands in.

    A data directory left group- or world-writable is a plausible deployment
    mistake, and the key file has to survive it: it is the whole boundary.
    """
    data_dir = tmp_path / "loose"
    data_dir.mkdir()
    # chmod rather than `mkdir(mode=...)`, which the umask would take the group
    # and other bits straight back off again, leaving the test not testing this.
    data_dir.chmod(0o777)
    load_or_create_key(data_dir, None)

    assert stat.S_IMODE((data_dir / "secret.key").stat().st_mode) == 0o600


def test_a_configured_key_wins_over_the_file(tmp_path):
    configured = load_or_create_key(tmp_path, "9" * 44)

    assert configured == b"9" * 44
    assert not (tmp_path / "secret.key").exists(), "nothing is written when a key is supplied"


def test_a_secret_encrypted_under_another_key_will_not_decrypt(tmp_path):
    """`InvalidToken`, not `KeyError`.

    A caller has to tell "this secret is gone" from "this database is not mine",
    and only the second is a reason to stop the whole server.
    """
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")

    other = SecretStore(secrets.database, load_or_create_key(tmp_path / "other", None))
    with pytest.raises(InvalidToken):
        other.get(secret_id)


def test_a_tampered_ciphertext_will_not_decrypt(tmp_path):
    """Fernet authenticates, so an edited row is a failure and not a wrong answer.

    The edit is a flipped character inside the token rather than an appended
    one: base64 decoding ignores anything after the padding, so a suffix is not
    a tamper at all.
    """
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")
    with closing(secrets.database.connect()) as connection:
        token = connection.execute(
            "SELECT ciphertext FROM secret WHERE id = ?", (secret_id,)
        ).fetchone()[0]
        middle = len(token) // 2
        edited = token[:middle] + ("B" if token[middle] == "A" else "A") + token[middle + 1 :]
        connection.execute("UPDATE secret SET ciphertext = ? WHERE id = ?", (edited, secret_id))

    with pytest.raises(InvalidToken):
        secrets.get(secret_id)


def test_deleting_a_secret_removes_it(tmp_path):
    secrets = store(tmp_path)
    secret_id = secrets.put("hunter2")
    secrets.delete(secret_id)

    with pytest.raises(KeyError):
        secrets.get(secret_id)
