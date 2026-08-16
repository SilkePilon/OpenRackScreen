"""The one step in `install` whose failure mode is an SD reader.

Everything else this command does can be undone from a shell on the machine.
A `config.txt` that does not parse is a Pi that does not boot, and the only
recovery is another computer and the card in your hand.
"""

from __future__ import annotations

from pathlib import Path

from ors_daemon.boot_config import SPI_LINES, enable_spi, find_config

NOW = "2026-08-16T12:00:00"
"""Passed in rather than read from a clock, so the backup's name is a fact the
test states rather than one it has to discover."""


def _boot(tmp_path: Path, relative: str, text: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_it_prefers_the_bookworm_path(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "# firmware\n")
    _boot(tmp_path, "config.txt", "# legacy\n")
    assert find_config(tmp_path) == tmp_path / "firmware/config.txt"


def test_it_falls_back_to_the_pre_bookworm_path(tmp_path):
    _boot(tmp_path, "config.txt", "# legacy\n")
    assert find_config(tmp_path) == tmp_path / "config.txt"


def test_a_machine_that_is_not_a_pi_is_not_an_error(tmp_path):
    """Installing the daemon on an x86 box to drive virtual panels is a real
    thing this project supports -- `render` and the virtual display exist for
    it -- and it must not fail at a file only a Pi has."""
    result = enable_spi(tmp_path, NOW)
    assert result.path is None
    assert result.added == ()
    assert result.backup is None


def test_it_adds_both_lines_to_a_config_that_has_neither(tmp_path):
    path = _boot(tmp_path, "firmware/config.txt", "# a comment\ndtparam=audio=on\n")
    result = enable_spi(tmp_path, NOW)

    assert result.added == SPI_LINES
    text = path.read_text()
    for line in SPI_LINES:
        assert f"\n{line}\n" in text
    # Nothing that was there before is gone. The whole file is rewritten, so
    # this is the assertion that catches a rewrite that drops what it did not
    # recognise.
    assert "dtparam=audio=on" in text
    assert "# a comment" in text


def test_it_backs_the_file_up_before_touching_it(tmp_path):
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    result = enable_spi(tmp_path, NOW)

    assert result.backup == path.parent / f"config.txt.ors-{NOW}"
    assert result.backup.read_text() == "# original\n"


def test_a_second_run_changes_nothing_and_takes_no_backup(tmp_path):
    """Idempotent, because `install` is documented as safe to re-run and is the
    upgrade path. A backup per run would fill /boot with copies."""
    _boot(tmp_path, "firmware/config.txt", "# a comment\n")
    enable_spi(tmp_path, NOW)
    after_first = (tmp_path / "firmware/config.txt").read_text()

    second = enable_spi(tmp_path, "2026-08-16T13:00:00")

    assert second.added == ()
    assert second.backup is None
    assert (tmp_path / "firmware/config.txt").read_text() == after_first
    assert not (tmp_path / "firmware/config.txt.ors-2026-08-16T13:00:00").exists()


def test_an_existing_line_that_differs_is_left_exactly_as_written(tmp_path):
    """Somebody who tuned their overlay meant it. Overwriting a deliberate
    `spi1-3cs` with our `spi1-2cs` silently removes a chip select they wired."""
    _boot(tmp_path, "firmware/config.txt", "dtoverlay=spi1-3cs\n")
    result = enable_spi(tmp_path, NOW)

    text = (tmp_path / "firmware/config.txt").read_text()
    assert "dtoverlay=spi1-3cs" in text
    assert "dtoverlay=spi1-2cs" not in text
    assert result.added == ("dtparam=spi=on",)


def test_a_commented_out_line_does_not_count_as_present(tmp_path):
    """`#dtparam=spi=on` is the state a Pi ships in. Treating it as enabled is
    how every panel comes up unavailable with a config.txt that looks right."""
    _boot(tmp_path, "firmware/config.txt", "#dtparam=spi=on\n")
    result = enable_spi(tmp_path, NOW)
    assert "dtparam=spi=on" in result.added


def test_the_diff_names_only_what_changed(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "dtparam=audio=on\n")
    result = enable_spi(tmp_path, NOW)
    assert "+dtparam=spi=on" in result.diff
    assert "+dtoverlay=spi1-2cs" in result.diff
    assert "-dtparam=audio=on" not in result.diff


def test_a_file_with_no_trailing_newline_does_not_lose_its_last_line(tmp_path):
    """`dtparam=audio=ondtparam=spi=on` is one broken line, and the Pi that
    boots from it has no audio and no SPI."""
    _boot(tmp_path, "firmware/config.txt", "dtparam=audio=on")
    enable_spi(tmp_path, NOW)
    lines = (tmp_path / "firmware/config.txt").read_text().splitlines()
    assert "dtparam=audio=on" in lines
    assert "dtparam=spi=on" in lines
