"""The two subcommands that change a machine, and the one thing they check first."""

from __future__ import annotations

from ors_daemon.__main__ import main


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
