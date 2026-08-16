"""The two subcommands that change a machine, and the one thing they check first."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ors_daemon.__main__ import main
from ors_daemon.install import Roots


def test_install_without_root_refuses_and_changes_nothing(monkeypatch, capsys, tmp_path):
    """Exit 2, the conventional "you typed it wrong" code, with no partial
    state: a half-done install is worse than none, because the next thing it
    would have done is the thing that reports what went wrong."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    code = main(["install", "--prefix", str(tmp_path / "opt")])
    assert code == 2
    captured = capsys.readouterr()
    assert "root" in (captured.err + captured.out).lower()
    assert not (tmp_path / "opt").exists()


def test_uninstall_without_root_refuses(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
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


def _fake_roots(tmp_path: Path, prefix: Path) -> Roots:
    for name in ("etc", "boot", "state", "systemd"):
        (tmp_path / name).mkdir(exist_ok=True)
    return Roots(
        etc=tmp_path / "etc",
        boot=tmp_path / "boot",
        state=tmp_path / "state",
        prefix=prefix,
        systemd=tmp_path / "systemd",
    )


def _as_root_with_fake_machine(monkeypatch, tmp_path: Path) -> None:
    """The two patches every test past the root check needs."""
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "ors_daemon.__main__._real_roots",
        lambda prefix: _fake_roots(tmp_path, prefix),
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
    capsys.readouterr()
    assert "dtparam=spi=on" not in config_txt.read_text()


def test_uninstall_prints_something(monkeypatch, capsys, tmp_path):
    _as_root_with_fake_machine(monkeypatch, tmp_path)
    code = main(["uninstall"])
    assert code == 0
    assert capsys.readouterr().out
