"""Turning both SPI buses on, in the one file where getting it wrong costs a
card reader.

The single most common reason every panel comes up `unavailable` is that SPI1
was never enabled, and it is invisible from software: `/dev/spidev0.*` exists,
the daemon opens what it can, and the two panels on the auxiliary block are
simply not there. Nothing reports why.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

SPI_LINES = ("dtparam=spi=on", "dtoverlay=spi1-2cs")
"""Both buses, in the order `daemon/README.md` documents them.

`dtparam=spi=on` is SPI0: CE0 on GPIO8 and CE1 on GPIO7, panels 1 and 2.
`dtoverlay=spi1-2cs` is the auxiliary block: CE0 on GPIO18 and CE1 on GPIO17,
panels 3 and 4. A rack with four panels needs both; a rack with two needs the
first, and the second costs it nothing."""

CANDIDATES = ("firmware/config.txt", "config.txt")
"""Bookworm moved it. In this order, so a machine that has both -- an upgrade
that left the old file behind -- gets the one the firmware actually reads."""


@dataclass(frozen=True)
class EnableResult:
    """What `enable_spi` did, in enough detail to print.

    `path is None` means the machine has no `config.txt` at all, which is not
    an error: installing the daemon on an x86 box to drive virtual panels is
    supported, and `render` exists for exactly that.
    """

    path: Path | None
    added: tuple[str, ...]
    backup: Path | None
    diff: str


def find_config(boot_root: Path) -> Path | None:
    for relative in CANDIDATES:
        candidate = boot_root / relative
        if candidate.is_file():
            return candidate
    return None


def _already_has(lines: list[str], setting: str) -> bool:
    # `dtoverlay=spi1-2cs` counts as present when any `dtoverlay=spi1-` line is
    # there: somebody running `spi1-3cs` wired a third chip select, and
    # appending ours would fight theirs. A commented-out line is not present --
    # `#dtparam=spi=on` is the state a Pi ships in, and reading it as enabled is
    # how a config.txt that looks right produces four unavailable panels.
    key = setting.split("=")[0]
    prefix = "spi1-" if setting.startswith("dtoverlay=spi1-") else None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != key:
            continue
        if prefix is not None and value.strip().startswith(prefix):
            return True
        if prefix is None and stripped == setting:
            return True
    return False


def enable_spi(boot_root: Path, now: str) -> EnableResult:
    """Add the SPI lines this project needs, once, behind a backup.

    `now` is passed in rather than read from a clock so the backup's name is
    decided by the caller and asserted by the tests without freezing time.
    """
    path = find_config(boot_root)
    if path is None:
        return EnableResult(path=None, added=(), backup=None, diff="")

    original = path.read_text()
    lines = original.splitlines()
    missing = tuple(line for line in SPI_LINES if not _already_has(lines, line))
    if not missing:
        # No backup on a no-op: `install` is safe to re-run and is the upgrade
        # path, so a copy per run would fill /boot with them.
        return EnableResult(path=path, added=(), backup=None, diff="")

    updated = lines + ["", "# Added by `ors-daemon install`.", *missing]
    # `splitlines` dropped whatever the file ended with; this puts a newline
    # back deliberately. Without it a file with no trailing newline gets
    # `dtparam=audio=ondtparam=spi=on`, which is one broken line and a Pi with
    # neither setting.
    text = "\n".join(updated) + "\n"

    backup = path.with_name(f"{path.name}.ors-{now}")
    shutil.copy2(path, backup)
    path.write_text(text)

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            text.splitlines(),
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
        )
    )
    return EnableResult(path=path, added=missing, backup=backup, diff=diff)
