"""The one step in `install` whose failure mode is an SD reader.

Everything else this command does can be undone from a shell on the machine.
A `config.txt` that does not parse is a Pi that does not boot, and the only
recovery is another computer and the card in your hand.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from ors_daemon.boot_config import SPI_LINES, enable_spi, find_config

NOW = "2026-08-16T12:00:00"
"""Passed in rather than read from a clock, so the backup's name is a fact the
test states rather than one it has to discover."""


def _boot(tmp_path: Path, relative: str, text: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _boot_bytes(tmp_path: Path, relative: str, data: bytes) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
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
    # Pinned as a behaviour: `_create_backup` opens with an explicit `0o600`
    # rather than inheriting whatever `shutil.copy2` would have preserved
    # from the source. `stat.S_IMODE` masks off the file-type bits `st_mode`
    # also carries, so this compares only the permission bits.
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600


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


@pytest.mark.parametrize(
    ("commented", "expected"),
    [
        ("#dtparam=spi=on", "dtparam=spi=on"),
        ("# dtparam=spi=on", "dtparam=spi=on"),
        ("  #dtparam=spi=on", "dtparam=spi=on"),
        ("#dtoverlay=spi1-2cs", "dtoverlay=spi1-2cs"),
        # No bare "#" case: a line containing only "#" has no "=" in it at
        # all, so `_already_has` excludes it via the `"=" not in stripped`
        # clause alone, before the comment guard or the name comparison ever
        # come into play -- the same code path an *empty* file takes. Every
        # case kept here contains a real "key=value" shape under the "#" so
        # the exclusion is actually forced through the name comparison, the
        # thing this test exists to pin.
    ],
)
def test_a_commented_out_line_does_not_count_as_present(tmp_path, commented, expected):
    """`#dtparam=spi=on` is the state a Pi ships in. Treating it as enabled is
    how every panel comes up unavailable with a config.txt that looks right.

    Pinned as a behaviour, not a branch: for every one of these inputs the
    name comparison in `_already_has` (not the `stripped.startswith("#")`
    guard) is what actually excludes the line -- a commented name never
    equals the bare key. The explicit `#` guard is kept anyway as deliberate
    defense-in-depth against a future edit that loosens the name comparison,
    but no single input here isolates it, so this parametrization locks in
    the outcome the guard exists to protect rather than the clause that
    happens to produce it today.
    """
    _boot(tmp_path, "firmware/config.txt", f"{commented}\n")
    result = enable_spi(tmp_path, NOW)
    assert expected in result.added


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


# --- HIGH 1: appended lines must not land inside a trailing filter section --


def test_appended_lines_land_under_an_unconditional_all_filter(tmp_path):
    """A file ending inside a model filter must not silently swallow the
    appended lines -- on a Pi 4/5 that leaves SPI off while the tool reports
    success."""
    _boot(
        tmp_path,
        "firmware/config.txt",
        "[all]\ndtparam=audio=on\n\n[pi3]\narm_freq=1200\n",
    )
    result = enable_spi(tmp_path, NOW)
    text = (tmp_path / "firmware/config.txt").read_text()

    added_at = text.index("dtparam=spi=on")
    reset_at = text.rindex("[all]", 0, added_at)
    # No filter header between the reset `[all]` and the appended lines.
    assert "[pi3]" not in text[reset_at:added_at]
    assert result.added == SPI_LINES


def test_appended_lines_land_under_all_even_when_the_file_ends_in_none(tmp_path):
    """`[none]` is the harshest case: lines under it are dead by definition."""
    _boot(tmp_path, "firmware/config.txt", "dtparam=audio=on\n\n[none]\nsome=setting\n")
    result = enable_spi(tmp_path, NOW)
    text = (tmp_path / "firmware/config.txt").read_text()

    added_at = text.index("dtparam=spi=on")
    reset_at = text.rindex("[all]", 0, added_at)
    assert "[none]" not in text[reset_at:added_at]
    assert result.added == SPI_LINES


# --- HIGH 2: `_already_has` must not count a filtered line as present ------


def test_a_line_only_present_under_a_model_filter_is_not_counted_as_present(tmp_path):
    """SPI set only inside `[pi3]` does nothing on a Pi 5; the tool must add
    its own unconditional copy rather than reporting nothing to do."""
    _boot(tmp_path, "firmware/config.txt", "[pi3]\ndtparam=spi=on\ndtoverlay=spi1-2cs\n")
    result = enable_spi(tmp_path, NOW)
    assert result.added == SPI_LINES


def test_a_line_under_none_is_not_counted_as_present(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "[none]\ndtparam=spi=on\ndtoverlay=spi1-2cs\n")
    result = enable_spi(tmp_path, NOW)
    assert result.added == SPI_LINES


def test_a_line_before_the_first_filter_header_counts_as_present(tmp_path):
    """Content before any header is unconditional, the same as `[all]`."""
    _boot(
        tmp_path,
        "firmware/config.txt",
        "dtparam=spi=on\ndtoverlay=spi1-2cs\n[pi3]\narm_freq=1200\n",
    )
    result = enable_spi(tmp_path, NOW)
    assert result.added == ()


def test_a_line_under_an_explicit_all_counts_as_present(tmp_path):
    _boot(tmp_path, "firmware/config.txt", "[all]\ndtparam=spi=on\ndtoverlay=spi1-2cs\n")
    result = enable_spi(tmp_path, NOW)
    assert result.added == ()


def test_a_line_under_an_uppercase_all_header_counts_as_present(tmp_path):
    """`_already_has` lowercases the filter name before comparing it to
    `"all"`, so `[ALL]`/`[All]`/... must be recognised the same as `[all]`.

    This assumes the Pi firmware's own `config.txt` parser is itself
    case-insensitive about `[all]` -- an assumption this test cannot check,
    since nothing here runs on real firmware. If that assumption is ever
    wrong, this test and the `.lower()` it pins would both need to change
    together.
    """
    _boot(tmp_path, "firmware/config.txt", "[ALL]\ndtparam=spi=on\ndtoverlay=spi1-2cs\n")
    result = enable_spi(tmp_path, NOW)
    assert result.added == ()


def test_a_second_run_after_the_all_reset_finds_nothing_to_do(tmp_path):
    """HIGH 1 and HIGH 2 combined: a run that adds lines under a fresh
    `[all]` must be recognised as already-present by the very next run, or
    the tool can never converge."""
    _boot(
        tmp_path,
        "firmware/config.txt",
        "[all]\ndtparam=audio=on\n\n[pi3]\narm_freq=1200\n",
    )
    enable_spi(tmp_path, NOW)
    second = enable_spi(tmp_path, "2026-08-16T13:00:00")
    assert second.added == ()


# --- HIGH 3: the write must be atomic ---------------------------------------


def test_the_config_file_is_replaced_and_never_truncated(tmp_path, monkeypatch):
    """A write failure between the temp file and the rename must not leave
    the file this Pi boots from empty or partial -- unlike a plain
    `write_text`, which truncates the target before the new bytes ever land.
    """
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    original_bytes = path.read_bytes()

    def die(source, target):  # noqa: ANN001
        raise OSError("the power went")

    monkeypatch.setattr(os, "replace", die)
    with pytest.raises(OSError):
        enable_spi(tmp_path, NOW)

    assert path.read_bytes() == original_bytes, "config.txt must be byte-identical"
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == [], (
        "no temporary file left behind"
    )


def test_write_does_not_follow_a_symlink_planted_at_the_temp_path(tmp_path):
    """`_write_config`'s temp-file open needs the same `O_NOFOLLOW` guard
    `_create_backup` already has. Without it, a symlink planted at
    `.config.txt.tmp` gets written *through* into whatever it points at, and
    the `os.replace` at the end -- which moves the temp path itself, not
    its target -- then installs that symlink over `path`: the live boot
    file becomes a symlink to an attacker-chosen path. `O_NOFOLLOW` turns
    the open into an `OSError` (`ELOOP`) instead, before any of that can
    happen.
    """
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    target = tmp_path / "attacker-target.txt"
    target.write_text("untouched\n")
    (path.parent / ".config.txt.tmp").symlink_to(target)

    with pytest.raises(OSError):
        enable_spi(tmp_path, NOW)

    assert target.read_text() == "untouched\n", "the symlink's target must not be written through"
    assert not path.is_symlink(), "config.txt must never become a symlink"
    assert path.read_text() == "# original\n"


# --- MEDIUM 4: the rewrite must not decompose bytes it does not understand -


def test_the_rewrite_does_not_decompose_the_original_bytes(tmp_path):
    """Appending must not go through `splitlines()`/`"\\n".join(...)`, which
    silently rewrites every CRLF line ending in the file and can split on
    separators the firmware does not treat as line breaks."""
    original = b"dtparam=audio=on\r\ndtparam=other=on\r\n"
    path = _boot_bytes(tmp_path, "firmware/config.txt", original)
    enable_spi(tmp_path, NOW)
    assert path.read_bytes().startswith(original)


def test_the_diff_does_not_claim_untouched_lines_changed(tmp_path):
    """The diff is the entire safety story for this command -- it must not
    tell the user three lines were added while every line's ending was
    silently rewritten."""
    original = b"dtparam=audio=on\r\n"
    path = _boot_bytes(tmp_path, "firmware/config.txt", original)
    result = enable_spi(tmp_path, NOW)
    assert "-dtparam=audio=on" not in result.diff
    # An empty diff would also satisfy the assertion above, without proving
    # the diff says anything at all -- this pins that it actually names what
    # was added.
    assert "+dtparam=spi=on" in result.diff
    assert path.read_bytes().startswith(original)


# --- MEDIUM 5: the backup must never overwrite a file that is already there


def test_a_backup_that_already_exists_is_not_overwritten(tmp_path):
    """A second run inside the same `now`, or a planted file at the backup
    path, must not cost the one pristine copy of `config.txt`."""
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    existing_backup = path.parent / f"config.txt.ors-{NOW}"
    existing_backup.write_text("# do not touch\n")

    result = enable_spi(tmp_path, NOW)

    assert existing_backup.read_text() == "# do not touch\n"
    assert result.backup == path.parent / f"config.txt.ors-{NOW}.1"
    assert result.backup.read_text() == "# original\n"


def test_a_backup_path_that_is_a_symlink_is_not_followed(tmp_path):
    """The other half of the finding above: a symlink planted at the backup
    path, not just a plain file. `O_EXCL` refuses to open a path that
    already exists at all -- symlink included -- so the symlink's target
    must come out untouched, exactly like the plain-file case, and the real
    backup must still land under the counter-suffixed name.
    """
    path = _boot(tmp_path, "firmware/config.txt", "# original\n")
    target = tmp_path / "elsewhere.txt"
    target.write_text("do not touch\n")
    planted = path.parent / f"config.txt.ors-{NOW}"
    planted.symlink_to(target)

    result = enable_spi(tmp_path, NOW)

    assert target.read_text() == "do not touch\n", "the symlink's target must be untouched"
    assert planted.is_symlink(), "the planted symlink itself must be untouched"
    assert result.backup == path.parent / f"config.txt.ors-{NOW}.1"
    assert result.backup.read_text() == "# original\n"
    assert not result.backup.is_symlink()


# --- MEDIUM 6: the file must be read and written with a fixed encoding -----


def test_a_non_utf8_comment_does_not_crash_the_install(tmp_path):
    """`install` runs under `sudo`, whose environment is scrubbed, so relying
    on the locale encoding is an avoidable abort waiting to happen."""
    original = b"# caf\xe9 comment\n"
    path = _boot_bytes(tmp_path, "firmware/config.txt", original)

    result = enable_spi(tmp_path, NOW)

    assert result.added == SPI_LINES
    assert original in path.read_bytes()


# --- LOW 7: the marker text is part of the contract -------------------------


def test_the_added_lines_are_marked_with_the_install_command(tmp_path):
    """Hardcoded rather than compared against a constant imported from the
    module under test: if the implementation's marker text ever changes,
    this must fail, not pass along with it."""
    _boot(tmp_path, "firmware/config.txt", "# original\n")
    enable_spi(tmp_path, NOW)
    text = (tmp_path / "firmware/config.txt").read_text()
    assert "# Added by `ors-daemon install`.\n" in text
