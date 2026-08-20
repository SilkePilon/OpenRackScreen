"""Everything `install` changes on a machine, against roots that are not it.

No test in this file may touch /etc, /boot, /var or systemd. Every path is
under `tmp_path` and every subprocess goes through `FakeRunner`, which records
argv and returns whatever the test says. A test that shelled out for real would
pass on the author's laptop and reconfigure a reviewer's.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from ors_daemon.install import (
    SERVICE_NAME,
    SERVICE_USER,
    Roots,
    install,
    uninstall,
)

NOW = "2026-08-16T12:00:00"


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
    for name in ("etc", "boot", "state", "opt", "systemd"):
        (tmp_path / name).mkdir()
    return Roots(
        etc=tmp_path / "etc",
        boot=tmp_path / "boot",
        state=tmp_path / "state",
        prefix=tmp_path / "opt" / "openrackscreen",
        systemd=tmp_path / "systemd",
    )


def _install(roots: Roots, runner: FakeRunner, **kwargs):
    kwargs.setdefault("version", "0.2.0")
    kwargs.setdefault("now", NOW)
    return install(roots, runner, **kwargs)


def _non_comment_lines(text: str) -> str:
    """`text` with every `#`-comment line removed, joined back with newlines.

    A comment is free to name a setting it explains the *absence* of --
    "Deliberately NOT PrivateDevices=yes" and "No --config." are both
    exactly that -- so an assertion that a setting is absent has to check
    the real `key=value` lines only, or a verbatim-copied explanatory
    comment would trip it.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


# --- directories -----------------------------------------------------------


def test_it_creates_the_directories_the_daemon_needs(roots):
    _install(roots, FakeRunner())
    assert (roots.etc / "openrackscreen").is_dir()
    assert (roots.state / "openrackscreen").is_dir()


def test_the_state_directory_is_private(roots):
    """0700. It holds the pairing and the install identity: the right to
    reconfigure this rack and to draw on its panels."""
    _install(roots, FakeRunner())
    mode = stat.S_IMODE((roots.state / "openrackscreen").stat().st_mode)
    assert mode == 0o700


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
    assert SERVICE_USER in argv
    assert "--system" in argv
    # A daemon account that can be logged into is an account that can be logged
    # into. There is nothing for a person to do as this user.
    assert "/usr/sbin/nologin" in argv or "/sbin/nologin" in argv


def test_an_existing_user_is_not_recreated(roots):
    """`useradd` on an existing user exits 9, and treating that as a failure
    would make the second `install` -- the upgrade path -- fail."""
    runner = FakeRunner(codes={"useradd": 9})
    report = _install(roots, runner)
    assert report.created_user is False
    assert report.failed is False


def test_it_joins_the_groups_that_reach_the_panels(roots):
    """On Raspberry Pi OS /dev/spidev* is group `spi` and /dev/gpiochip* is
    group `gpio`. Without both, every screen comes up unavailable."""
    runner = FakeRunner()
    _install(roots, runner)
    joined = " ".join(" ".join(argv) for argv in runner.argv_for("usermod"))
    assert "spi" in joined
    assert "gpio" in joined


def test_a_missing_group_is_reported_and_not_created(roots):
    """The groups come with the udev rules that make them mean anything.
    Inventing a group with no rules behind it produces a rack that comes up
    with four unavailable screens and a configuration that looks correct."""
    runner = FakeRunner(codes={"usermod": 6})
    report = _install(roots, runner)
    assert "gpio" in report.warnings_text() or "spi" in report.warnings_text()
    assert "groupadd" not in runner.programs()
    # Exit 6 means "no such group" -- a warning, not a failure. Falling
    # through to the generic-failure branch would make a rack whose udev
    # rules have not landed yet report a failed install.
    assert report.failed is False


# --- the venv --------------------------------------------------------------


def test_it_installs_itself_into_a_predictable_prefix(roots):
    """`sudo uv tool install ors-daemon` lands in *root's* data directory,
    which User=openrackscreen cannot read -- and the rack then comes up dead
    with a permission error on the interpreter, which appears in no daemon log
    because the daemon never starts."""
    runner = FakeRunner()
    _install(roots, runner)
    [venv] = runner.argv_for("uv")[:1]
    assert "venv" in venv
    assert str(roots.prefix) in " ".join(venv)
    installed = " ".join(" ".join(argv) for argv in runner.argv_for("uv")[1:])
    assert "ors-daemon[hardware]==0.2.0" in installed


def test_a_uv_that_fails_is_reported_and_the_install_is_marked_failed(roots):
    """`uv venv` or `uv pip install` exiting non-zero is the ordinary failure
    on a Pi being set up before it has a network, or against an index that 404s
    the version -- and discarding its code produced `failed=False`, exit 0 and
    a printed short code for a rack with no daemon installed on it at all."""
    runner = FakeRunner(codes={"uv": 1})
    report = _install(roots, runner)
    assert report.failed is True
    assert "uv" in report.warnings_text()


def test_a_package_that_did_not_install_is_not_enabled_at_boot(roots):
    """The unit is `Type=simple` with `Restart=always`, `RestartSec=5` and
    `StartLimitIntervalSec=0`: the start job completes at fork, so a missing
    `ExecStart` fails asynchronously and `enable --now` exits 0 regardless, and
    with no start limit the unit can never latch into `failed`. What that
    installs into the boot path is a rack that stays dark forever, re-execing
    every five seconds, with `systemctl is-failed` answering no and nothing but
    journal spam to find it by. The unit is still written -- the next `install`
    overwrites it -- but nothing enables or starts it."""
    runner = FakeRunner(codes={"uv": 1})
    report = _install(roots, runner)
    assert "systemctl" not in runner.programs()
    assert report.unit_path.is_file()
    assert "not enabled" in report.warnings_text()


def test_the_unit_points_at_the_prefix(roots):
    report = _install(roots, FakeRunner())
    assert f"ExecStart={roots.prefix}/bin/ors-daemon run" in report.unit_path.read_text()


def test_the_current_interpreter_can_be_used_instead(roots):
    runner = FakeRunner()
    interpreter = roots.etc / "readable" / "bin" / "ors-daemon"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)

    report = _install(roots, runner, use_current_interpreter=True, executable=interpreter)

    assert "uv" not in runner.programs()
    assert f"ExecStart={interpreter} run" in report.unit_path.read_text()


def test_a_symlinked_interpreter_is_checked_at_its_target(roots):
    """`/usr/local/bin/x -> /opt/...` and pipx/uv-tool shims are exactly this
    shape -- and it is exactly the `/root/.local/share/uv/tools/...` case the
    whole check exists for. `executable.stat()` follows the link to a
    world-readable target, but climbing the *link's* lexical parents skips
    the target's actually-private directory entirely."""
    runner = FakeRunner()
    target = roots.etc / "private" / "bin" / "ors-daemon"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    target.parent.chmod(0o700)

    link_dir = roots.etc / "usr" / "local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "ors-daemon"
    link.symlink_to(target)

    report = _install(roots, runner, use_current_interpreter=True, executable=link)

    assert report.failed is True
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


def test_an_unreadable_interpreter_is_refused_before_the_unit_is_written(roots):
    """Refusing after writing the unit leaves a machine that is configured to
    fail at every boot, and `systemctl status` blames the executable rather
    than the install that chose it."""
    runner = FakeRunner()
    interpreter = roots.etc / "private" / "bin" / "ors-daemon"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o700)

    report = _install(roots, runner, use_current_interpreter=True, executable=interpreter)

    assert report.failed is True
    assert "0700" in report.warnings_text() or "readable" in report.warnings_text()
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


def test_a_nonexistent_interpreter_is_refused_not_crashed(roots):
    """A typo'd path or a broken symlink must produce the same refusal as an
    unreadable one, not a traceback -- `executable.stat()` raises
    `FileNotFoundError` on either."""
    runner = FakeRunner()
    interpreter = roots.etc / "nope" / "bin" / "ors-daemon"

    report = _install(roots, runner, use_current_interpreter=True, executable=interpreter)

    assert report.failed is True
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


# --- the unit --------------------------------------------------------------


def test_the_unit_keeps_every_line_that_is_load_bearing(roots):
    """Each of these was learned in front of a rack, and each has a comment in
    `daemon/examples/openrackscreen.service` saying what it costs to drop."""
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "StartLimitIntervalSec=0" in text
    assert "SupplementaryGroups=spi gpio" in text
    assert "TimeoutStopSec=30" in text
    assert "RuntimeDirectory=openrackscreen" in text
    assert "StateDirectory=openrackscreen" in text
    assert f"User={SERVICE_USER}" in text
    # Deliberately absent as a *setting*. The hardening comment right above it
    # in the real file names it too, to explain why -- which is exactly what
    # `_non_comment_lines` excludes before this checks.
    assert "PrivateDevices=yes" not in text


def test_the_unit_does_not_pass_a_config_file(roots):
    """M3c made --config optional: a paired rack's configuration comes from the
    server, and a unit naming a file would make every rack need one."""
    report = _install(roots, FakeRunner())
    text = _non_comment_lines(report.unit_path.read_text())
    assert "--config" not in text


def test_the_generated_unit_and_the_example_do_not_drift(roots):
    """Two copies of this file is the seam this project has been bitten by.

    The example is what a person reads and edits by hand; the generated one is
    what actually runs. Every setting in the example that is not a path must be
    in what `install` writes.
    """
    report = _install(roots, FakeRunner())
    generated = report.unit_path.read_text()
    example = (
        Path(__file__).resolve().parents[1] / "examples" / "openrackscreen.service"
    ).read_text()

    def settings(text: str) -> set[str]:
        return {
            line.strip()
            for line in _non_comment_lines(text).splitlines()
            if "=" in line
            # Paths differ by construction: the example names /opt and the test
            # names a tmp_path.
            and not line.strip().startswith(("ExecStart=", "Documentation="))
        }

    assert settings(example) <= settings(generated)


def test_it_enables_and_starts_the_service(roots):
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("daemon-reload" in call for call in systemctl)
    assert any("enable" in call and "--now" in call for call in systemctl)


def test_a_systemctl_that_fails_is_reported_and_the_install_is_marked_failed(roots):
    """A `daemon-reload` or an `enable --now` that fails leaves a rack whose
    unit file exists and whose daemon will not come back after a reboot. The
    exit code was discarded, so the CLI printed the ordinary success block --
    short code and all -- and returned 0 over exactly that."""
    runner = FakeRunner(codes={"systemctl": 1})
    report = _install(roots, runner)
    assert report.failed is True
    assert "systemctl" in report.warnings_text()


def test_a_second_install_restarts_the_running_service(roots):
    """`enable --now` is a no-op on a unit that is already active, so on an
    upgrade -- where `install` has just replaced the venv underneath a
    running daemon -- nothing short of an explicit restart makes the new
    version take effect before the next reboot."""
    runner = FakeRunner()
    _install(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("try-restart" in call and SERVICE_NAME in call for call in systemctl)


# --- SPI -------------------------------------------------------------------


def test_it_enables_spi_by_default(roots):
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    report = _install(roots, FakeRunner())
    text = (roots.boot / "firmware" / "config.txt").read_text()
    assert "dtparam=spi=on" in text
    assert report.reboot_needed is True


def test_spi_can_be_skipped(roots):
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    report = _install(roots, FakeRunner(), enable_spi_step=False)
    assert "dtparam=spi=on" not in (roots.boot / "firmware" / "config.txt").read_text()
    assert report.reboot_needed is False


def test_no_reboot_is_claimed_when_spi_was_already_on(roots):
    """A reboot people are told to take and do not need teaches them to ignore
    the next one."""
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("dtparam=spi=on\ndtoverlay=spi1-2cs\n")
    report = _install(roots, FakeRunner())
    assert report.reboot_needed is False


def test_a_missing_config_txt_is_reported_not_silent(roots):
    """`enable_spi` returns `path=None` when the machine has no `config.txt`
    at all. Without `--no-spi`, saying nothing here tells the operator SPI
    is handled when it was never touched."""
    # roots.boot has no config.txt anywhere under it (the fixture only makes
    # the directory), so boot_config.find_config finds nothing.
    report = _install(roots, FakeRunner())
    assert report.reboot_needed is False
    assert "config.txt" in report.warnings_text()


# --- the identity ----------------------------------------------------------


def test_it_mints_the_install_identity(roots):
    """So the short code exists to be printed at the end of this command, which
    is where a person reads it before approving in the interface."""
    report = _install(roots, FakeRunner())
    assert (roots.state / "openrackscreen" / "identity.json").is_file()
    assert len(report.short_code) == 6


def test_a_second_install_keeps_the_same_short_code(roots):
    """Otherwise every upgrade files a new claim under a new code, and the
    pending entry an admin was looking at becomes a stranger."""
    first = _install(roots, FakeRunner())
    second = _install(roots, FakeRunner())
    assert second.short_code == first.short_code


# --- uninstall -------------------------------------------------------------


def test_uninstall_stops_disables_and_removes_the_unit(roots):
    _install(roots, FakeRunner())
    runner = FakeRunner()
    uninstall(roots, runner)
    systemctl = [" ".join(argv) for argv in runner.argv_for("systemctl")]
    assert any("stop" in call for call in systemctl)
    assert any("disable" in call for call in systemctl)
    assert not (roots.systemd / f"{SERVICE_NAME}.service").exists()


def test_uninstall_leaves_the_pairing_alone(roots):
    """The state directory holds the pairing and the identity. Removing it
    costs a re-approval in the interface, and a command called `uninstall`
    should not silently cost that."""
    _install(roots, FakeRunner())
    uninstall(roots, FakeRunner())
    assert (roots.state / "openrackscreen" / "identity.json").is_file()


def test_purge_removes_it_and_says_so(roots):
    _install(roots, FakeRunner())
    report = uninstall(roots, FakeRunner(), purge=True)
    assert not (roots.state / "openrackscreen").exists()
    assert "re-approv" in report.warnings_text().lower()


def test_the_reports_are_actually_hashable(roots):
    """Both report dataclasses are `frozen=True`, which makes a dataclass
    hashable by default -- but a `list` field breaks that promise at
    `hash()` time rather than at the class statement, so nothing before this
    test would have noticed a `TypeError` there."""
    report = _install(roots, FakeRunner())
    hash(report)
    hash(uninstall(roots, FakeRunner(), purge=True))


def test_uninstall_never_reverts_config_txt(roots):
    """Disabling SPI is not obviously desirable, and the backup is on disk with
    a name that says where it came from."""
    (roots.boot / "firmware").mkdir()
    (roots.boot / "firmware" / "config.txt").write_text("# nothing\n")
    _install(roots, FakeRunner())
    uninstall(roots, FakeRunner(), purge=True)
    assert "dtparam=spi=on" in (roots.boot / "firmware" / "config.txt").read_text()
