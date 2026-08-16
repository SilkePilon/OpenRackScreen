"""The two subcommands that change a machine, and the one thing they check first."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ors_daemon.__main__ import _parser, _real_roots, main
from ors_daemon.install import Roots


def _explode_if_reached(monkeypatch):
    """Wire `_real_roots` and `_SubprocessRunner` to blow up if either is ever
    reached, and assert on the *shape* of the root guard rather than on a
    filesystem side effect.

    `_uninstall` evaluates `_real_roots(...)` as the first argument of its
    call to `uninstall()`, and `main` has no broad `except Exception` around
    dispatch -- so if the `os.geteuid() != 0` guard in `main` is ever removed
    or short-circuited, one of these two fires and the `AssertionError`
    propagates out of `main` uncaught, failing the test loudly instead of
    quietly touching the real machine.

    This is strictly stronger than asserting a `tmp_path` directory was never
    created: the real damage a broken guard does lands in
    `/etc/systemd/system` and `/etc/openrackscreen`, not under `tmp_path`, so
    a check confined to `tmp_path` proves nothing about the guard at all.
    """

    class Boom:
        def run(self, argv: list[str]) -> int:
            raise AssertionError(f"runner reached: {argv}")

    monkeypatch.setattr(
        "ors_daemon.__main__._real_roots",
        lambda prefix: (_ for _ in ()).throw(AssertionError(f"_real_roots reached: {prefix}")),
    )
    monkeypatch.setattr("ors_daemon.__main__._SubprocessRunner", Boom)


def test_install_without_root_refuses_and_changes_nothing(monkeypatch, capsys, tmp_path):
    """Exit 2, the conventional "you typed it wrong" code, with no partial
    state: a half-done install is worse than none, because the next thing it
    would have done is the thing that reports what went wrong."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    _explode_if_reached(monkeypatch)
    code = main(["install", "--prefix", str(tmp_path / "opt")])
    assert code == 2
    captured = capsys.readouterr()
    assert "root" in (captured.err + captured.out).lower()
    assert not (tmp_path / "opt").exists()


def test_uninstall_without_root_refuses(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    _explode_if_reached(monkeypatch)
    assert main(["uninstall"]) == 2


def test_install_help_names_every_flag(capsys):
    # `main` catches the `SystemExit` argparse raises for `--help` in the same
    # `try` that catches it for a bad flag -- see
    # `daemon/src/ors_daemon/__main__.py:192-199` -- so `--help` never escapes
    # `main` as `SystemExit`; it comes back as a plain `0`.
    code = main(["install", "--help"])
    assert code == 0
    text = capsys.readouterr().out
    for flag in ("--no-spi", "--prefix", "--use-current-interpreter", "--upgrade"):
        assert flag in text


def test_uninstall_help_says_what_purge_costs(capsys):
    code = main(["uninstall", "--help"])
    assert code == 0
    text = capsys.readouterr().out
    assert "--purge" in text
    assert "approv" in text.lower()


# -- pure, no root and no filesystem ---------------------------------------


def test_real_roots_names_the_actual_machine_paths():
    """`_real_roots` is the one function in this module that decides where a
    real `install`/`uninstall` lands -- every test below patches it away, so
    nothing else pins what it actually returns. Pure: reads and writes
    nothing."""
    assert _real_roots(Path("/x")) == Roots(
        etc=Path("/etc"),
        boot=Path("/boot"),
        state=Path("/var/lib"),
        prefix=Path("/x"),
        systemd=Path("/etc/systemd/system"),
    )


def test_install_prefix_defaults_to_opt_openrackscreen():
    assert _parser().parse_args(["install"]).prefix == Path("/opt/openrackscreen")


# -- past the root check --------------------------------------------------
#
# Everything below pretends to be root (`os.geteuid` monkeypatched to `0`) to
# reach `_install`/`_uninstall`, and then keeps the sandbox promise the same
# way `daemon/tests/test_install.py` does: `ors_daemon.__main__._real_roots`
# is monkeypatched to a `Roots` built under `tmp_path` instead of `/etc`,
# `/boot`, `/var/lib` and `/etc/systemd/system`, and
# `ors_daemon.__main__._SubprocessRunner` is monkeypatched to `FakeRunner`,
# which records argv and never calls `subprocess.run`. No test in this
# section may touch the real machine.


@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return 0


def _fake_roots(base: Path, prefix: Path) -> Roots:
    for name in ("etc", "boot", "state", "systemd"):
        (base / name).mkdir(exist_ok=True)
    return Roots(
        etc=base / "etc",
        boot=base / "boot",
        state=base / "state",
        prefix=prefix,
        systemd=base / "systemd",
    )


def _as_root_with_fake_machine(monkeypatch, tmp_path: Path, base: Path | None = None) -> None:
    """The two patches every test past the root check needs."""
    base = base if base is not None else tmp_path
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "ors_daemon.__main__._real_roots",
        lambda prefix: _fake_roots(base, prefix),
    )
    monkeypatch.setattr("ors_daemon.__main__._SubprocessRunner", FakeRunner)


def test_install_prints_the_short_code(monkeypatch, capsys, tmp_path):
    """The short code is what a person compares against the web interface
    before approving a rack -- if `install` does not print it, that approval
    gesture has nothing to check against."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["install", "--prefix", str(tmp_path / "prefix")])
    assert code == 0
    out = capsys.readouterr().out
    assert "short code:" in out
    printed_code = out.split("short code:", 1)[1].splitlines()[0].strip()
    assert printed_code  # not empty


def test_install_honours_the_prefix_flag(monkeypatch, capsys, tmp_path):
    """`--prefix` has to reach the venv `install` builds, not the hardcoded
    default -- otherwise every install lands at /opt/openrackscreen no matter
    what an operator asked for."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    prefix = tmp_path / "somewhere-else"
    code = main(["install", "--prefix", str(prefix)])
    assert code == 0
    capsys.readouterr()
    # The `_SubprocessRunner` the CLI actually used is a fresh `FakeRunner`
    # instance created inside `_install`, so it cannot be inspected directly;
    # the unit file it wrote is the durable record of which prefix was used.
    unit_text = (tmp_path / "systemd" / "openrackscreen.service").read_text()
    exec_start = next(line for line in unit_text.splitlines() if line.startswith("ExecStart="))
    assert str(prefix) in exec_start
    assert "/opt/openrackscreen" not in exec_start


def test_install_forwards_no_spi_to_enable_spi_step(monkeypatch, capsys, tmp_path):
    """`--no-spi` has to reach `enable_spi_step=False` -- otherwise the flag
    that exists so an operator can opt out of a config.txt edit does nothing,
    and the edit happens anyway."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    config_txt = tmp_path / "boot" / "firmware" / "config.txt"
    config_txt.parent.mkdir(parents=True)
    config_txt.write_text("# nothing\n")
    code = main(["install", "--no-spi", "--prefix", str(tmp_path / "prefix")])
    assert code == 0
    out = capsys.readouterr().out
    assert "dtparam=spi=on" not in config_txt.read_text()
    assert "SPI: skipped (--no-spi)" in out


def test_uninstall_prints_something(monkeypatch, capsys, tmp_path):
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["uninstall"])
    assert code == 0
    assert capsys.readouterr().out


def test_uninstall_says_the_venv_is_left_behind(monkeypatch, capsys, tmp_path):
    """`--help` used to promise `uninstall` removes "the unit and the venv",
    but `uninstall()` never reads `Roots.prefix` and leaves the venv on disk
    -- the printed confirmation has to say so rather than claim otherwise."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["uninstall"])
    assert code == 0
    out = capsys.readouterr().out
    assert "left on disk" in out


# -- --purge -----------------------------------------------------------------


def test_uninstall_with_purge_removes_the_state_dir(monkeypatch, capsys, tmp_path):
    """`purge=args.purge` mutated to `True` unconditionally would destroy the
    pairing on every plain `uninstall`; mutated to `False` would silently
    spare a pairing the operator explicitly asked to destroy. Both directions
    need a test."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    state_dir = tmp_path / "state" / "openrackscreen"
    state_dir.mkdir(parents=True)
    (state_dir / "identity.json").write_text("{}")

    code = main(["uninstall", "--purge"])

    assert code == 0
    assert not state_dir.exists()
    err = capsys.readouterr().err
    assert "re-approv" in err.lower()


def test_uninstall_without_purge_leaves_the_state_dir(monkeypatch, capsys, tmp_path):
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    state_dir = tmp_path / "state" / "openrackscreen"
    state_dir.mkdir(parents=True)
    (state_dir / "identity.json").write_text("{}")

    code = main(["uninstall"])

    assert code == 0
    assert state_dir.exists()
    assert (state_dir / "identity.json").read_text() == "{}"


# -- the exit code on a failed install -----------------------------------


def test_install_exits_1_when_the_report_is_failed(monkeypatch, capsys, tmp_path):
    """A failed install returning 0 makes `ors-daemon install && reboot`
    proceed on a machine that was never actually configured."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)

    class FailingUseraddRunner(FakeRunner):
        def run(self, argv: list[str]) -> int:
            super().run(argv)
            return 1 if argv[:1] == ["useradd"] else 0

    monkeypatch.setattr("ors_daemon.__main__._SubprocessRunner", FailingUseraddRunner)

    code = main(["install", "--prefix", str(tmp_path / "prefix")])

    assert code == 1
    err = capsys.readouterr().err
    assert "useradd exited 1" in err
    assert "did not finish cleanly" in err


# -- --use-current-interpreter ---------------------------------------------


def test_install_use_current_interpreter_names_that_executable(monkeypatch, capsys, tmp_path):
    """Dropping either `--use-current-interpreter` or its `executable` from
    the unit lands a service that runs from the /opt venv instead of the
    running interpreter the operator explicitly named."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    stub = tmp_path / "etc" / "readable" / "bin" / "ors-daemon"
    stub.parent.mkdir(parents=True)
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(stub)])

    prefix = tmp_path / "prefix"
    code = main(["install", "--use-current-interpreter", "--prefix", str(prefix)])

    assert code == 0
    capsys.readouterr()
    unit_text = (tmp_path / "systemd" / "openrackscreen.service").read_text()
    exec_start = next(line for line in unit_text.splitlines() if line.startswith("ExecStart="))
    assert str(stub) in exec_start
    assert f"{prefix}/bin/ors-daemon" not in exec_start


def test_install_use_current_interpreter_refusal_prints_only_the_warning(
    monkeypatch, capsys, tmp_path
):
    """The early refusal returns from `install()` before anything is touched
    -- no unit, no user, no SPI step -- so the CLI must not print `unit:`,
    `service user:` or an SPI line that describes work that never
    happened."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    bad = tmp_path / "etc" / "private" / "bin" / "ors-daemon"
    bad.parent.mkdir(parents=True)
    bad.write_text("#!/bin/sh\n")
    bad.chmod(0o700)  # not o+rx
    monkeypatch.setattr(sys, "argv", [str(bad)])

    code = main(["install", "--use-current-interpreter", "--prefix", str(tmp_path / "prefix")])

    assert code == 1
    out = capsys.readouterr().out
    assert "unit:" not in out
    assert "service user:" not in out
    assert "SPI" not in out
    assert not (tmp_path / "systemd" / "openrackscreen.service").exists()


def test_use_current_interpreter_refuses_a_python_dash_m_launch(monkeypatch, capsys, tmp_path):
    """`sudo python -m ors_daemon install --use-current-interpreter` puts
    `.../ors_daemon/__main__.py` in `sys.argv[0]` -- a source file, not
    something the service user could execute -- and has to be refused
    outright rather than pointed at with "chmod it", which is advice for a
    real executable."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "src" / "ors_daemon" / "__main__.py")])

    code = main(["install", "--use-current-interpreter", "--prefix", str(tmp_path / "prefix")])

    assert code == 1
    err = capsys.readouterr().err
    assert "-m ors_daemon" in err
    assert not (tmp_path / "systemd" / "openrackscreen.service").exists()


def test_use_current_interpreter_resolves_a_bare_launcher_name_via_path(
    monkeypatch, capsys, tmp_path
):
    """A launcher with no shebang leaves `sys.argv[0]` as a bare name;
    `Path(name).resolve()` would join that to the current working directory
    instead of to wherever `PATH` actually found it."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    stub = tmp_path / "etc" / "readable" / "bin" / "ors-daemon"
    stub.parent.mkdir(parents=True)
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    monkeypatch.setattr(sys, "argv", ["ors-daemon"])
    monkeypatch.setattr(shutil, "which", lambda name: str(stub) if name == "ors-daemon" else None)

    code = main(["install", "--use-current-interpreter", "--prefix", str(tmp_path / "prefix")])

    assert code == 0
    unit_text = (tmp_path / "systemd" / "openrackscreen.service").read_text()
    exec_start = next(line for line in unit_text.splitlines() if line.startswith("ExecStart="))
    assert str(stub) in exec_start


# -- the SPI line -------------------------------------------------------


def test_install_spi_line_says_not_attempted_when_no_config_txt(monkeypatch, capsys, tmp_path):
    """No config.txt anywhere under `roots.boot` means `install()` returns
    `reboot_needed=False` with a warning -- SPI was never touched, which is
    neither "already enabled" nor "no change"."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["install", "--prefix", str(tmp_path / "prefix")])
    assert code == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("SPI:"))
    assert line == "SPI: not attempted (no config.txt found under the boot partition)"


def test_install_prints_reboot_needed_line(monkeypatch, capsys, tmp_path):
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    config_txt = tmp_path / "boot" / "firmware" / "config.txt"
    config_txt.parent.mkdir(parents=True)
    config_txt.write_text("# nothing\n")

    code = main(["install", "--prefix", str(tmp_path / "prefix")])

    assert code == 0
    out = capsys.readouterr().out
    assert "reboot needed: yes" in out


def test_install_prints_warnings_to_stderr(monkeypatch, capsys, tmp_path):
    """Both `install`'s and `uninstall`'s warnings go to stderr, consistently
    -- a warning is a diagnostic, not the confirmation a script would parse
    off stdout."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["install", "--prefix", str(tmp_path / "prefix")])
    assert code == 0
    captured = capsys.readouterr()
    assert "no config.txt was found" in captured.err
    assert "no config.txt was found" not in captured.out


# -- --upgrade is inert -----------------------------------------------------


def test_upgrade_flag_changes_nothing(monkeypatch, capsys, tmp_path):
    """`install()` is already idempotent and documented as its own upgrade
    path; `--upgrade` is parsed but forwarded nowhere, so a fresh install has
    to behave identically with and without it."""
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    prefix = tmp_path / "prefix"

    code_without = main(["install", "--prefix", str(prefix)])
    out_without = capsys.readouterr().out
    assert code_without == 0

    # Reset the two markers `install()` uses to tell a fresh run from a
    # re-run, so the second call starts from the same state as the first --
    # the state dir (and the identity inside it) is left alone, so the short
    # code -- randomly generated only the first time it is created -- stays
    # the same and does not itself become a spurious difference.
    shutil.rmtree(tmp_path / "etc" / "openrackscreen")
    (tmp_path / "systemd" / "openrackscreen.service").unlink()

    code_with = main(["install", "--upgrade", "--prefix", str(prefix)])
    out_with = capsys.readouterr().out
    assert code_with == 0

    assert out_without == out_with
