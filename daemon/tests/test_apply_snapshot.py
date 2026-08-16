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
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from ors_daemon.__main__ import (
    _RECLOCK_EXIT,
    _USAGE_EXIT,
    DEFAULT_LINK_PATH,
    _command_handler,
    _frame_pump,
    main,
)
from ors_daemon.clock import ClockError, system_clock
from ors_daemon.config import load_cached_snapshot, resolve_screens
from ors_daemon.frames import FramePump, FrameStream
from ors_daemon.link import LinkSettings, write_link_settings
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor
from ors_schema.daemon import DaemonConfig
from ors_schema.link import Command, FramesRequest
from PIL import Image

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


def write_config(tmp_path: Path, name: str = "LOCAL", timezone: str = "UTC") -> Path:
    path = tmp_path / "rack.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "timezone": timezone,
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
    # Set by a test that needs something to happen *during* `run_forever` --
    # a real one blocks until `stop`, and a push arriving mid-block is exactly
    # what `_run`'s `_RECLOCK_EXIT` handling needs to observe. This fake
    # returns at once, so without a hook there is no "during" to speak of.
    run_forever_hook: Callable[[RecordingSupervisor], None] | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.applied: list[DaemonConfig] = []
        self.identified: list[int | None] = []
        self.ran = 0
        self.stops = 0
        # A real `Supervisor` publishes this so that everything it does not own
        # -- the link thread here -- parks on the same event its own `stop` sets.
        self.stop_event = _Flag()
        RecordingSupervisor.instances.append(self)

    def run_forever(self) -> None:
        self.ran += 1
        if RecordingSupervisor.run_forever_hook is not None:
            RecordingSupervisor.run_forever_hook(self)

    def stop(self) -> None:
        self.stops += 1

    def apply(self, config: DaemonConfig) -> None:
        self.applied.append(config)

    def identify(self, screen_id: int | None = None) -> int:
        self.identified.append(screen_id)
        return 0


class _Flag:
    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


class _RecordingHandler(logging.Handler):
    """Appends every record it sees. See `test_prometheus.py`'s
    `recorded_warnings` for why this attaches straight to a module's own
    logger instead of using `caplog`: `setup_logging` sets `propagate = False`
    on the `ors_daemon` logger, and `caplog`'s handler lives on the root."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


class FakeLink:
    """A link that connects to nothing and records how it was built."""

    instances: list[FakeLink] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.joins: list[float | None] = []
        self.alive = False
        self.sent: list[Any] = []
        FakeLink.instances.append(self)

    def send(self, message: Any) -> bool:
        """What the frame pump is handed. A real link answers False when it is down."""
        self.sent.append(message)
        return False

    def start(self) -> None:
        self.started += 1

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture(autouse=True)
def _fresh_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingSupervisor.instances.clear()
    RecordingSupervisor.run_forever_hook = None
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


@pytest.mark.parametrize(
    ("what", "written"),
    [
        ("a version past what a float can hold", '{"version": 1e400, "snapshot": {}}'),
        ("a version that is JSON's own infinity", '{"version": Infinity, "snapshot": {}}'),
        ("a document nested deeper than the parser's stack", "[" * 20_000 + "]" * 20_000),
    ],
    # Named, or the third case puts forty kilobytes of brackets in the test id.
    ids=["1e400", "Infinity", "20k nested arrays"],
)
def test_a_cache_no_narrow_guard_could_have_named_is_no_cache(
    tmp_path: Path, what: str, written: str
) -> None:
    """The third time this class of bug has shipped in this milestone.

    `1e400` and `Infinity` both parse to a float `int()` refuses with
    `OverflowError`, which is an `ArithmeticError`; twenty thousand nested arrays
    exhaust the parser's stack with `RecursionError`, which is a `RuntimeError`.
    Neither is in any list of "what a corrupt file looks like" written from
    experience, and both escaped `_boot`, `_run` and `main` -- which under the
    shipped unit's `Restart=always` and `StartLimitIntervalSec=0` is a
    five-second restart loop with four dark circles.

    This is why the guard is broad: the file is untrusted input on the boot path,
    and nothing in it is worth killing the daemon over.
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


@pytest.mark.parametrize("force", [[], ["--force"]])
def test_connect_drops_the_previous_servers_cached_snapshot(
    tmp_path: Path, force: list[str]
) -> None:
    """Otherwise the next boot draws the old server's rack and claims its version.

    `LinkClient._paired` clears the cache when the server answers, which covers
    the pairing that completes. This covers the window before it: between
    `connect` and the first successful connect there is a reboot, and the cache
    on disk names a configuration from a server this rack has been pointed away
    from. `_boot` hands that cache's *version* to `_link`, which claims it in
    `Hello.config_version` -- so the new server can match the number, skip the
    push, and leave the previous server's rack on the glass for ever.

    Both invocations, and that is the point of the parametrisation: the docstring
    used to say `--force` cleared the cache and the code cleared it always. The
    code was right. A pairing file too corrupt to read is precisely a file that
    cannot say which server the cache beside it came from, and `--force` is not
    needed to get past one of those.
    """
    link = tmp_path / "link.json"
    cache = write_cache(tmp_path / "snapshot.json")
    if force:
        main(["connect", "--server", "http://old", "--token", "a", "--link", str(link)])
        cache = write_cache(tmp_path / "snapshot.json")

    main(["connect", "--server", "http://s", "--token", "a", "--link", str(link), *force])

    assert not cache.exists()


def test_connect_run_as_root_says_the_daemon_may_not_be_able_to_read_it(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sudo ors-daemon connect` writes a pairing the daemon's own user cannot read.

    Which unpairs the rack silently: the daemon sees a `PermissionError`, runs
    from its config file and says it was never paired. The file itself is
    correct, so this is a note and not a failure -- said to the person who can
    still fix it with one `chown`.
    """
    link = tmp_path / "link.json"
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert main(["connect", "--server", "http://s", "--token", "t", "--link", str(link)]) == 0

    error = capsys.readouterr().err
    assert "chown" in error
    assert "User=openrackscreen" in error


def test_connect_as_an_ordinary_user_says_nothing_about_ownership(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note only says anything if the ordinary case is quiet."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    assert (
        main(
            ["connect", "--server", "http://s", "--token", "t", "--link", str(tmp_path / "l.json")]
        )
        == 0
    )
    assert capsys.readouterr().err == ""


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


@pytest.mark.parametrize("hostile", ["/", ".", ""])
def test_connect_refuses_a_link_path_with_no_filename_rather_than_raising(
    capsys: Any, hostile: str
) -> None:
    """`--link /` is M2's `--status /` bug, byte for byte.

    A path with no filename has no name to derive the cache beside, and
    `Path.with_name` answers that with `ValueError` rather than `OSError` -- so
    it went past every guard here and out of `main` as a traceback. `main`
    promises a message and an exit code for anything a person can mistype.
    """
    assert main(["connect", "--server", "http://s", "--token", "t", "--link", hostile]) == 1

    error = capsys.readouterr().err
    assert "Traceback" not in error
    assert hostile in error or "no file name" in error


@pytest.mark.parametrize("hostile", ["/", ".", ""])
def test_run_boots_from_its_config_file_when_the_link_path_has_no_filename(
    tmp_path: Path, hostile: str
) -> None:
    """The same typo on the other command, where the cost is higher.

    `Supervisor.tick` and `LinkClient._write_cache` each defend against this by
    name; the cache path `run` derives did not. Under `Restart=always` that is a
    five-second restart loop with four dark circles, over a flag that names no
    pairing this rack was ever going to find. There is a perfectly servable
    config file, so the rack runs from it.
    """
    write_config(tmp_path)

    assert (
        main(
            [
                "run",
                "--config",
                str(tmp_path / "rack.yaml"),
                "--status",
                str(tmp_path / "status.json"),
                "--link",
                hostile,
            ]
        )
        == 0
    )
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["LOCAL"]


def test_connect_needs_no_config_file(tmp_path: Path) -> None:
    """Pairing happens before a rack has a configuration, and after it has one
    the configuration comes from the server. Requiring `--config` here would make
    the first thing an operator runs the one thing they cannot yet supply."""
    link = tmp_path / "link.json"

    assert main(["connect", "--server", "http://s", "--token", "t", "--link", str(link)]) == 0


# --- boot without --config: the five-row table's rows 2-5 -------------------


def run_no_config(tmp_path: Path, *extra: str) -> int:
    return main(
        [
            "run",
            "--status",
            str(tmp_path / "status.json"),
            "--link",
            str(tmp_path / "link.json"),
            *extra,
        ]
    )


def test_run_boots_from_the_cached_snapshot_with_no_config_file_at_all(tmp_path: Path) -> None:
    """Row 2: a paired rack that has been pushed to never reads the YAML
    `--config` used to force it to supply, because `--config` is not on the
    command line at all here."""
    link = tmp_path / "link.json"
    main(["connect", "--server", "http://s", "--token", "t", "--link", str(link)])
    write_cache(tmp_path / "snapshot.json")

    assert run_no_config(tmp_path) == 0
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["CPU"]


def test_a_paired_rack_with_nothing_pushed_yet_starts_with_no_screens(tmp_path: Path) -> None:
    """Row 3: not an error, and there is no file to fall back to either -- one
    was never given. The panels appear when the server's first push lands."""
    link = tmp_path / "link.json"
    main(["connect", "--server", "http://s", "--token", "t", "--link", str(link)])

    assert run_no_config(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert supervisor.kwargs["screens"] == []
    # `0` is a real version -- the one an empty server counts from
    # (`link.py:699`) -- and `_dispatch` skips a push whose version equals the
    # claim (`link.py:869`). A row-3 rack claiming `0` instead of `None` could
    # be left blank forever by a server that starts counting there.
    assert FakeLink.instances[0].kwargs["config_version"] is None


def test_a_freshly_installed_rack_enters_the_join_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 4: no pairing and no `--config` is the state `install` leaves a
    machine in, and it is not an error. Before M3c `--config` was required, so
    this state could not even be booted; this also proves that mutation dead:
    `run_no_config` never passes `--config` at all, so a required flag would
    fail this in argparse before `join_a_server` was ever reached.

    `join_a_server` blocks until this rack is paired and writes the pairing to
    `args.link` -- see its docstring -- so the fake here does the same thing a
    real implementation must, and `_run` is expected to *continue in-process*
    from there rather than exit and trust systemd's `Restart=` to bring the
    process back already paired: run by hand, an exit there would print
    nothing and leave the panels dark with no explanation at all.
    """
    joined: list[Any] = []

    def fake_join(args: Any) -> None:
        joined.append(True)
        write_link_settings(
            args.link,
            LinkSettings(server_url="http://s", cache_path=tmp_path / "snapshot.json", token="t"),
        )

    monkeypatch.setattr("ors_daemon.__main__.join_a_server", fake_join)

    assert run_no_config(tmp_path) == 0
    assert joined == [True]
    assert RecordingSupervisor.instances != [], (
        "the caller falls through to _boot once paired, in this same process, "
        "rather than exiting for a restart to pick it up"
    )
    assert RecordingSupervisor.instances[-1].kwargs["screens"] == [], (
        "row 3: freshly paired, nothing pushed yet"
    )


def test_running_unpaired_without_config_fails_cleanly_not_with_a_traceback(
    tmp_path: Path, capsys: Any
) -> None:
    """Row 4 today, before Task 15 lands: `join_a_server` is a deliberate stub
    that raises `NotImplementedError` rather than silently doing nothing (see
    its docstring). This is the *real*, unpatched stub -- unlike every other
    test of row 4 in this file, nothing here monkeypatches `join_a_server`.

    This is exactly the state `ors-daemon install` leaves a fresh machine in:
    both `examples/openrackscreen.service` and the unit `install.py` generates
    run `ExecStart=... run` with no `--config`, under `Restart=always` and
    `StartLimitIntervalSec=0`. Left uncaught, this command's `NotImplementedError`
    was a Python traceback written to the journal every `RestartSec=5`, forever.
    """
    assert run_no_config(tmp_path) == 1
    error = capsys.readouterr().err
    assert "Traceback" not in error
    assert "ors-daemon connect" in error, "the message has to name what to do instead"
    assert RecordingSupervisor.instances == []


def test_join_a_server_returning_unpaired_fails_cleanly_not_with_an_assert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """MEDIUM-C. `join_a_server`'s own docstring promises it blocks "until
    either the interface approves it or the operator interrupts the process"
    -- a clean return with nothing written, on abandonment, is a shape that
    docstring names rather than rules out. So is a pairing written but then
    unreadable, which is HIGH 2's own scenario from round 1.

    Both used to fall through to `_boot`'s `assert settings is not None`,
    which is an `AssertionError` traceback under an ordinary interpreter and
    silently vanishes under `python -O` into an unrequested row-3 boot.
    Neither is a message a person over SSH can act on; `_run` has to check
    this itself, explicitly, right after the re-read.

    Round 3, LOW 7: the message named no remedy ("nothing to run") for the
    SSH audience this module keeps invoking, and the assertion only checked
    that a traceback was absent -- deleting the whole `print` while keeping
    `return 1` (a silent exit 1, the very shape HIGH 1 was raised about) still
    passed. Now asserts the message names the way out.
    """
    monkeypatch.setattr("ors_daemon.__main__.join_a_server", lambda args: None)

    assert run_no_config(tmp_path) == 1
    error = capsys.readouterr().err
    assert "Traceback" not in error
    assert "ors-daemon connect" in error, "the message has to name a remedy, not just say no"
    assert "--server" in error
    assert "--token" in error
    assert RecordingSupervisor.instances == [], "nothing was ever started"


@pytest.mark.parametrize("extra_args", [(), ("--no-discovery",)], ids=["default", "--no-discovery"])
def test_an_unreadable_pairing_still_boots_from_a_good_cached_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_args: tuple[str, ...]
) -> None:
    """`load_link_settings` returns `None` for a corrupt or unreadable pairing
    file exactly the way it does for an absent one -- its own docstring: "a
    corrupt pairing file is not a reason to leave four panels dark" -- and the
    common cause is `sudo ors-daemon connect`, which writes link.json
    root-owned 0600 while the shipped unit runs the daemon as
    `User=openrackscreen`, so every read of it there is a `PermissionError`.

    Under the shipped unit there is no `--config`, so a rack in this state must
    not fall into the join flow -- and, before Task 15, into a traceback --
    while a perfectly good `snapshot.json` sits right beside the pairing it
    cannot read.

    LOW-E: parametrized over `--no-discovery` too, not only the default. Row
    5's refusal -- discovery off, no `--server` -- has to be decided *after*
    trying the cache, not evaluated first with the cache never reached; the
    reworded `--no-discovery` help text and the README both claim the cache
    gate applies to row 5 as well, and nothing before this exercised the two
    together.

    Round 3, LOW 8: also pins `_link`'s `version is not None` branch, which
    the round-1 report claimed was "covered incidentally by every existing
    test" with nothing actually asserted about it -- collapsing both of
    `_link`'s log branches onto the plain "no pairing" message (`if version is
    not None:` -> `if False:`) passed the whole suite. This scenario -- an
    unreadable pairing with a usable cache beside it -- is exactly the one the
    `version is not None` branch exists for, so the pin belongs here rather
    than in a new test.

    Attached straight to `ors_daemon.__main__`'s own logger rather than using
    `caplog`: `main()` calls `setup_logging`, which sets `propagate = False`
    on the `ors_daemon` logger the moment this test runs, and `caplog`'s own
    handler lives on the root logger -- see `test_prometheus.py`'s
    `recorded_warnings` for the same workaround, already established in this
    suite for the same reason.
    """
    link = tmp_path / "link.json"
    link.write_text("{ not valid json")  # load_link_settings returns None for this
    write_cache(tmp_path / "snapshot.json")

    joined: list[Any] = []
    monkeypatch.setattr("ors_daemon.__main__.join_a_server", lambda *a, **k: joined.append(True))

    records: list[logging.LogRecord] = []
    logger = logging.getLogger("ors_daemon.__main__")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        assert run_no_config(tmp_path, *extra_args) == 0
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert joined == [], "a usable cached snapshot must be tried before giving up on this rack"
    assert [
        screen.config.name for screen in RecordingSupervisor.instances[-1].kwargs["screens"]
    ] == ["CPU"]

    cache_records = [r for r in records if "cached snapshot beside" in r.getMessage()]
    assert len(cache_records) == 1, (
        "the pairing-unreadable-but-cache-usable branch has to log its own message, "
        "not the plain no-pairing one"
    )
    assert cache_records[0].config_version == 12, "and it has to name the version it is running"


def test_discovery_off_with_no_server_and_no_config_names_every_way_to_fix_it(
    tmp_path: Path, capsys: Any
) -> None:
    """Row 5: the one case with no way to obtain a configuration at all --
    discovery is off and nothing names a server to dial directly. A message
    naming every way out, not a traceback: the audience is somebody over SSH,
    and a traceback tells them about this program rather than about their
    machine.
    """
    assert run_no_config(tmp_path, "--no-discovery") == 1
    error = capsys.readouterr().err
    assert "--config" in error
    assert "--server" in error
    assert "Traceback" not in error
    assert RecordingSupervisor.instances == []


def test_a_server_flag_still_enters_the_join_flow_with_discovery_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--server` is one of row 5's two ways out even with discovery off: it
    names a server to dial directly, so there is still a way to get a
    configuration and this is row 4 again, not row 5."""
    joined: list[Any] = []

    def fake_join(args: Any) -> None:
        joined.append(True)
        write_link_settings(
            args.link,
            LinkSettings(
                server_url="http://s:8080", cache_path=tmp_path / "snapshot.json", token="t"
            ),
        )

    monkeypatch.setattr("ors_daemon.__main__.join_a_server", fake_join)

    assert run_no_config(tmp_path, "--no-discovery", "--server", "http://s:8080") == 0
    assert joined == [True]


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


def test_a_push_that_changes_the_timezone_stops_the_rack_instead_of_running_it_mismatched(
    tmp_path: Path,
) -> None:
    """`Supervisor.apply` never rebuilds the clock -- `supervisor.py`'s own
    docstring says why: it is built once in `__main__` and handed to the
    supervisor as a component it does not own. `NightWindow` defaults to
    enabled, 23:00-07:00, so applying a push that changes the timezone in place
    would leave the night window evaluated in the *old* zone for the rest of
    the process -- a UTC+2 rack sleeping at 01:00 local and waking at 09:00
    local, silently, until an operator restarts it by hand.

    So a timezone-changing push must not be applied here at all: the
    supervisor is stopped instead, which the shipped unit's `Restart=always`
    turns into the restart the mismatch actually needs.

    MEDIUM-D: booted from a *non-UTC* config and pushed `UTC`, deliberately the
    reverse of the obvious way to write this. Every other test in this suite
    boots `UTC`, so a mutant that replaced `booted_timezone` at its call site
    with the literal `"UTC"` still passed the whole daemon suite -- nothing
    pinned that `_snapshot_handler` is comparing against the timezone the rack
    actually booted with rather than the schema's own default. Booting
    `Europe/Amsterdam` and pushing `UTC` fails under that mutant (`"UTC" ==
    "UTC"` looks like no change at all) exactly where the UTC-booted version of
    this test cannot tell the difference.
    """
    write_config(tmp_path, timezone="Europe/Amsterdam")
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    pushed = DaemonConfig.model_validate({**SNAPSHOT, "timezone": "UTC"})

    FakeLink.instances[0].kwargs["on_snapshot"](pushed, 9)

    assert supervisor.applied == [], "must not run against a clock built for the old zone"
    assert supervisor.stops == 1, "left to restart, which is what re-clocks it"


def test_a_push_matching_the_timezone_the_rack_actually_booted_with_applies_normally(
    tmp_path: Path,
) -> None:
    """MEDIUM-D's converse, and the other half of the same mutant: a rack
    booted `Europe/Amsterdam` that gets pushed the exact same zone back has to
    apply it, not stop -- a rack that cannot tell "same zone" from "different
    zone" any way but "differs from a hardcoded UTC" would stop on *every*
    push it was ever sent, forever, once it was ever paired to anything but a
    UTC-configured server.
    """
    write_config(tmp_path, timezone="Europe/Amsterdam")
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    pushed = DaemonConfig.model_validate({**SNAPSHOT, "timezone": "Europe/Amsterdam"})

    FakeLink.instances[0].kwargs["on_snapshot"](pushed, 9)

    assert supervisor.applied == [pushed], "same timezone as booted; must apply, not stop"
    assert supervisor.stops == 0


def test_a_push_with_an_unresolvable_timezone_is_refused_not_boot_looped(tmp_path: Path) -> None:
    """MEDIUM-B. `DaemonConfig.timezone` is a bare `str` with no validation
    against the host's tzdata -- the server has no way to know what any given
    rack ships -- so `Europe/Amsterdaam`, one letter off, reaches here exactly
    as easily as a real zone does.

    Treated as an ordinary mismatch -- stop, let `Restart=always` bring it back
    -- that typo boot-loops a rack that was healthy: the next boot rebuilds the
    clock from the very same unresolvable name, `_boot_from_cache` raises
    `ClockError`, falls back to row 3, claims `config_version=None`, and the
    server reads that as "never got it" and pushes the identical broken value
    again. Forever, panels dark.

    So this is raised instead of swallowed into a stop. `LinkClient._config`
    turns any exception out of `on_snapshot` into a `Nack` naming the reason
    and leaves the running configuration untouched -- refused, not applied,
    and not stopped either.
    """
    write_config(tmp_path)  # timezone: UTC
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0
    supervisor = RecordingSupervisor.instances[-1]
    pushed = DaemonConfig.model_validate({**SNAPSHOT, "timezone": "Europe/Amsterdaam"})

    with pytest.raises(ClockError):
        FakeLink.instances[0].kwargs["on_snapshot"](pushed, 9)

    assert supervisor.applied == [], "an unresolvable timezone must not be applied either"
    assert supervisor.stops == 0, "a stop only helps if the next boot could rebuild the clock"


def test_a_timezone_reclock_stop_exits_nonzero_not_like_a_clean_stop(
    tmp_path: Path, capsys: Any
) -> None:
    """MEDIUM-A. `run` used to return 0 unconditionally, so a stop caused by a
    timezone-changing push was indistinguishable from a clean SIGTERM. Only
    `Restart=always` recovers a rack that exits that way; `Restart=on-failure`,
    any supervisor that is not systemd, and a rack run by hand at a terminal
    all read exit 0 as success and never re-clock at all -- the hand-run rack
    prints nothing and just stops.

    The push has to land *during* `run_forever`, not after `run` has already
    returned -- `run_forever_hook` is what lets this fake simulate that,
    calling the link's `on_snapshot` from inside the call `_run` is blocked on
    in a real process.

    Round 3, HIGH 1: this used to assert `run(tmp_path) == _RECLOCK_EXIT`,
    importing and comparing against the very constant it was meant to pin --
    tautological in the value, so `_RECLOCK_EXIT = 0` passed this test (and
    all 847 others) even though 0 is a clean stop, the one thing this exit
    code exists to be told apart from. Asserted on the literal below instead,
    plus a standing guarantee this constant can never again collide with 0,
    with `_USAGE_EXIT`, or with the plain `1` every other failure in this
    module returns.

    Round 3, HIGH 2: the stderr line is the entire deliverable for a rack run
    by hand, and used to be unpinned -- deleting the whole `print` while
    keeping `return _RECLOCK_EXIT` passed the suite. Asserted below that the
    message names both the timezone and the restart.
    """

    def push_a_mismatched_timezone(supervisor: RecordingSupervisor) -> None:
        pushed = DaemonConfig.model_validate({**SNAPSHOT, "timezone": "Europe/Amsterdam"})
        FakeLink.instances[-1].kwargs["on_snapshot"](pushed, 9)

    write_config(tmp_path)  # timezone: UTC
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))
    RecordingSupervisor.run_forever_hook = push_a_mismatched_timezone

    assert run(tmp_path) == 10, "the literal value, not the constant that names it"
    assert _RECLOCK_EXIT not in (0, 1, _USAGE_EXIT), (
        "reserved: 0 is a clean stop, 1 is every plain failure in this module, "
        "and _USAGE_EXIT is argparse's own -- none may ever also mean "
        "'restart to re-clock'"
    )
    assert RecordingSupervisor.instances[-1].stops == 1

    error = capsys.readouterr().err
    assert "timezone" in error, "names what changed"
    assert "restart" in error, "names the remedy"


def test_a_clean_stop_still_exits_zero(tmp_path: Path, capsys: Any) -> None:
    """The pin for the test above: `_RECLOCK_EXIT` must be reserved for a
    re-clock, not handed out for an ordinary stop that never touched a push at
    all.

    Round 3, HIGH 2: also pins that the re-clock stderr line is not printed
    unconditionally -- an ordinary stop must say nothing on stderr at all. The
    pairing is written 0600 and a cache is provided, unlike this file's other
    fixtures, precisely so that stderr is otherwise silent: `link.json` at the
    default `write_text` mode logs a "readable by more than their owner"
    warning, and no cache logs one of its own, both unrelated to the thing
    this test pins and both loud enough to swamp the assertion below.
    """
    write_config(tmp_path)
    link = tmp_path / "link.json"
    link.write_text(json.dumps({"server_url": "http://s", "key": "k"}))
    link.chmod(0o600)
    write_cache(tmp_path / "snapshot.json")

    assert run(tmp_path) == 0
    assert capsys.readouterr().err == ""


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


# --- frames -----------------------------------------------------------------


def test_a_frames_request_from_the_server_reaches_the_stream_the_workers_offer_to(
    tmp_path: Path,
) -> None:
    """One stream, wired to both ends: the link asks it for frames and the
    supervisor's workers hand it panels. Two would be a rack that encodes
    nothing for a browser that is asking, with nothing anywhere saying why."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0

    handler = FakeLink.instances[0].kwargs["on_frames_request"]
    frames = RecordingSupervisor.instances[-1].kwargs["frames"]
    assert handler.__self__ is frames


def test_a_link_that_drops_stops_the_stream_it_started(
    tmp_path: Path,
) -> None:
    """The other end of the same wire. A subscription arrives on a socket, so
    the daemon may not go on encoding for one that has gone -- the server asks
    again on every connect, which is what makes forgetting free."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0

    handler = FakeLink.instances[0].kwargs["on_link_down"]
    frames = RecordingSupervisor.instances[-1].kwargs["frames"]
    assert handler.__self__ is frames
    assert handler.__func__ is FrameStream.disable


def test_the_pump_sends_what_it_encodes_down_the_link(tmp_path: Path) -> None:
    """That `_frame_pump` wires the pump's `send` to *this* link's, driven by hand.

    The pump's own thread is stopped before anything is offered, and that is the
    fix for a race rather than a tidiness: the thread this function starts calls
    `take()` in a loop, so a test that then calls `take()` itself is two readers
    of a queue holding one item, and whichever loses indexes an empty list. It
    did not reproduce in 300 repetitions, and the pump was doing nothing the
    test needed -- what is being asked is where the frames go, not that a thread
    can carry them, which the pump's own suite covers.
    """
    frames = FrameStream()
    supervisor = RecordingSupervisor()
    supervisor.stop_event.set()
    link = FakeLink()

    pump = _frame_pump(frames, supervisor, link)

    assert pump is not None
    pump.join(5.0)
    assert not pump.is_alive(), "the pump is still racing this test for the queue"
    try:
        frames.request(FramesRequest(enabled=True, screen_ids=[7]))
        frames.offer(7, Image.new("RGB", (240, 240), (0, 128, 255)))
        pump._one(*frames.take(1.0)[0])
    finally:
        frames.close()
    assert [message.screen_id for message in link.sent] == [7]


def test_the_frame_stream_is_closed_when_the_daemon_stops(tmp_path: Path) -> None:
    """The pump parks on the stream, not on the stop event, so a stream left
    open is a thread that leaves at its next poll rather than at once."""
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0

    assert RecordingSupervisor.instances[-1].kwargs["frames"].closed is True


def test_an_unpaired_rack_starts_no_frame_pump(tmp_path: Path) -> None:
    """Nothing could ask for a frame and there is nowhere to put one, so the
    thread would be a lifetime of parking and waking on a Pi with four panels."""
    write_config(tmp_path)

    assert run(tmp_path) == 0

    assert FakeLink.instances == []
    assert _frame_pump(FrameStream(), RecordingSupervisor(), None) is None


def test_a_pump_that_will_not_start_leaves_the_rack_drawing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Pi too short of memory for one more thread still has four panels."""
    monkeypatch.setattr(FramePump, "start", _raises)

    assert _frame_pump(FrameStream(), RecordingSupervisor(), FakeLink()) is None


def _raises(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("can't start new thread")


# --- commands ---------------------------------------------------------------


def test_a_command_from_the_server_reaches_the_running_supervisor(tmp_path: Path) -> None:
    """The wire that was never joined. `LinkClient` was built with `on_snapshot`,
    `on_frames_request` and `on_link_down` and no `on_command` at all, so
    `_dispatch` logged "nothing is listening for this" and the API's
    `POST /api/daemons/{id}/command` answered `{"delivered": true}` for a message
    that reached no supervisor -- a button that lies by a different road from the
    one its own docstring reasons about.
    """
    write_config(tmp_path)
    (tmp_path / "link.json").write_text(json.dumps({"server_url": "http://s", "key": "k"}))

    assert run(tmp_path) == 0

    handler = FakeLink.instances[0].kwargs["on_command"]
    assert handler is not None
    handler(Command(command="identify", screen_id=None))
    assert RecordingSupervisor.instances[-1].identified == [None]


class _RealRack:
    """A real `Supervisor` over virtual panels, for the handler's own tests.

    A double that only recorded the call would prove the handler was called and
    nothing about what each command *means* against a rack that is driving
    panels -- which is the whole question, since three of the four turn out to
    mean nothing here at all.
    """

    def __init__(self, tmp_path: Path, screens: int = 2) -> None:
        raw = {
            "version": 1,
            "timezone": "UTC",
            "night": {"enabled": False},
            "integrations": [],
            "screens": [
                {
                    "id": position * 10,
                    "name": f"S{position}",
                    "position": position,
                    "display": {"backend": "virtual", "out_dir": str(tmp_path / "panels")},
                    "template": "text-only",
                    "params": {"line1": "x"},
                }
                for position in range(1, screens + 1)
            ],
        }
        config = DaemonConfig.model_validate(raw)
        self.supervisor = Supervisor(
            config=config,
            screens=resolve_screens(config),
            store=SnapshotStore(),
            clock=system_clock("UTC"),
            status_path=tmp_path / "status.json",
        )
        self.supervisor.start()
        self.handler = _command_handler(self.supervisor)

    def scenes(self) -> list[str | None]:
        return [worker.current_scene for worker in self.supervisor.workers]


def test_identify_off_the_link_paints_the_ordinals_on_a_real_rack(tmp_path: Path) -> None:
    rack = _RealRack(tmp_path)
    try:
        rack.handler(Command(command="identify"))

        assert rack.scenes() == ["identify", "identify"]
    finally:
        rack.supervisor.stop()


def test_identify_off_the_link_can_name_one_panel(tmp_path: Path) -> None:
    rack = _RealRack(tmp_path)
    try:
        rack.handler(Command(command="identify", screen_id=20))

        assert rack.scenes()[1] == "identify"
        assert rack.scenes()[0] != "identify"
    finally:
        rack.supervisor.stop()


@pytest.mark.parametrize("command", ["sleep", "wake", "reload"])
def test_a_command_this_build_cannot_honour_says_so_and_touches_nothing(
    tmp_path: Path, command: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The three that have no honest meaning against a running supervisor.

    `sleep` and `wake` are what the night window and `ScreenConfig.sleep_override`
    already are, and deciding which of a transient command and a persistent
    configuration field wins is a design question rather than a wiring one;
    `reload` has nothing to reload, because a paired rack's configuration comes
    from the server and `POST /api/daemons/{id}/push` is how it is re-sent. The
    server refuses all three, so nothing this daemon is talking to can send one
    -- but a newer server or a replayed message can, and the answer is a line
    somebody can act on rather than silence.
    """
    rack = _RealRack(tmp_path)
    try:
        before = rack.scenes()
        with caplog.at_level("WARNING"):
            rack.handler(Command(command=command))

        assert rack.scenes() == before
        assert [record.command for record in caplog.records if hasattr(record, "command")] == [
            command
        ]
    finally:
        rack.supervisor.stop()


def test_a_command_arriving_while_the_daemon_stops_touches_no_panel(tmp_path: Path) -> None:
    """The panels are slept and their serial devices closed by then, and on a
    GC9A01 a command written to one that has been torn down is a write to
    nothing. `_dispatch` runs on the thread the stop event is consulted on, so
    the message really can arrive here."""
    rack = _RealRack(tmp_path)
    rack.supervisor.stop()
    before = rack.scenes()

    rack.handler(Command(command="identify"))

    assert rack.scenes() == before
    assert "identify" not in before
