"""Booting from a cached snapshot, pairing from a shell, and wiring the two together.

The supervisor's own `apply` is pinned in `test_supervisor.py`, beside the
shutdown ordering it has to keep. What is here is the three things around it: the
loader that decides what a rack draws when the server is unreachable, the
`connect` command that writes the pairing, and the `run` that joins a link to a
supervisor.

Nothing here opens a socket, binds a port or touches SPI. The link and the
supervisor are both stood in for, because what `run` is being asked is how it
wires them, not what they do once wired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from ors_daemon.__main__ import DEFAULT_LINK_PATH, main
from ors_daemon.config import load_cached_snapshot
from ors_schema.daemon import DaemonConfig

SNAPSHOT: dict[str, Any] = {
    "version": 1,
    "timezone": "UTC",
    "night": {"enabled": False},
    "integrations": [],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/ors-panels"},
            "template": "text-only",
            "params": {"big": "one"},
        }
    ],
}


def write_cache(path: Path, version: int = 12, snapshot: Any = None) -> Path:
    path.write_text(json.dumps({"version": version, "snapshot": snapshot or SNAPSHOT}))
    return path


def write_config(tmp_path: Path, name: str = "LOCAL") -> Path:
    path = tmp_path / "rack.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "timezone": "UTC",
                "night": {"enabled": False},
                "screens": [
                    {
                        "name": name,
                        "position": 1,
                        "display": {"backend": "virtual", "out_dir": str(tmp_path / "panels")},
                        "template": "text-only",
                        "params": {"big": "local"},
                    }
                ],
            }
        )
    )
    return path


class RecordingSupervisor:
    """Stands in for the real one so `run` returns instead of running forever."""

    instances: list[RecordingSupervisor] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.applied: list[DaemonConfig] = []
        self.ran = 0
        self.stops = 0
        # A real `Supervisor` publishes this so that everything it does not own
        # -- the link thread here -- parks on the same event its own `stop` sets.
        self.stop_event = _Flag()
        RecordingSupervisor.instances.append(self)

    def run_forever(self) -> None:
        self.ran += 1

    def stop(self) -> None:
        self.stops += 1

    def apply(self, config: DaemonConfig) -> None:
        self.applied.append(config)


class _Flag:
    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


class FakeLink:
    """A link that connects to nothing and records how it was built."""

    instances: list[FakeLink] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.joins: list[float | None] = []
        self.alive = False
        FakeLink.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture(autouse=True)
def _fresh_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingSupervisor.instances.clear()
    FakeLink.instances.clear()
    monkeypatch.setattr("ors_daemon.__main__.Supervisor", RecordingSupervisor)
    monkeypatch.setattr("ors_daemon.__main__.LinkClient", FakeLink)
    monkeypatch.setattr("ors_daemon.__main__._install_signal_handlers", lambda stop: True)


def run(tmp_path: Path, *extra: str) -> int:
    return main(
        [
            "run",
            "--config",
            str(tmp_path / "rack.yaml"),
            "--status",
            str(tmp_path / "status.json"),
            "--link",
            str(tmp_path / "link.json"),
            *extra,
        ]
    )


# --- the cache loader -------------------------------------------------------


def test_a_cached_snapshot_is_loaded_with_its_version(tmp_path: Path) -> None:
    loaded = load_cached_snapshot(write_cache(tmp_path / "snapshot.json"))

    assert loaded is not None
    config, version = loaded
    assert version == 12
    assert config.screens[0].name == "CPU"


@pytest.mark.parametrize(
    ("what", "written"),
    [
        ("not json at all", "{ not json"),
        ("a bare number", "7"),
        ("a top-level array", '[{"version": 1}]'),
        ("a top-level string", '"snapshot"'),
        ("null", "null"),
        ("no version", json.dumps({"snapshot": SNAPSHOT})),
        ("no snapshot", json.dumps({"version": 3})),
        ("a version that is not a number", json.dumps({"version": "abc", "snapshot": SNAPSHOT})),
        ("a version that is a list", json.dumps({"version": [1], "snapshot": SNAPSHOT})),
        ("a version that is null", json.dumps({"version": None, "snapshot": SNAPSHOT})),
        ("a snapshot that is a string", json.dumps({"version": 1, "snapshot": "nope"})),
        ("a snapshot that is a list", json.dumps({"version": 1, "snapshot": []})),
        ("a snapshot the schema refuses", json.dumps({"version": 1, "snapshot": {"screens": 4}})),
        (
            "a screen the schema refuses",
            json.dumps({"version": 1, "snapshot": {"screens": [{"rotation": 45}]}}),
        ),
        ("an empty file", ""),
    ],
)
def test_a_cache_that_cannot_be_read_is_no_cache_rather_than_a_crash(
    tmp_path: Path, what: str, written: str
) -> None:
    """Task 9 shipped a loader whose docstring promised None and which raised.

    The promise is load-bearing: this is read at startup, and a daemon that
    refuses to boot over a corrupt file is four dark panels for the sake of a
    file it does not need in order to draw. So every one of these is a shape a
    half-written or hand-edited file really takes, not a schema violation dressed
    up as one -- `int("abc")` raises `ValueError`, `int([1])` and `"nope"["x"]`
    raise `TypeError`, and none of them is a `ValidationError`.
    """
    cache = tmp_path / "snapshot.json"
    cache.write_text(written)

    assert load_cached_snapshot(cache) is None, what


def test_a_cache_that_is_not_text_at_all_is_no_cache(tmp_path: Path) -> None:
    """A truncated write on a Pi that lost power leaves bytes, not characters.

    `read_text` raises `UnicodeDecodeError` for those, which is a `ValueError`
    and not an `OSError` -- the same distinction that made `--status /` an
    infinite restart loop in M2.
    """
    cache = tmp_path / "snapshot.json"
    cache.write_bytes(b"\xff\xfe\x00 not utf-8")

    assert load_cached_snapshot(cache) is None


def test_a_cache_that_is_not_there_is_no_cache(tmp_path: Path) -> None:
    assert load_cached_snapshot(tmp_path / "never-written.json") is None


def test_a_cache_path_that_is_a_directory_is_no_cache(tmp_path: Path) -> None:
    directory = tmp_path / "snapshot.json"
    directory.mkdir()

    assert load_cached_snapshot(directory) is None


# --- connect ----------------------------------------------------------------


def test_connect_writes_link_settings_a_run_can_find(tmp_path: Path) -> None:
    link = tmp_path / "link.json"

    assert (
        main(["connect", "--server", "http://s:8080", "--token", "tok", "--link", str(link)]) == 0
    )

    written = json.loads(link.read_text())
    assert written["server_url"] == "http://s:8080"
    assert written["token"] == "tok"
    assert written["key"] is None, "a key is minted by the server, never by this command"
    assert written["cache_path"] == str(tmp_path / "snapshot.json")


def test_connect_writes_the_pairing_no_wider_than_its_owner(tmp_path: Path) -> None:
    """The token is the right to reconfigure this rack and draw on its panels."""
    link = tmp_path / "link.json"

    main(["connect", "--server", "http://s:8080", "--token", "tok", "--link", str(link)])

    assert link.stat().st_mode & 0o777 == 0o600


def test_connect_refuses_to_overwrite_an_existing_pairing_without_force(
    tmp_path: Path, capsys: Any
) -> None:
    link = tmp_path / "link.json"
    main(["connect", "--server", "http://s", "--token", "a", "--link", str(link)])

    assert main(["connect", "--server", "http://s", "--token", "b", "--link", str(link)]) == 1
    assert "already paired" in capsys.readouterr().err
    assert json.loads(link.read_text())["token"] == "a", "the working pairing is untouched"


def test_connect_replaces_a_pairing_when_it_is_told_to(tmp_path: Path) -> None:
    link = tmp_path / "link.json"
    main(["connect", "--server", "http://s", "--token", "a", "--link", str(link)])

    assert (
        main(["connect", "--server", "http://s", "--token", "b", "--link", str(link), "--force"])
        == 0
    )
    assert json.loads(link.read_text())["token"] == "b"


def test_connect_drops_the_previous_servers_cached_snapshot(tmp_path: Path) -> None:
    """Otherwise the next boot draws the old server's rack and claims its version.

    `LinkClient._paired` clears the cache when the server answers, which covers
    the pairing that completes. This covers the window before it: between
    `connect --force` and the first successful connect there is a reboot, and
    the cache on disk names a configuration from a server this rack has been
    pointed away from.
    """
    link = tmp_path / "link.json"
    cache = write_cache(tmp_path / "snapshot.json")
    main(["connect", "--server", "http://s", "--token", "a", "--link", str(link)])

    assert not cache.exists()


def test_connect_refuses_a_server_url_nothing_can_dial(tmp_path: Path, capsys: Any) -> None:
    """`--server rack:8080` names no host, and the failure belongs here.

    Written to disk it is a daemon that dials nothing once a backoff forever,
    with the reason only in its own log.
    """
    link = tmp_path / "link.json"

    assert main(["connect", "--server", "rack:8080", "--token", "t", "--link", str(link)]) == 1
    assert "rack:8080" in capsys.readouterr().err
    assert not link.exists()


def test_connect_reports_a_path_it_cannot_write_without_a_traceback(
    tmp_path: Path, capsys: Any
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")

    assert (
        main(["connect", "--server", "http://s", "--token", "t", "--link", str(blocked / "l.json")])
        == 1
    )
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.strip()


def test_connect_needs_no_config_file(tmp_path: Path) -> None:
    """Pairing happens before a rack has a configuration, and after it has one
    the configuration comes from the server. Requiring `--config` here would make
    the first thing an operator runs the one thing they cannot yet supply."""
    link = tmp_path / "link.json"

    assert main(["connect", "--server", "http://s", "--token", "t", "--link", str(link)]) == 0


# --- run --------------------------------------------------------------------


def test_run_boots_from_the_cached_snapshot_when_there_is_one(tmp_path: Path) -> None:
    """A server that is down must not be able to darken the rack, which means
    the last thing it pushed has to be what a cold boot draws."""
    write_config(tmp_path)
    write_cache(tmp_path / "snapshot.json")

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert [screen.config.name for screen in supervisor.kwargs["screens"]] == ["CPU"]


def test_run_boots_from_the_config_file_when_there_is_no_cache(tmp_path: Path) -> None:
    write_config(tmp_path)

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert [screen.config.name for screen in supervisor.kwargs["screens"]] == ["LOCAL"]


def test_run_boots_from_the_config_file_when_the_cache_is_corrupt(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "snapshot.json").write_text("{ not json")

    assert run(tmp_path) == 0
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["LOCAL"]


def test_run_boots_from_the_config_file_when_the_cache_names_a_template_it_cannot_serve(
    tmp_path: Path,
) -> None:
    """Valid against the schema is not the same as servable.

    A cached snapshot naming a template this build does not ship validates
    perfectly and then resolves to nothing at all, which without the fallback is
    a rack that stays dark for as long as the server is away.
    """
    write_config(tmp_path)
    broken = {**SNAPSHOT, "screens": [{**SNAPSHOT["screens"][0], "template": "no-such-template"}]}
    write_cache(tmp_path / "snapshot.json", snapshot=broken)

    assert run(tmp_path) == 0
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["LOCAL"]


def test_run_boots_from_the_config_file_when_the_cache_names_an_unknown_timezone(
    tmp_path: Path,
) -> None:
    """Night mode is computed in the configured zone, so the daemon refuses to
    start on one the host cannot resolve -- and a cache is not worth refusing on."""
    write_config(tmp_path)
    write_cache(tmp_path / "snapshot.json", snapshot={**SNAPSHOT, "timezone": "Mars/Olympus"})

    assert run(tmp_path) == 0
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["LOCAL"]


def test_run_with_neither_a_cache_nor_a_usable_config_says_so_and_leaves(
    tmp_path: Path, capsys: Any
) -> None:
    """No panel has been opened, so there is nothing to darken and nothing to
    keep alive. What must not happen is a traceback: the audience is someone
    reading `journalctl` after a `systemctl start` that did not take."""
    (tmp_path / "rack.yaml").write_text("screens: [{}]")

    assert run(tmp_path) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert str(tmp_path / "rack.yaml") in captured.err
    assert RecordingSupervisor.instances == [], "nothing was started"


def test_run_names_both_sources_when_neither_can_be_read(tmp_path: Path, capsys: Any) -> None:
    (tmp_path / "rack.yaml").write_text("screens: [{}]")
    (tmp_path / "snapshot.json").write_text("{ not json")

    assert run(tmp_path) == 1
    error = capsys.readouterr().err
    assert str(tmp_path / "snapshot.json") in error
    assert str(tmp_path / "rack.yaml") in error


def test_run_starts_no_link_when_this_daemon_has_never_been_paired(tmp_path: Path) -> None:
    """M2's rack still runs: a local config file and no server at all."""
    write_config(tmp_path)

    assert run(tmp_path) == 0
    assert FakeLink.instances == []


def test_run_starts_a_link_when_there_is_a_pairing(tmp_path: Path) -> None:
    write_config(tmp_path)
    link = tmp_path / "link.json"
    link.write_text(json.dumps({"server_url": "http://s:8080", "key": "k", "daemon_id": 4}))

    assert run(tmp_path) == 0
    assert len(FakeLink.instances) == 1
    started = FakeLink.instances[0]
    assert started.started == 1
    assert started.kwargs["settings"].server_url == "http://s:8080"
    assert started.kwargs["settings_path"] == link, (
        "the key from Paired is written here; a link without it pairs and is locked out"
    )


def test_a_pushed_snapshot_reaches_the_supervisor(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    pushed = DaemonConfig.model_validate(SNAPSHOT)
    FakeLink.instances[0].kwargs["on_snapshot"](pushed, 9)

    assert RecordingSupervisor.instances[-1].applied == [pushed]


def test_the_link_claims_the_version_the_rack_is_actually_running(tmp_path: Path) -> None:
    """`Hello.config_version` lets the server skip a push, and a push it skips
    wrongly is a rack showing the previous configuration forever."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))
    write_cache(tmp_path / "snapshot.json", version=31)

    assert run(tmp_path) == 0
    assert FakeLink.instances[0].kwargs["config_version"] == 31


def test_the_link_claims_no_version_when_the_rack_booted_from_its_own_file(
    tmp_path: Path,
) -> None:
    """None means "I have nothing", and getting it wrong optimistically is a
    blank rack: 0 is a real version, and the one an empty server counts from."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    assert FakeLink.instances[0].kwargs["config_version"] is None


def test_the_link_claims_no_version_when_the_cache_it_named_was_unusable(
    tmp_path: Path,
) -> None:
    """A cache whose version parses but whose snapshot does not is a cache the
    daemon did not boot from, so claiming its number claims a configuration that
    is not on the panels."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))
    broken = {**SNAPSHOT, "screens": [{**SNAPSHOT["screens"][0], "template": "no-such-template"}]}
    write_cache(tmp_path / "snapshot.json", version=31, snapshot=broken)

    assert run(tmp_path) == 0
    assert FakeLink.instances[0].kwargs["config_version"] is None


def test_the_link_parks_on_the_event_the_supervisors_own_stop_sets(tmp_path: Path) -> None:
    """One event, not two. The link refuses to *start* an apply once it is set,
    and `Supervisor.stop` sets it before it joins anything -- so a push already
    in flight when SIGTERM lands is not a teardown and repaint of four panels
    against a supervisor that is being torn down underneath it."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert FakeLink.instances[0].kwargs["stop"] is supervisor.stop_event


def test_run_waits_for_the_link_thread_with_a_deadline(tmp_path: Path) -> None:
    """A wedged link must not be what keeps the process alive past `TimeoutStopSec`."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    joined = FakeLink.instances[0].joins
    assert joined, "the link thread is joined, not abandoned"
    assert all(timeout is not None and timeout > 0 for timeout in joined)


def test_a_link_that_will_not_start_does_not_stop_the_rack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the whole link exists under: no failure of it may darken the rack.

    A Pi too short of memory to fork answers `Thread.start` with a
    `RuntimeError`, and a daemon that let that out would exit rather than draw
    the configuration it has already loaded and can serve perfectly well.
    """
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    class Refusing(FakeLink):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr("ors_daemon.__main__.LinkClient", Refusing)

    assert run(tmp_path) == 0
    assert RecordingSupervisor.instances[-1].ran == 1, "the rack ran anyway"


def test_the_default_link_path_is_the_one_the_shipped_unit_creates() -> None:
    """The two have to agree, and nothing else makes them.

    The pairing is written by the *daemon* as well as by `connect` -- the key
    from `Paired` arrives once and is unrecoverable if it is not saved -- and the
    daemon runs as its own user under `ProtectSystem=full`. So the directory has
    to be one systemd creates for it. If the default here ever moves out of the
    unit's `StateDirectory`, every rack pairs successfully, logs one error, and
    is locked out on its next boot.
    """
    unit = (Path(__file__).resolve().parents[1] / "examples" / "openrackscreen.service").read_text()

    assert DEFAULT_LINK_PATH.is_absolute()
    assert DEFAULT_LINK_PATH.name == "link.json"
    assert "StateDirectory=openrackscreen" in unit
    assert DEFAULT_LINK_PATH.parent == Path("/var/lib/openrackscreen")
