"""Everything `ors-server install` changes on a machine, against roots that are not it.

No test in this file may touch `/etc`, `/var`, `/opt` or systemd. Every path is
under `tmp_path` and every subprocess goes through `FakeRunner`, which records
argv and returns whatever the test says. A test that shelled out for real would
pass on the author's laptop and reconfigure a reviewer's -- and the damage would
land in `/etc/systemd/system`, where nothing under `tmp_path` would notice it.

The file is deliberately shaped like `daemon/tests/test_install.py` -- same
`Roots`, same `FakeRunner`, same sandbox rule -- and just as deliberately shares
no code with it. `ors_server.install`'s module docstring says why the two
modules are not merged; this suite is where the difference between the two units
is actually asserted, in both directions: what the server's unit must carry that
the daemon's does not (`ORS_DATA_DIR`, `PrivateDevices=yes`) and what it must not
carry that the daemon's does (`SupplementaryGroups`, `TimeoutStopSec=30`).
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from ors_server import __version__
from ors_server.install import Roots, install, uninstall, unit_text


@dataclass
class FakeRunner:
    """Records what would have been run, and answers what the test decides.

    `codes` maps the first argument -- `useradd`, `systemctl`, `uv` -- to the
    exit code to return, defaulting to 0. Keyed on the program and not on the
    whole argv because a test that cared about the arguments asserts on
    `calls`, and one that only wants a failure should not have to spell the
    successful command out to get it.
    """

    codes: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return self.codes.get(Path(argv[0]).name, 0)

    def programs(self) -> list[str]:
        return [Path(call[0]).name for call in self.calls]

    def argv_for(self, program: str) -> list[list[str]]:
        return [call for call in self.calls if Path(call[0]).name == program]


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    for name in ("state", "opt", "systemd"):
        (tmp_path / name).mkdir()
    return Roots(
        state=tmp_path / "state",
        prefix=tmp_path / "opt" / "ors-server",
        systemd=tmp_path / "systemd",
    )


def _install(roots: Roots, runner: FakeRunner, **kwargs):
    # `ors_server.__version__` and not the literal it happens to hold today: a
    # hardcoded string here is self-referential, and the assertion that
    # `install` puts the version on the `uv pip install` command line would go
    # on passing at 0.3.0 while proving only that this file agrees with itself.
    kwargs.setdefault("version", __version__)
    return install(roots, runner, **kwargs)


def _non_comment_lines(text: str) -> str:
    """`text` with every `#`-comment line removed, joined back with newlines.

    A comment is free to name a setting it explains the *absence* of -- this
    unit's hardening block names `SupplementaryGroups` and `TimeoutStopSec`
    to say they are the daemon's and not the server's -- so an assertion that
    a setting is absent has to check the real `key=value` lines only, or the
    explanation would trip the very test it exists to justify.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


# --- the data directory ----------------------------------------------------


def test_it_creates_the_data_directory(roots):
    _install(roots, FakeRunner())
    assert (roots.state / "ors-server").is_dir()


def test_the_data_directory_is_private(roots):
    """0700. It holds `secret.key` -- the Fernet key every stored integration
    credential is encrypted under -- and `ors.db`, which holds the ciphertext
    and the admin password hash right beside it. A mode anyone can read is a
    key anyone can read."""
    _install(roots, FakeRunner())
    mode = stat.S_IMODE((roots.state / "ors-server").stat().st_mode)
    assert mode == 0o700


def test_the_data_directory_is_not_the_daemons(roots):
    """`/var/lib/openrackscreen` is the daemon's `StateDirectory`, created
    0700 and owned by the same user, and it holds the pairing -- the one file
    a rack cannot be given again. Dropping `ors.db` into it is the collision
    `server/README.md` already warns about for bind mounts, and an install
    that made it by default would make that warning useless."""
    _install(roots, FakeRunner())
    assert not (roots.state / "openrackscreen").exists()


def test_running_it_twice_changes_nothing_the_second_time(roots):
    """`install` is documented as safe to re-run and is the upgrade path."""
    first = _install(roots, FakeRunner())
    second = _install(roots, FakeRunner())
    assert first.unit_path.read_text() == second.unit_path.read_text()
    assert first.created_user is True
    assert second.created_user is False


# --- the user --------------------------------------------------------------


def test_it_creates_a_system_user_with_no_login(roots):
    runner = FakeRunner()
    _install(roots, runner)
    [argv] = runner.argv_for("useradd")
    assert "openrackscreen" in argv
    assert "--system" in argv
    # A service account that can be logged into is an account that can be
    # logged into. There is nothing for a person to do as this user.
    assert "/usr/sbin/nologin" in argv or "/sbin/nologin" in argv


def test_an_existing_user_is_not_recreated(roots):
    """`useradd` on an existing user exits 9 -- which is also what happens on
    a Pi where `ors-daemon install` already created it -- and treating that as
    a failure would make both the upgrade path and the both-halves-one-machine
    install fail."""
    runner = FakeRunner(codes={"useradd": 9})
    report = _install(roots, runner)
    assert report.created_user is False
    assert report.failed is False


def test_a_useradd_that_fails_is_reported_and_the_install_is_marked_failed(roots):
    """Any other exit code is a machine with a unit naming a `User=` that does
    not exist, which systemd reports as a start failure on a unit that looks
    perfectly well written."""
    runner = FakeRunner(codes={"useradd": 1})
    report = _install(roots, runner)
    assert report.failed is True
    assert "useradd" in report.warnings_text()


def test_it_does_not_touch_the_groups_that_reach_the_panels(roots):
    """`spi` and `gpio` are the daemon's, and the reason its unit has
    `SupplementaryGroups`. This process draws on no panel and opens no
    `/dev/spidev*`; adding it to those groups would hand a network-facing
    server the rack's hardware for nothing."""
    runner = FakeRunner()
    _install(roots, runner)
    assert "usermod" not in runner.programs()
    everything = " ".join(" ".join(argv) for argv in runner.calls)
    assert "spi" not in everything
    assert "gpio" not in everything


# --- the venv --------------------------------------------------------------


def test_it_installs_the_server_into_a_predictable_prefix(roots):
    """`sudo uv tool install ors-server` lands in *root's* data directory,
    which `User=openrackscreen` cannot read -- and the unit then fails at
    every boot with a permission error on the console script, in a journal
    entry that names the interpreter rather than the install that chose it."""
    runner = FakeRunner()
    _install(roots, runner)
    [venv] = runner.argv_for("uv")[:1]
    assert "venv" in venv
    assert str(roots.prefix) in " ".join(venv)
    installed = " ".join(" ".join(argv) for argv in runner.argv_for("uv")[1:])
    assert f"ors-server=={__version__}" in installed


def test_the_venv_is_not_the_daemons(roots):
    """Two venvs, because they are two distributions with different
    dependencies and `uv venv` on an existing prefix rebuilds it -- one
    prefix shared would mean each install silently replacing the other
    half's interpreter."""
    runner = FakeRunner()
    _install(roots, runner)
    joined = " ".join(" ".join(argv) for argv in runner.argv_for("uv"))
    assert "openrackscreen" not in joined
    assert "[hardware]" not in joined


def test_a_uv_that_fails_is_reported_and_the_install_is_marked_failed(roots):
    """`uv venv` or `uv pip install` exiting non-zero is the ordinary failure
    on a Pi being set up with no network yet, or against an index that 404s the
    version -- and discarding its code produced `failed=False`, exit 0 and a
    printed healthcheck for a machine with no server installed on it at all."""
    runner = FakeRunner(codes={"uv": 1})
    report = _install(roots, runner)
    assert report.failed is True
    assert "uv" in report.warnings_text()


def test_a_package_that_did_not_install_is_not_enabled_at_boot(roots):
    """The unit is `Type=simple` with `Restart=always`, `RestartSec=5` and
    `StartLimitIntervalSec=0`: the start job completes at fork, so a missing
    `ExecStart` fails asynchronously and `enable --now` exits 0 regardless, and
    with no start limit the unit can never latch into `failed`. It re-execs
    every five seconds forever while `systemctl is-failed` answers no. So the
    unit is still written -- the next `install` overwrites it -- but nothing
    enables or starts it, and the report says why."""
    runner = FakeRunner(codes={"uv": 1})
    report = _install(roots, runner)
    assert "systemctl" not in runner.programs()
    assert report.unit_path.is_file()
    assert "not enabled" in report.warnings_text()


def test_the_unit_points_at_the_prefix(roots):
    report = _install(roots, FakeRunner())
    assert f"ExecStart={roots.prefix}/bin/ors-server" in report.unit_path.read_text()


def test_the_unit_runs_the_server_with_no_subcommand(roots):
    """`ors-server` with no arguments runs the server, and this unit is one of
    the things that makes that a promise rather than an accident: there is no
    `serve` subcommand to name, and a unit that named one would be naming a
    word the parser rejects."""
    report = _install(roots, FakeRunner())
    [exec_start] = [
        line for line in report.unit_path.read_text().splitlines() if line.startswith("ExecStart=")
    ]
    assert exec_start == f"ExecStart={roots.prefix}/bin/ors-server"


# --- the unit --------------------------------------------------------------


def test_the_unit_sets_the_data_directory_explicitly(roots):
    """The single most important line in the file.

    The code default is `$XDG_STATE_HOME/openrackscreen`, resolved against the
    *service user's* home -- which for a `--system --no-create-home` account is
    `/`, or whatever systemd hands it. A unit relying on that default puts
    `ors.db` somewhere unwritable or somewhere new, and the second shape is the
    dangerous one: the server comes up healthy, answers `/api/health`, and asks
    for an admin password again because it is a fresh database. Nothing in any
    log says why.
    """
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert f"Environment=ORS_DATA_DIR={roots.state}/ors-server" in text


def test_the_unit_is_not_the_daemons(roots):
    """The assertion that the two install modules did not get merged.

    Each of these is load-bearing in `daemon/examples/openrackscreen.service`
    and meaningless -- or worse -- here. `SupplementaryGroups=spi gpio` hands
    the rack's hardware to a network-facing process; `TimeoutStopSec=30` is
    derived from four numbers in the daemon's shutdown path, none of which
    exist in this one, and thirty seconds of a stuck stop is thirty seconds a
    reboot waits for nothing.
    """
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "SupplementaryGroups" not in text
    assert "TimeoutStopSec=30" not in text
    assert "spidev" not in text
    assert "gpio" not in text


def test_the_unit_hides_the_devices_this_process_has_no_business_seeing(roots):
    """`PrivateDevices=yes`, which the daemon's unit deliberately does *not*
    have -- it would hide `/dev/spidev*` and `/dev/gpiochip*` and take the rack
    dark. The server touches no hardware at all, so the setting is free here,
    and this opposition is exactly why sharing one template between the two
    would be wrong."""
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "PrivateDevices=yes" in text


def test_the_unit_leaves_the_announcement_on(roots):
    """Unlike the image, which sets `ORS_ANNOUNCE=0` because a bridge network
    does not carry mDNS at all. This server is on the host's own link, which is
    where announcing is the whole point: it is how a rack that has never been
    paired finds a server to ask to join. Set explicitly rather than left to the
    code default, because a reader comparing this unit with the Dockerfile will
    ask which way it goes."""
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "Environment=ORS_ANNOUNCE=1" in text


def test_the_unit_keeps_the_settings_that_make_it_survive_a_reboot(roots):
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "WantedBy=multi-user.target" in text
    assert "Restart=always" in text
    assert "RestartSec=5" in text
    assert "StartLimitIntervalSec=0" in text
    assert "User=openrackscreen" in text
    # The hardening block, named line by line rather than sampled: every one of
    # these is deletable without any other test in this file noticing, and each
    # deletion loosens the sandbox a network-facing server runs inside.
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=full" in text
    assert "ProtectHome=yes" in text
    assert "ProtectControlGroups=yes" in text
    assert "ProtectKernelTunables=yes" in text
    assert "RestrictSUIDSGID=yes" in text
    # Without this the journal is where an operator is told to look and nothing
    # they are looking for is there.
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text


def test_the_unit_keeps_the_state_directory_private_too(roots):
    """`StateDirectory=` is what makes `/var/lib/ors-server` exist owned by
    `User=` on every start, whatever a restore or a `chmod -R` did to it in
    between -- the other half of the 0700 `install` sets once."""
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "StateDirectory=ors-server" in text
    assert "StateDirectoryMode=0700" in text


def test_the_port_reaches_both_the_unit_and_the_healthcheck(roots):
    """One number, two readers. The unit is what the server binds; the printed
    healthcheck is what an operator pastes to see whether it did. Two literals
    would be two numbers that have to agree, and the way they stop agreeing is
    a healthcheck that reports a healthy server on a port nothing is on."""
    report = _install(roots, FakeRunner(), port=9443)
    text = _non_comment_lines(report.unit_path.read_text())
    assert "Environment=ORS_PORT=9443" in text
    assert "9443" in report.health_command
    # `/api/health` reads no rows and needs no session. `/api/daemons` assembles
    # a snapshot per rack, which is nothing once and a standing cost forever --
    # `server/README.md` says so about the image's HEALTHCHECK, and this line is
    # read by a person who will paste it into `watch`.
    assert "/api/health" in report.health_command
    assert "/api/daemons" not in report.health_command


def test_the_port_defaults_to_the_one_the_deploy_notes_publish(roots):
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "Environment=ORS_PORT=8080" in text
    assert "8080" in report.health_command


def test_unit_text_is_callable_without_installing_anything(tmp_path):
    """`unit_text` is a pure function of three arguments, which is what lets a
    reader -- or a later test -- see the file the install would write without
    creating a user or a venv to get it."""
    text = unit_text("/opt/ors-server/bin/ors-server", tmp_path / "data", 8443)
    assert "ExecStart=/opt/ors-server/bin/ors-server" in text
    assert f"Environment=ORS_DATA_DIR={tmp_path / 'data'}" in text
    assert "Environment=ORS_PORT=8443" in text


# --- enabling --------------------------------------------------------------


def test_it_enables_and_starts_the_service(roots):
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("daemon-reload" in call for call in systemctl)
    assert any("enable" in call and "--now" in call for call in systemctl)


def test_the_unit_it_enables_is_the_one_it_wrote(roots):
    report = _install(roots, FakeRunner())
    assert report.unit_path == roots.systemd / "ors-server.service"
    assert report.unit_path.is_file()
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("ors-server.service" in call for call in systemctl)


def test_a_systemctl_that_fails_is_reported_and_the_install_is_marked_failed(roots):
    """A `daemon-reload` or an `enable --now` that fails leaves a machine whose
    unit file exists and whose service will not come back after a reboot. The
    exit code was discarded, so the CLI printed the ordinary success block and
    returned 0 over exactly that."""
    runner = FakeRunner(codes={"systemctl": 1})
    report = _install(roots, runner)
    assert report.failed is True
    assert "systemctl" in report.warnings_text()


def test_a_second_install_restarts_the_running_service(roots):
    """`enable --now` is a no-op on a unit that is already active, so on an
    upgrade -- where the venv step has just replaced the code underneath a
    running server -- nothing short of an explicit restart makes the new
    version take effect before the next reboot."""
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("try-restart" in call and "ors-server" in call for call in systemctl)


# --- the refusal -----------------------------------------------------------


def test_a_port_no_socket_could_bind_is_refused_before_anything_is_touched(roots):
    """`--port 70000` is a typo, and every step after this one is a step that
    would have to be undone: a user created, a venv built, a unit written that
    systemd would enable and start and that would then die on every boot with
    `Invalid argument`. Refusing here leaves the machine exactly as it was."""
    runner = FakeRunner()
    report = _install(roots, runner, port=70000)
    assert report.failed is True
    assert report.refused is True
    assert "70000" in report.warnings_text()
    assert runner.calls == []
    assert not (roots.systemd / "ors-server.service").exists()
    assert not (roots.state / "ors-server").exists()


def test_port_zero_is_refused_too(roots):
    """`0` means "any free port" to a kernel and "nothing a rack can be told to
    dial" to this deployment -- the announcement would name it before the bind
    picked one."""
    report = _install(roots, FakeRunner(), port=0)
    assert report.refused is True


# --- uninstall -------------------------------------------------------------


def test_uninstall_stops_disables_and_removes_the_unit(roots):
    _install(roots, FakeRunner())
    runner = FakeRunner()
    uninstall(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("stop" in call for call in systemctl)
    assert any("disable" in call for call in systemctl)
    assert not (roots.systemd / "ors-server.service").exists()


def test_uninstall_reloads_systemd_after_removing_the_unit(roots):
    """A removed unit file that systemd has not been told about is a unit
    `systemctl status` still answers for."""
    _install(roots, FakeRunner())
    runner = FakeRunner()
    uninstall(roots, runner)
    assert any("daemon-reload" in " ".join(argv) for argv in runner.argv_for("systemctl"))


def test_uninstall_on_a_machine_that_never_had_it_is_not_an_error(roots):
    """The state `uninstall` is trying to reach is exactly the state it found."""
    report = uninstall(roots, FakeRunner())
    assert report.warnings == ()


def test_uninstall_leaves_the_database_alone(roots):
    """The data directory holds the admin password, the secret key and every
    stored credential. A command called `uninstall` must not silently cost
    them; that is what `--purge` is for."""
    _install(roots, FakeRunner())
    (roots.state / "ors-server" / "ors.db").write_text("rows")
    uninstall(roots, FakeRunner())
    assert (roots.state / "ors-server" / "ors.db").read_text() == "rows"


def test_purge_removes_it_and_says_exactly_what_that_costs(roots):
    """`secret.key` is the only thing that can decrypt the integration
    credentials in `ors.db`, and nothing anywhere keeps a second copy. A
    `--purge` that removed it without saying so would be a silent, permanent
    loss dressed as tidying up."""
    _install(roots, FakeRunner())
    (roots.state / "ors-server" / "secret.key").write_text("k")
    report = uninstall(roots, FakeRunner(), purge=True)
    assert not (roots.state / "ors-server").exists()
    warnings = report.warnings_text().lower()
    assert "password" in warnings
    assert "key" in warnings
    assert "credential" in warnings


def test_purge_on_a_machine_with_no_data_directory_does_not_raise(roots):
    """`uninstall --purge` run twice, or run on a machine where `install`
    never got as far as the directory. `rmtree` on a missing path raises, and
    a traceback is not a thing a person running `uninstall` should ever
    see."""
    report = uninstall(roots, FakeRunner(), purge=True)
    assert "password" in report.warnings_text().lower()


def test_the_reports_are_actually_hashable(roots):
    """Both report dataclasses are `frozen=True`, which makes a dataclass
    hashable by default -- but a `list` field breaks that promise at `hash()`
    time rather than at the class statement, so nothing before this test would
    have noticed a `TypeError` there."""
    hash(_install(roots, FakeRunner()))
    hash(uninstall(roots, FakeRunner(), purge=True))


# --- the sandbox itself ----------------------------------------------------


def test_nothing_it_would_run_names_a_path_on_the_real_machine(roots, tmp_path):
    """The rule this whole file is written under, asserted rather than trusted.

    Every path `install` puts on a command line comes from `Roots`, which the
    fixture points at `tmp_path` -- so a hardcoded `/etc/systemd/system` or
    `/var/lib` appearing in this module shows up here as an argv token nothing
    under `tmp_path` explains, whether or not `FakeRunner` would have run it.
    `/usr/sbin/nologin` is the one absolute path that is a *value* rather than
    a location this install writes to, and it is named explicitly rather than
    passed over by a prefix rule.
    """
    runner = FakeRunner()
    _install(roots, runner)
    for argv in runner.calls:
        for token in argv:
            if token.startswith("/") and token != "/usr/sbin/nologin":
                assert token.startswith(str(tmp_path)), f"{token} is not under tmp_path"


# --- the two subcommands, from the command line ----------------------------
#
# Everything below drives `ors_server.__main__.main` rather than `install()`,
# and keeps the same sandbox promise by a different route: the tests before the
# root check make `_real_roots` and `_SubprocessRunner` blow up if either is
# reached, and the tests after it monkeypatch both -- `_real_roots` to a `Roots`
# under `tmp_path`, `_SubprocessRunner` to `FakeRunner`. No test here may reach
# `/etc/systemd/system`, `/var/lib` or `subprocess.run`.


def _explode_if_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire `_real_roots` and `_SubprocessRunner` to blow up if either is ever
    reached, so the root guard is asserted on its *shape* rather than on a
    filesystem side effect.

    Strictly stronger than checking that no `tmp_path` directory appeared: the
    damage a broken guard does lands in `/etc/systemd/system` and `/var/lib`,
    which nothing under `tmp_path` would ever notice.
    """

    class Boom:
        def run(self, argv: list[str]) -> int:
            raise AssertionError(f"runner reached: {argv}")

    monkeypatch.setattr(
        "ors_server.__main__._real_roots",
        lambda prefix: (_ for _ in ()).throw(AssertionError(f"_real_roots reached: {prefix}")),
    )
    monkeypatch.setattr("ors_server.__main__._SubprocessRunner", Boom)


def _fake_roots(base: Path, prefix: Path) -> Roots:
    for name in ("state", "systemd"):
        (base / name).mkdir(exist_ok=True)
    return Roots(state=base / "state", prefix=prefix, systemd=base / "systemd")


def _as_root_with_fake_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The two patches every test past the root check needs."""
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "ors_server.__main__._real_roots", lambda prefix: _fake_roots(tmp_path, prefix)
    )
    monkeypatch.setattr("ors_server.__main__._SubprocessRunner", FakeRunner)


def test_install_without_root_refuses_and_changes_nothing(monkeypatch, capsys, tmp_path):
    """Exit 2, the conventional "you typed it wrong" code, and no partial
    state: writing a unit into `/etc/systemd/system` is not something a
    non-root process can do halfway and report honestly about."""
    from ors_server.__main__ import main

    monkeypatch.setattr("os.geteuid", lambda: 1000)
    _explode_if_reached(monkeypatch)
    code = main(["install", "--prefix", str(tmp_path / "opt")])
    assert code == 2
    captured = capsys.readouterr()
    assert "root" in (captured.out + captured.err).lower()
    assert not (tmp_path / "opt").exists()


def test_uninstall_without_root_refuses(monkeypatch, capsys):
    from ors_server.__main__ import main

    monkeypatch.setattr("os.geteuid", lambda: 1000)
    _explode_if_reached(monkeypatch)
    assert main(["uninstall"]) == 2
    assert "root" in (capsys.readouterr().err).lower()


def test_install_help_names_every_flag(capsys):
    """`main` catches the `SystemExit` argparse raises for `--help` in the same
    `try` that catches it for a bad flag, so `--help` comes back as a plain
    `0` rather than escaping a function that promises an `int`."""
    from ors_server.__main__ import main

    assert main(["install", "--help"]) == 0
    text = capsys.readouterr().out
    assert "--prefix" in text
    assert "--port" in text


def test_uninstall_help_says_what_purge_costs(capsys):
    from ors_server.__main__ import main

    assert main(["uninstall", "--help"]) == 0
    text = capsys.readouterr().out.lower()
    assert "--purge" in text
    assert "credential" in text


def test_real_roots_names_the_actual_machine_paths():
    """The one function in the CLI that decides where a real install lands --
    every test above patches it away, so nothing else pins what it returns.
    Pure: reads and writes nothing."""
    from ors_server.__main__ import _real_roots

    assert _real_roots(Path("/x")) == Roots(
        state=Path("/var/lib"), prefix=Path("/x"), systemd=Path("/etc/systemd/system")
    )


def test_the_default_prefix_is_not_the_daemons():
    """`/opt/openrackscreen` is where `ors-daemon install` builds its venv, and
    `uv venv` on an existing prefix rebuilds it -- one shared prefix would make
    installing either half break the other."""
    from ors_server.__main__ import _parser

    assert _parser().parse_args(["install"]).prefix == Path("/opt/ors-server")


def test_the_default_port_is_the_one_the_deploy_notes_publish():
    from ors_server.__main__ import _parser

    assert _parser().parse_args(["install"]).port == 8080


def test_install_prints_the_unit_and_the_healthcheck(monkeypatch, capsys, tmp_path):
    """What an operator does next, in the two lines they will actually use:
    where the unit went, and one command that says whether the thing is up."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    assert main(["install", "--prefix", str(tmp_path / "prefix")]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "systemd" / "ors-server.service") in out
    assert "/api/health" in out


def test_install_honours_the_port_flag(monkeypatch, capsys, tmp_path):
    """Otherwise every install lands on 8080 whatever an operator asked for,
    and the first symptom is a port conflict with whatever else is on 8080."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    assert main(["install", "--port", "9443", "--prefix", str(tmp_path / "prefix")]) == 0
    capsys.readouterr()
    text = (tmp_path / "systemd" / "ors-server.service").read_text()
    assert "Environment=ORS_PORT=9443" in text
    assert "Environment=ORS_PORT=8080" not in text


def test_install_honours_the_prefix_flag(monkeypatch, capsys, tmp_path):
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    prefix = tmp_path / "somewhere-else"
    assert main(["install", "--prefix", str(prefix)]) == 0
    capsys.readouterr()
    text = (tmp_path / "systemd" / "ors-server.service").read_text()
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert str(prefix) in exec_start
    assert "/opt/ors-server" not in exec_start


def test_the_real_runner_hands_argv_to_subprocess_and_returns_its_code(monkeypatch):
    """`_SubprocessRunner` is production's only conduit to the machine, and the
    one place the exit codes `install()` reads actually come from -- yet every
    other test in this file replaces it, so its two-line body was pinned by
    nothing: `return 0` in place of `subprocess.run(argv).returncode` passed the
    whole suite while turning every failed command into a silent success.

    `subprocess.run` is replaced rather than given a real command, so this stays
    inside the sandbox rule the rest of the file is written under.
    """
    from ors_server.__main__ import _SubprocessRunner

    seen: list[list[str]] = []

    class Completed:
        returncode = 7

    def fake_run(argv, *args, **kwargs):
        seen.append(list(argv))
        return Completed()

    monkeypatch.setattr("ors_server.__main__.subprocess.run", fake_run)

    assert _SubprocessRunner().run(["systemctl", "daemon-reload"]) == 7
    assert seen == [["systemctl", "daemon-reload"]]


def test_install_installs_the_version_this_command_ships(monkeypatch, capsys, tmp_path):
    """`version=__version__` mutated to a literal survives everything else: the
    only other version assertion runs against this file's own `_install`
    helper, which supplies the version itself. This one reads the argv the CLI
    actually built."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    recorded: list[list[str]] = []

    class RecordingRunner(FakeRunner):
        def run(self, argv: list[str]) -> int:
            recorded.append(list(argv))
            return super().run(argv)

    monkeypatch.setattr("ors_server.__main__._SubprocessRunner", RecordingRunner)

    assert main(["install", "--prefix", str(tmp_path / "prefix")]) == 0
    capsys.readouterr()
    pip = [argv for argv in recorded if argv[:3] == ["uv", "pip", "install"]]
    assert pip, f"no `uv pip install` reached the runner: {recorded}"
    assert f"ors-server=={__version__}" in pip[0]


def test_install_exits_1_when_the_report_is_failed(monkeypatch, capsys, tmp_path):
    """A failed install returning 0 makes `ors-server install && reboot`
    proceed on a machine that was never actually configured."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)

    class FailingUseraddRunner(FakeRunner):
        def run(self, argv: list[str]) -> int:
            super().run(argv)
            return 1 if argv[:1] == ["useradd"] else 0

    monkeypatch.setattr("ors_server.__main__._SubprocessRunner", FailingUseraddRunner)

    assert main(["install", "--prefix", str(tmp_path / "prefix")]) == 1
    err = capsys.readouterr().err
    assert "useradd" in err
    assert "did not finish cleanly" in err


def test_a_refused_install_prints_only_the_reason(monkeypatch, capsys, tmp_path):
    """`refused=True` is the one shape where nothing was touched -- no user, no
    venv, no unit -- so printing `unit:` and a healthcheck would be reporting
    work that never happened, and the healthcheck would name a port that was
    refused for being impossible."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["install", "--port", "70000", "--prefix", str(tmp_path / "prefix")])
    assert code == 1
    captured = capsys.readouterr()
    assert "70000" in captured.err
    assert "unit:" not in captured.out
    assert not (tmp_path / "systemd" / "ors-server.service").exists()


def test_uninstall_with_purge_removes_the_data_directory(monkeypatch, capsys, tmp_path):
    """`purge=args.purge` mutated to `True` unconditionally would destroy the
    database on every plain `uninstall`; mutated to `False` would silently
    spare one the operator explicitly asked to destroy. Both directions need a
    test."""
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    data_dir = tmp_path / "state" / "ors-server"
    data_dir.mkdir(parents=True)
    (data_dir / "ors.db").write_text("rows")

    assert main(["uninstall", "--purge"]) == 0
    assert not data_dir.exists()
    assert "credential" in capsys.readouterr().err.lower()


def test_uninstall_without_purge_leaves_the_data_directory(monkeypatch, capsys, tmp_path):
    from ors_server.__main__ import main

    _as_root_with_fake_machine(monkeypatch, tmp_path)
    data_dir = tmp_path / "state" / "ors-server"
    data_dir.mkdir(parents=True)
    (data_dir / "ors.db").write_text("rows")

    assert main(["uninstall"]) == 0
    capsys.readouterr()
    assert (data_dir / "ors.db").read_text() == "rows"
