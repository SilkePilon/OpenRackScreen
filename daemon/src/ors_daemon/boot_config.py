"""Turning both SPI buses on, in the one file where getting it wrong costs a
card reader.

The single most common reason every panel comes up `unavailable` is that SPI1
was never enabled, and it is invisible from software: `/dev/spidev0.*` exists,
the daemon opens what it can, and the two panels on the auxiliary block are
simply not there. Nothing reports why.

Two more things make `config.txt` unusually dangerous to edit by machine, and
both are handled below rather than assumed away:

* It supports conditional filter sections -- `[all]`, `[pi3]`, `[pi4]`,
  `[cm4]`, `[none]`, `[EDID=...]` -- and a setting applies only until the next
  filter header. A tool that reads the file blind to sections can report a
  line as "already there" when it is only active on a different model, or
  append its own lines where a stray `[pi3]`/`[none]` at EOF makes them dead
  on arrival. See `_already_has` and the `[all]` reset in `enable_spi`.
* A file that fails to parse is a Pi that does not boot, and the only
  recovery is another computer and the card in your hand. See `_write_config`
  and `_create_backup` for what makes the write and the backup safe.
"""

from __future__ import annotations

import difflib
import os
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

    `backup` names whichever file the backup actually landed in. It is
    usually `<config.txt>.ors-<now>`, but `_create_backup` appends a counter
    (`.1`, `.2`, ...) rather than overwrite a backup that is already there, so
    a second `install` run inside the same `now` still gets one.
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
    """Whether `setting` is already active and unconditional.

    `config.txt` sections change what everything under them means: a setting
    inside `[pi3]` or `[EDID=...]` applies only to a machine matching that
    filter, not to whatever booted this file. A line the scan finds under any
    filter other than `[all]` -- including `[none]`, a model filter, or an
    EDID match -- does not count as present, or a Pi 5 whose file happens to
    end with a leftover `[pi3]` block would be reported as already configured
    while SPI stays off for it. Content before the first header applies the
    same way `[all]` does; there is nothing else it could mean, and starting
    `unconditional` at `True` gives that for free. A header is a stripped
    line that starts with `[` and ends with `]`.

    `dtoverlay=spi1-2cs` counts as present when any `dtoverlay=spi1-` line is
    there (under an unconditional section): somebody running `spi1-3cs` wired
    a third chip select, and appending ours would fight theirs.

    A commented-out line is not present -- `#dtparam=spi=on` is the state a
    Pi ships in, and reading it as enabled is how a config.txt that looks
    right produces four unavailable panels. The name comparison below is what
    actually enforces that: `"#dtparam=spi=on".partition("=")` yields the name
    `"#dtparam"`, which can never equal `"dtparam"` or `"dtoverlay"`, no
    matter what the `#` check does. The explicit `stripped.startswith("#")`
    guard is a second, deliberately redundant gate kept for a future edit that
    loosens the name comparison (e.g. to `key in name`) and would otherwise
    silently start matching comments too.
    """
    key = setting.split("=")[0]
    prefix = "spi1-" if setting.startswith("dtoverlay=spi1-") else None
    unconditional = True
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            filter_name = stripped[1:-1].strip().lower()
            unconditional = filter_name == "all"
            continue
        if not unconditional:
            continue
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


def _create_backup(path: Path, now: str, data: bytes) -> Path:
    """Copy `data` into a fresh file beside `path`, never overwriting one that
    is already there.

    `shutil.copy2` overwrites unconditionally, which loses the one copy of
    the pristine file in two ways: a second `install` run inside the same
    `now` string finds the backup path already occupied by the *previous*
    backup and clobbers it, and a symlink planted at the backup path gets
    followed and overwritten too. `O_EXCL` refuses to open a path that exists
    at all (a plain file or a symlink), `O_NOFOLLOW` refuses to follow a
    symlink even so, and on `FileExistsError` a counter suffix is tried
    instead of giving up, so re-running `install` twice in the same second
    still produces a backup rather than silently skipping it.
    """
    base = f"{path.name}.ors-{now}"
    suffix = 0
    while True:
        name = base if suffix == 0 else f"{base}.{suffix}"
        candidate = path.with_name(name)
        try:
            descriptor = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            suffix += 1
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def _write_config(path: Path, text: str) -> None:
    """Replace `path` with `text` without ever leaving it half-written.

    Mirrors `write_status` in `status.py`, for the same reason spelled out
    there: `write_text` truncates the target first and writes the new bytes
    after, and an `OSError` between those two steps -- a full `/boot`
    partition, which the backup written immediately before this can itself
    cause, since it lands in the same small vfat filesystem -- leaves the file
    this Pi boots from empty. Instead the new content goes into a temporary
    file created beside the target (a rename is only atomic within one
    filesystem), is flushed and `fsync`ed, and only then replaces `path` with
    `os.replace`. Whatever fails, `path` still names either the complete old
    file or the complete new one, never something in between.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        # `path` is untouched -- that is the whole point of writing beside it
        # -- so all that is left is not to leave litter the next run would
        # have to reason about.
        temporary.unlink(missing_ok=True)
        raise


def enable_spi(boot_root: Path, now: str) -> EnableResult:
    """Add the SPI lines this project needs, once, behind a backup.

    `now` is passed in rather than read from a clock so the backup's name is
    decided by the caller and asserted by the tests without freezing time.
    """
    path = find_config(boot_root)
    if path is None:
        return EnableResult(path=None, added=(), backup=None, diff="")

    original_bytes = path.read_bytes()
    # `errors="surrogateescape"` rather than the locale encoding: `install`
    # runs under `sudo`, whose environment is scrubbed, and a `config.txt`
    # comment that happens to be Latin-1 (or anything else that is not valid
    # UTF-8) must not abort a command whose entire job is making SPI usable.
    # Written back out with the same codec, so a byte this process cannot
    # interpret is carried through unchanged rather than replaced or dropped.
    original = original_bytes.decode("utf-8", errors="surrogateescape")
    lines = original.splitlines()
    missing = tuple(line for line in SPI_LINES if not _already_has(lines, line))
    if not missing:
        # No backup on a no-op: `install` is safe to re-run and is the upgrade
        # path, so a copy per run would fill /boot with them.
        return EnableResult(path=path, added=(), backup=None, diff="")

    # Appended to the original string, not rebuilt through
    # `splitlines()`/`"\n".join(...)`: that round trip silently rewrites every
    # CRLF line ending in the file into a bare LF (harmless to the firmware,
    # but the diff below is computed from the same text, so it would then
    # claim only the SPI lines changed while every line's ending did), and
    # `splitlines()` also breaks on separators the firmware does not treat as
    # line breaks (`\x0b`, `\x1c`-`\x1e`, `\x85`, ...), which would land a
    # stray fragment at column 0. Appending to the raw string instead leaves
    # every byte that was already there untouched.
    #
    # The `[all]` line resets whatever filter section the file's last line
    # left active before the SPI lines are appended. `config.txt` sections
    # apply until the next header; appending after a `[pi3]` or `[none]`
    # block without this would add lines a Pi 4/5 never reads while `install`
    # still reports success and every panel comes up `unavailable`. `[all]`
    # is the documented reset and a no-op on a file with no sections at all.
    suffix = "" if original.endswith("\n") or not original else "\n"
    text = (
        original
        + suffix
        + "\n[all]\n# Added by `ors-daemon install`.\n"
        + "\n".join(missing)
        + "\n"
    )

    backup = _create_backup(path, now, original_bytes)
    _write_config(path, text)

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
