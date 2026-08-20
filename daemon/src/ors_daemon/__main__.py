"""The daemon's front door: `run`, `connect`, `validate`, `render`, `identify`.

Five commands, one config path, and a deliberate split down the middle of them:
`validate` and `render` touch no hardware at all, so they are what a person runs
on a laptop before a config ever reaches the Pi; `run` and `identify` open
panels, so they are what runs in front of the rack. `connect` touches neither --
it writes the pairing a later `run` dials with.

*Where a running rack's configuration comes from.* `--config` is optional on
`run`: a paired rack's configuration is the server's, not a file an operator
was forced to hand-author, and `install` leaves a machine paired with nothing
yet. Five states follow, and `_boot`'s docstring names them as rows: a pushed
snapshot outranks a config file whenever both exist, because the server is the
source of truth once a rack is paired; the file is the fallback that keeps a
server outage from darkening anything, when one was given; a paired rack with
nothing pushed yet starts with no screens and waits, rather than refusing to
start; a rack that is neither paired nor handed a file browses for a server to
join instead of running at all (`join_a_server`), which is not an error -- it
is the state a freshly installed rack is in; and only when that is also
impossible -- discovery off, no `--server` -- is there truly no way to obtain a
configuration, which is a message on stderr and a non-zero exit rather than a
traceback or a rack that pretends.

Rows 4 and 5 are decided only when there is also no usable cached snapshot:
`load_link_settings` returns `None` for a pairing file it could not read (a
`sudo ors-daemon connect` run under the wrong user, most often) exactly as it
does for one that was never written, and a corrupt pairing is not a reason to
abandon a perfectly good `snapshot.json` sitting beside it. `join_a_server`
blocks until this rack is paired, and `_run` continues in-process from there --
it falls through into `_boot`, the same rows 2/3 a restart would have taken --
rather than exiting and trusting the unit's `Restart=` to bring it back
already paired, which left a rack run by hand printing nothing and staying
dark.

*`render` goes through the renderer, not through a backend.* It is the reason
the shipped `examples/rack.yaml` -- four GC9A01 panels on two SPI buses -- can
be checked on a machine with no luma installed and no rack in the room. A
`--data` file stands in for the poll a laptop cannot make.

*Signals are installed on the main thread, and their handler is `stop`.* See
`_install_signal_handlers`: this is the code path that decides whether four
panels are left lit when the daemon goes away, and everything about it is
arranged so that the second signal is as harmless as the first.

*`identify` runs no loop.* It is the setup wizard's tool, not a mode of the
daemon: see `_identify`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any

from ors_render import RenderContext, render_screen
from ors_schema.daemon import DaemonConfig, DisplayConfig
from ors_schema.link import Command

from ors_daemon import __version__, discovery, identity, join
from ors_daemon.clock import Clock, ClockError, system_clock
from ors_daemon.config import (
    ConfigError,
    ResolvedScreen,
    load_cached_snapshot,
    load_config,
    resolve_screens,
    system_scenes,
)
from ors_daemon.discovery import Found
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.frames import FramePump, FrameStream
from ors_daemon.hardware import SPI_ROOT, detect_handler, probe_handler
from ors_daemon.install import InstallReport, Roots, install, uninstall
from ors_daemon.link import (
    LinkClient,
    LinkError,
    LinkSettings,
    load_link_settings,
    websocket_url,
    write_link_settings,
)
from ors_daemon.logging import setup_logging
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor

log = logging.getLogger(__name__)

DEFAULT_STATUS_PATH = Path("/tmp/ors-status.json")
"""Where `run` writes its status file when nothing says otherwise.

The shipped unit file overrides it with `/run/openrackscreen/status.json`, which
is where it belongs on the Pi: `RuntimeDirectory=` creates that directory owned
by the daemon's own user on every start, and clears it on stop. The default is
`/tmp` because that is the one path a person running the daemon by hand from a
checkout can always write to.
"""

DEFAULT_LINK_PATH = Path("/var/lib/openrackscreen/link.json")
"""Where the pairing lives when nothing says otherwise.

Not beside `--config`, and the difference matters: the config file is a document
an operator edits and copies around, and this one holds the credential that
grants a server the right to reconfigure this rack and draw on its panels. It
belongs in the daemon's own state directory -- `StateDirectory=` in the shipped
unit, which systemd creates 0700 owned by the daemon's user -- and it is written
0600 on top of that.

The snapshot cache sits beside it by default, for the same reason and one more:
the pair are rewritten together when a daemon pairs, and a rename is only atomic
within one filesystem.
"""

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
"""Constrained by argparse rather than handed to `setup_logging` unchecked: it
passes the string to `Logger.setLevel`, which raises on anything else -- and a
typo in a systemd unit would then be a traceback at boot instead of a usage
error the moment the unit was written."""

_USAGE_EXIT = 2
"""The conventional shell exit code for "you typed it wrong", and argparse's."""

_RECLOCK_EXIT = 10
"""`run` returns this, not 0, when a push changed the timezone and stopped the
rack instead of applying it against a stale clock (see `_snapshot_handler`).

Indistinguishable from 0 used to mean indistinguishable from a clean SIGTERM: a
supervisor watching for a non-zero exit -- `Restart=on-failure`, anything that
is not systemd, or nobody at all -- had no way to tell "stopped on purpose" from
"stopped and needs re-clocking" apart. `Restart=always` still does the actual
recovery on the shipped unit; this is for every caller that is not it, most of
all a rack run by hand, which used to print nothing and sit dark with an exit
code that claimed success.

Not 3, which this used to be. `systemd.exec(5)`'s "Process Exit Codes" table
(checked against systemd 259) reserves 1-7 for the LSB convention systemd
itself follows: 1 `EXIT_FAILURE`, 2 `EXIT_INVALIDARGUMENT`, 3
`EXIT_NOTIMPLEMENTED` ("Unimplemented feature"), 4-7 the rest of it, 200+
systemd's own. `systemctl status` and `journalctl` would have rendered a
re-clock stop as `status=3/NOTIMPLEMENTED` -- which in this module reads as
`join_a_server`'s own `NotImplementedError` stub having fired, the one other
thing here that is literally "not implemented". Nothing in this repo used 3;
the collision was with the platform, not with this code. 10 sits in the range
systemd leaves free for a program's own use (9-63 and 79-199; 64-78 is
`sysexits.h`'s own range, also worth staying out of).
"""

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
"""What systemd sends on `stop`, and what a person at a terminal presses."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ors-daemon",
        description="Drive the rack's panels from a local config file.",
    )
    parser.add_argument("--log-level", default="INFO", choices=_LOG_LEVELS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="drive the panels until stopped")
    run.add_argument(
        "--status",
        type=Path,
        default=DEFAULT_STATUS_PATH,
        help=f"where to write the status file (default: {DEFAULT_STATUS_PATH})",
    )
    run.add_argument(
        "--link",
        type=Path,
        default=DEFAULT_LINK_PATH,
        help=f"the pairing written by `connect` (default: {DEFAULT_LINK_PATH}). "
        "Absent means this rack runs from its config file and dials nothing.",
    )
    run.add_argument(
        "--no-discovery",
        action="store_true",
        help="do not browse for a server to join when this rack has nothing else to "
        "run from: no --config, no pairing, and no usable cached snapshot. Refused "
        "in exactly that state unless --server is also given, because that "
        "combination is the one case with no way to obtain a configuration at all -- "
        "it has no effect otherwise, since --config, a pairing or a usable cache "
        "each already make discovery moot.",
    )
    run.add_argument(
        "--server",
        help="dial this server directly instead of browsing for one, when this rack "
        "is neither paired nor given --config",
    )

    connect = subparsers.add_parser("connect", help="pair this rack with a server")
    connect.add_argument("--server", required=True, help="the server's URL, e.g. http://rack:8080")
    connect.add_argument("--token", required=True, help="a pairing token minted in the interface")
    connect.add_argument("--link", type=Path, default=DEFAULT_LINK_PATH, help="where to write it")
    connect.add_argument(
        "--cache",
        type=Path,
        help="where to keep the pushed snapshot (default: snapshot.json beside --link)",
    )
    connect.add_argument(
        "--force",
        action="store_true",
        help="replace an existing pairing. The key it holds cannot be recovered afterwards.",
    )

    subparsers.add_parser("validate", help="check the config and say what it describes")

    render = subparsers.add_parser("render", help="render every screen to PNG, with no hardware")
    render.add_argument("--out", required=True, type=Path, help="directory to write PNGs into")
    render.add_argument(
        "--data",
        type=Path,
        help="JSON object of integration namespaces to render against, "
        'e.g. {"prom": {"cpu": 42.4}}; without it every screen draws `connecting`',
    )

    identify = subparsers.add_parser("identify", help="paint each panel's ordinal on it")
    identify.add_argument(
        "--hold",
        type=float,
        help="seconds to hold the ordinals on the glass (default: until interrupted)",
    )

    install_command = subparsers.add_parser(
        "install", help="set this machine up to run the daemon as a service"
    )
    install_command.add_argument(
        "--no-spi",
        action="store_true",
        help="do not touch /boot/firmware/config.txt. Both SPI buses are enabled by "
        "default, because SPI1 being off is the most common reason every panel comes "
        "up unavailable and nothing reports it.",
    )
    install_command.add_argument(
        "--prefix",
        type=Path,
        default=Path("/opt/openrackscreen"),
        help="where to build the venv the service runs from (default: %(default)s)",
    )
    install_command.add_argument(
        "--use-current-interpreter",
        action="store_true",
        help="point the unit at the ors-daemon already running instead of building a "
        "venv. Refused if the service user could not execute it.",
    )
    install_command.add_argument(
        "--upgrade",
        action="store_true",
        help="changes nothing: install is already idempotent, so a plain `install` "
        "re-run is always the upgrade path. This flag exists to say so at the "
        "command line, not to gate any different behaviour.",
    )

    uninstall_command = subparsers.add_parser(
        "uninstall", help="stop the service and remove the systemd unit"
    )
    uninstall_command.add_argument(
        "--purge",
        action="store_true",
        help="also delete /var/lib/openrackscreen, which holds the pairing and this "
        "rack's identity. That costs a re-approval in the interface.",
    )

    # Added last and to every subparser that has a config to read, so `--config`
    # reads the same wherever it appears rather than being positional in one
    # command and not another. `connect` is the exception and deliberately so:
    # pairing happens before a rack has a configuration, and afterwards the
    # configuration comes from the server -- requiring it here would make the
    # first thing an operator runs the one thing they cannot yet supply.
    # `install` and `uninstall` are exceptions for the same underlying reason:
    # neither reads a rack.yaml, so requiring one here would make the very
    # first command anyone runs on a fresh machine ask for a file that has no
    # reason to exist yet.
    #
    # Not `required=True` here, not any more. It used to be, and that hid the
    # fact that `run` decides its own configuration -- a pushed snapshot
    # outranks the file, and a rack `install` just set up has neither yet, so
    # forcing `--config` here made the state a freshly installed machine is in
    # impossible to start at all. `run`'s own dispatch in `main` says what a
    # missing `--config` means for it; `validate`, `render` and `identify` have
    # no other source and check for its absence themselves, once, in one place
    # rather than three.
    for name, sub in subparsers.choices.items():
        if name not in ("connect", "install", "uninstall"):
            sub.add_argument("--config", type=Path, help="path to rack.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse, load the config, and dispatch. Returns a shell exit code, always.

    Nothing here raises for a user's mistake: a bad flag, an unreadable file, a
    field that does not validate and a timezone the host cannot resolve are all
    a message on stderr and a non-zero return. The audience is someone editing
    YAML over SSH, and a traceback tells them about this program rather than
    about their config.
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits where `main` has promised to return -- for `--help` as
        # well as for a bad flag -- and a caller that gets `SystemExit` out of a
        # function returning `int` has been lied to. `sys.exit(main())` at the
        # bottom of this file puts the same code back on the process.
        return exc.code if isinstance(exc.code, int) else _USAGE_EXIT

    setup_logging(args.log_level)

    # Both before the config is read, and for opposite reasons: `connect` has no
    # config to read, and `run` decides for itself what its configuration is --
    # a pushed snapshot outranks the file, and neither being readable is a
    # different answer from the one below.
    if args.command == "connect":
        return _connect(args)
    if args.command == "run":
        return _run(args)
    if args.command in ("install", "uninstall"):
        # Checked here rather than inside `install.install`, which is a pure
        # function over injected roots and must stay callable from a test
        # that is not root -- which is every test in that suite.
        if os.geteuid() != 0:
            print(f"ors-daemon {args.command} has to run as root.", file=sys.stderr)
            return _USAGE_EXIT
        return _install(args) if args.command == "install" else _uninstall(args)

    if args.config is None:
        # `validate`, `render` and `identify` describe a document, and unlike
        # `run` they have no other source to fall back on -- there is no
        # pushed snapshot to boot from and no pairing to wait on. `--config`
        # used to be `required=True` here and this is the same refusal, said
        # once for all three rather than left to argparse.
        print(f"ors-daemon {args.command}: --config is required", file=sys.stderr)
        return _USAGE_EXIT

    try:
        config = load_config(args.config)
        screens = resolve_screens(config)
        # Resolved for every command, not only the two that use a clock. A
        # config whose timezone the host cannot resolve is a config the daemon
        # will refuse to start on, and `validate` that passed it would be
        # answering a different question than the one it was asked.
        clock = system_clock(config.timezone)
    except (ConfigError, ClockError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"ok: {len(screens)} screen(s), {len(config.integrations)} integration(s)")
        return 0
    if args.command == "render":
        return _render(screens, args.out, args.data)
    # The parser's `choices` is what makes this exhaustive; the assert is how
    # that reaches a reader, and how a sixth subcommand added without a branch
    # here fails loudly instead of quietly driving the rack.
    assert args.command == "identify"
    return _identify(screens, config, clock, args.hold)


def _cache_beside(link: Path) -> Path | None:
    """Where the snapshot cache lives when `--cache` names nothing. None if nowhere.

    Beside the pairing, because the two are rewritten together when a daemon
    pairs and a rename is only atomic within one filesystem.

    None rather than a raise, and this function exists for that one word.
    `--link /`, `--link .` and `--link ""` are paths with no filename, so there
    is no name to put `snapshot.json` beside and `Path.with_name` says so with
    `ValueError` -- not `OSError`, which is what every guard around a path is
    written for. That is exactly M2's `--status /`, which the ledger records as
    an infinite restart loop: `Supervisor.tick` and `LinkClient._write_cache`
    both defend against it by name, and the two derivations here did not. The
    callers differ on what to do about it -- `connect` refuses, `run` boots from
    its config file -- and neither may traceback.
    """
    try:
        return link.with_name("snapshot.json")
    except ValueError:
        return None


def _connect(args: argparse.Namespace) -> int:
    """Write the pairing a later `run` dials with. Opens no socket.

    Deliberately offline. The token is spent on the first connect, by the
    daemon, and a `connect` that dialled would spend it here -- leaving the
    server holding a key this command would then have to write, and a failure
    anywhere in between unrecoverable without minting a new token. Writing the
    file is the whole job; `LinkClient` does the rest and persists what comes
    back.

    Three things are refused rather than written, because each of them is a
    failure that would otherwise only show up as a daemon dialling nothing once
    a backoff, forever, with the reason in its own log:

    *A URL nothing can dial.* `--server rack:8080` names no host, which is the
    typo M2's tunnel already had to learn to reject.

    *An existing pairing, without `--force`.* The file holds the key the server
    minted, the server keeps only its fingerprint, and there is no recovery
    short of minting a new token in the interface. Judged on whether one can be
    *loaded*, not on whether the path exists: a corrupt file holds no working
    key, and refusing over one would leave a rack unpairable for the sake of
    protecting nothing.

    *A path that cannot be written.* Silently carrying on would report a rack as
    paired when the next boot has nothing.

    *Every* successful connect clears the cached snapshot, `--force` or not,
    which is the same decision `LinkClient._paired` makes rather than an extra
    one. The docstring used to say `--force` did it, and the code was right: the
    cache holds a configuration pushed by *some* server, and this command is what
    decides which server this rack answers to. A reboot between here and the
    first successful connect boots from that cache -- and `_boot` hands its
    version to `_link`, which claims it in `Hello.config_version`, so the new
    server can match the number, skip the push it would otherwise send, and leave
    the rack showing the previous server's rack for ever. Deleting a cache costs
    one boot from the config file, which is what the config file is for; keeping
    a wrong one costs the rack.

    That covers the connect allowed through *because the pairing file was
    corrupt* as well, and deliberately: a pairing that cannot be read cannot say
    which server the cache beside it came from, and an unanswerable question
    about a credential is not one to answer optimistically.
    """
    try:
        websocket_url(args.server)
    except LinkError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    path = Path(args.link)
    if load_link_settings(path) is not None and not args.force:
        print(
            f"{path}: this rack is already paired with a server. "
            "Pass --force to replace that pairing; the key it holds cannot be recovered.",
            file=sys.stderr,
        )
        return 1

    cache = Path(args.cache) if args.cache else _cache_beside(path)
    if cache is None:
        print(
            f"{path}: --link has to name a file to write the pairing to, not a directory",
            file=sys.stderr,
        )
        return 1

    try:
        cache.unlink(missing_ok=True)
        write_link_settings(
            path, LinkSettings(server_url=args.server, cache_path=cache, token=args.token)
        )
    except (OSError, ValueError) as exc:
        print(f"cannot write {path}: {exc}", file=sys.stderr)
        return 1

    print(f"paired with {args.server}; the pairing is in {path}")
    _warn_if_the_daemon_will_not_be_able_to_read_it(path)
    return 0


def _warn_if_the_daemon_will_not_be_able_to_read_it(path: Path) -> None:
    """Say something when `connect` is run as root, because the daemon is not.

    `sudo ors-daemon connect` is the natural thing to type -- the pairing lives
    under `/var/lib/openrackscreen`, and the first instinct about a path under
    `/var/lib` is that it needs root -- and it is the one way to unpair a rack
    without touching it. The file lands root-owned and 0600, the shipped unit
    runs the daemon as `User=openrackscreen`, and every read of it from there is
    a `PermissionError`. `load_link_settings` reports that loudly on the daemon's
    side; this is the same fact said at the moment it is created, to the person
    who can still fix it in one command.

    A note rather than a failure: this may be a rack where the daemon really does
    run as root, and the file *has* been written correctly either way.
    """
    geteuid = getattr(os, "geteuid", None)  # not POSIX everywhere; the rack is
    if geteuid is None or geteuid() != 0:
        return
    print(
        f"note: {path} is owned by root. The shipped unit runs the daemon as "
        "User=openrackscreen, which cannot read a root-owned 0600 file -- that rack would "
        f"run unpaired. `chown openrackscreen: {path}`, or run this command as that user.",
        file=sys.stderr,
    )


@dataclass(frozen=True)
class _SubprocessRunner:
    """The real `install.Runner`: every argv goes through `subprocess.run`.

    The only implementation of `install.Runner` this module ships that
    touches the machine it runs on -- everything under `daemon/tests/` uses
    `FakeRunner` instead, which is what keeps `install`'s own test suite off
    the machine running it.
    """

    def run(self, argv: list[str]) -> int:
        """127, the shell's own code for "command not found", for a binary
        that is not there.

        Without this the missing binary is a `FileNotFoundError` out of the
        middle of `install()`, which is the one shape that function is built
        not to produce: it collects a warning for every step that fails and
        reports at the end what really happened, and a traceback skips all of
        that, leaving the rack half-configured with nothing said about it. The
        realistic case is not exotic -- astral's installer puts `uv` in
        `~/.local/bin` and `sudo` resets PATH to `secure_path`, so `sudo
        ors-daemon install` gets past `useradd` (in `/usr/sbin`) and then dies
        on `uv venv` with a traceback that never names `uv`. `README.md`
        promises the opposite for that exact scenario, and
        `--use-current-interpreter` is the documented way past it -- which is
        advice nobody can follow from a traceback that does not say what was
        missing.

        127 rather than 1 because `install()` prints the code it got, and 127
        is the number an operator can look up. It is not otherwise
        distinguished: every non-zero code takes the same warning path.
        """
        try:
            return subprocess.run(argv).returncode
        except FileNotFoundError:
            return 127


def _real_roots(prefix: Path) -> Roots:
    """`Roots` built from the real filesystem, for the CLI's own use.

    Nothing else in this module names `/etc`, `/boot`, `/var/lib` or
    `/etc/systemd/system` -- `install.install` and `install.uninstall` are
    parameterised on `Roots` precisely so that only this one function, called
    from production code and never from a test, has to.
    """
    return Roots(
        etc=Path("/etc"),
        boot=Path("/boot"),
        state=Path("/var/lib"),
        prefix=prefix,
        systemd=Path("/etc/systemd/system"),
    )


def _print_install_report(report: InstallReport, *, no_spi: bool) -> None:
    """The short code, the SPI diff and whether a reboot is owed.

    The short code is the one thing a person standing at the rack reads off
    this terminal and compares against what the web interface shows before
    approving it -- if this function does not print it, that whole approval
    gesture has nothing to check against.

    `no_spi` is `args.no_spi`, taken as a separate parameter rather than
    inferred from `report`: `InstallReport` carries only `reboot_needed`,
    which reads `False` both when SPI was already on and when `--no-spi`
    skipped the step entirely -- two different facts to tell an operator, one
    boolean between them.

    `report.failed` alone does not say which of two shapes a failure is, and
    printing the same way for both would be wrong in one direction or the
    other -- see `InstallReport.refused`. `refused=True` is the early
    `--use-current-interpreter` permission check, which returns before
    anything else is touched, so `unit:`/`service user:`/an SPI line would
    describe work that never happened; only the warnings are true, so only
    the warnings are printed. `refused=False` with `failed=True` is a failure
    discovered mid-run (`useradd`/`usermod` exiting something other than 0 or
    9, say) -- `install()` keeps going past that, so the unit may genuinely
    have been written, `systemctl` genuinely called and the SPI step
    genuinely attempted. Withholding those lines there is exactly the
    operator-hostile shape this function used to have: the person most in
    need of detail, debugging a half-finished install over SSH, would be told
    the least. Everything printed in that branch is something `install()`
    provably ran; nothing here claims a step that a `return` upstream
    prevented from happening.
    """
    if report.short_code:
        print(f"short code: {report.short_code}")
    if report.failed and report.refused:
        # Refused before touching anything -- no directories, no user, no
        # unit, no SPI step ran -- so printing those lines here would report
        # actions that were never taken. "see the warnings above" is the
        # honest summary of the whole of it.
        if report.warnings:
            print(report.warnings_text(), file=sys.stderr)
        print("install did not finish cleanly; see the warnings above.", file=sys.stderr)
        return
    if report.failed:
        print(
            "install: PARTIAL -- it failed partway through. Everything below is real, "
            "not undone, and the state below reflects what actually happened."
        )
    print(f"unit: {report.unit_path}")
    print(f"service user: {'created' if report.created_user else 'already existed'}")
    if no_spi:
        print("SPI: skipped (--no-spi)")
    elif report.reboot_needed:
        print("SPI: enabled, reboot needed")
    elif any(warning.startswith("no config.txt was found") for warning in report.warnings):
        print("SPI: not attempted (no config.txt found under the boot partition)")
    else:
        print("SPI: unchanged (already enabled)")
    print(f"reboot needed: {'yes' if report.reboot_needed else 'no'}")
    if report.warnings:
        print(report.warnings_text(), file=sys.stderr)
    if report.failed:
        print("install did not finish cleanly; see the warnings above.", file=sys.stderr)


def _install(args: argparse.Namespace) -> int:
    """Build the real `Roots` and a real `Runner`, then run `install.install`."""
    executable = None
    if args.use_current_interpreter:
        if sys.argv[0].endswith(".py"):
            # `sudo python -m ors_daemon install --use-current-interpreter`
            # puts `.../ors_daemon/__main__.py` in `sys.argv[0]` -- a source
            # file the service user was never going to be able to execute.
            # `install()` would refuse it too, but with "chmod it", which is
            # advice aimed at a real executable, not a module entry point.
            print(
                "--use-current-interpreter cannot be used with `python -m ors_daemon`: "
                f"{sys.argv[0]} names a source file, not something the service user "
                "could run. Install a console script instead (a venv, `pipx`, or "
                "`uv tool install`), or drop the flag and let install build one.",
                file=sys.stderr,
            )
            return 1
        # `shutil.which` first: a launcher with no shebang (some wrappers, some
        # `exec`-less shell scripts) leaves `sys.argv[0]` as the bare name it
        # was invoked with, and `Path(name).resolve()` would join that to the
        # current working directory instead of to wherever PATH actually found
        # it. The common case -- a real shebang -- already gives an absolute
        # path that `shutil.which` returns unchanged.
        executable = Path(shutil.which(sys.argv[0]) or sys.argv[0])
    report = install(
        _real_roots(args.prefix),
        _SubprocessRunner(),
        version=__version__,
        now=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        enable_spi_step=not args.no_spi,
        use_current_interpreter=args.use_current_interpreter,
        executable=executable,
    )
    _print_install_report(report, no_spi=args.no_spi)
    return 1 if report.failed else 0


def _uninstall(args: argparse.Namespace) -> int:
    """Build the real `Roots` and a real `Runner`, then run `install.uninstall`."""
    report = uninstall(
        _real_roots(Path("/opt/openrackscreen")), _SubprocessRunner(), purge=args.purge
    )
    if report.warnings:
        print(report.warnings_text(), file=sys.stderr)
    print(
        "uninstalled: the service is stopped, disabled and its unit removed. "
        "The venv this installed (if any) is left on disk; remove its prefix "
        "by hand if you want it gone too."
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    """Drive the rack until a signal arrives, taking configuration as it comes.

    The link is started before the loop, because the loop does not return until
    the daemon is stopping, and it is handed `supervisor.stop_event` rather than
    one of its own so that a SIGTERM stops it from *beginning* an apply.

    A link that will not start is logged and stepped over. That is the rule the
    whole module works under -- no failure of the server, of the socket or of the
    network may darken the rack -- and a Pi too short of memory to fork one more
    thread is exactly such a failure: the configuration is already loaded and
    perfectly servable.

    *Where a fresh install's rack goes before it has anything.* Neither paired
    nor handed `--config` is not a state `_boot` answers on its own -- there is
    no pairing to prefer a snapshot from and no file to fall back to -- unless a
    usable cached snapshot is sitting beside the pairing anyway (a corrupt or
    unreadable link.json is not a reason to abandon one: see `_boot_from_cache`).
    Absent that, it is decided here, before `_boot` is ever called, because it is
    not about what to run but about what to do instead of running: row 4 of the
    boot table joins a server, and row 5 -- the one case with truly no way to
    obtain a configuration -- says so and leaves. See `join_a_server`.

    *Row 4 continues in this same process once paired.* `join_a_server` blocks
    until it has written a pairing to `args.link`, and this falls through into
    `_boot` from there rather than returning -- byte-for-byte the rows 2/3 a
    restart would have taken, without needing one. See `join_a_server`'s
    docstring for why that is the contract rather than exiting for systemd's
    `Restart=` to pick back up.
    """
    # Read once and handed on, so the pairing that decides where the cache is
    # and the pairing the link dials with cannot be two different readings of a
    # file somebody is editing.
    settings = load_link_settings(args.link)

    if settings is None and args.config is None:
        # Tried before rows 4/5, not instead of `_boot`'s own cache-first logic
        # duplicated: a rack whose pairing is corrupt or unreadable is not
        # necessarily a rack with nothing to boot from, and the join flow (or
        # row 5's refusal) is not owed to one that still has a usable snapshot.
        booted, _cache_error = _boot_from_cache(args, settings)
        if booted is None:
            if args.no_discovery and not args.server:
                # Row 5: discovery is off and nothing names a server to dial
                # directly, so there is no way left to obtain a configuration
                # at all. stderr and a non-zero exit, not a traceback: the
                # audience is somebody over SSH, and a traceback tells them
                # about this program rather than about their machine.
                print(
                    f"{args.link}: this rack is not paired and no --config was given, so "
                    "there is nothing to run. Pass --config to run it standalone, pair it "
                    "first with `ors-daemon connect`, or drop --no-discovery (or pass "
                    "--server) so it can find a server on its own.",
                    file=sys.stderr,
                )
                return 1
            # Row 4: the state a freshly installed rack is in, and it is not an
            # error. Browse for a server (or dial --server directly), file a
            # claim, and wait to be approved in the interface.
            #
            # No `try` around it any more. It used to catch the
            # `NotImplementedError` its own stub raised, before Task 15; what
            # replaced the stub reports every failure it has as a log line and
            # a plain return, so a bare `except` here would only be able to
            # catch a bug -- and the shipped unit runs this under
            # `Restart=always` with `StartLimitIntervalSec=0`, so a traceback
            # here is one repeated every `RestartSec=5` forever either way.
            # The check below is what turns "returned without pairing" into a
            # message and an exit, and it does not care why.
            join_a_server(args)
            # Once paired, the pairing just written is what the rest of this
            # function needs -- re-read rather than assumed, because
            # `join_a_server` is the one thing here allowed to have changed it.
            settings = load_link_settings(args.link)
            if settings is None:
                # `join_a_server`'s own docstring promises it blocks "until
                # either the interface approves it or the operator interrupts
                # the process" -- a clean return with nothing written, on
                # abandonment, is a shape it names rather than rules out. The
                # same file also documents a pairing that was written but
                # cannot be read back (HIGH 2's own scenario). Checked here,
                # explicitly, rather than leaving `_boot`'s
                # `assert settings is not None` to catch it: an assert is a
                # traceback under `python -u`, and silently vanishes under
                # `python -O` into a row-3 boot nobody asked for -- neither is
                # a message a person over SSH can act on.
                print(
                    f"{args.link}: still not paired after the join flow returned; nothing to "
                    "run. Pair it by hand instead: `ors-daemon connect --server ... "
                    "--token ...`, then run this again.",
                    file=sys.stderr,
                )
                return 1
            booted = _boot(args, settings)
    else:
        booted = _boot(args, settings)

    if booted is None:
        return 1
    config, screens, clock, version = booted

    # Built whether or not this rack is paired, and idle until something asks:
    # the supervisor takes it at construction, so a stream created later could
    # only be given to workers that do not exist yet. It costs an unwatched rack
    # a set membership test per rendered frame.
    frames = FrameStream()
    supervisor = Supervisor(
        config=config,
        screens=screens,
        store=SnapshotStore(),
        clock=clock,
        status_path=args.status,
        frames=frames,
    )
    _install_signal_handlers(supervisor.stop)
    # Set by `_snapshot_handler` -- and only by it -- when a push stopped the
    # rack because it changed the timezone, so this is how `run` tells that
    # apart from an ordinary SIGTERM once `run_forever` returns. See
    # `_RECLOCK_EXIT`.
    reclock_requested = threading.Event()
    link = _link(
        args, settings, supervisor, clock, version, frames, config.timezone, reclock_requested
    )
    pump = _frame_pump(frames, supervisor, link)
    try:
        # `run_forever` shuts down from its own `finally`, including when `start`
        # fails partway -- so there is nothing to unwind here.
        supervisor.run_forever()
    finally:
        # Before the link is joined, and closed before it is joined at all: the
        # pump parks on the stream rather than on the stop event, so closing is
        # what releases it now instead of at its next poll.
        frames.close()
        if pump is not None:
            pump.join(FRAMES_JOIN_TIMEOUT)
        if link is not None:
            # The event it parks on is already set: `stop` sets it first, and
            # `run_forever` calls `stop` whatever happens. So this is a wait for
            # a thread that is leaving, not a request that it should.
            link.join(LINK_JOIN_TIMEOUT)
            if link.is_alive():
                # It is a daemon thread, so the process still exits. Said out
                # loud because the only way to get here is the failure the link's
                # own send watchdog exists for.
                log.warning("the link thread did not stop; leaving it to the process exit")
    if reclock_requested.is_set():
        # Distinct from 0 on purpose -- see `_RECLOCK_EXIT`. `Restart=always`
        # on the shipped unit does not read this at all; it restarts on any
        # exit. Everything else that might be running this (`Restart=on-failure`,
        # a different supervisor, a person at a terminal) does read it, and
        # used to be told this was a clean stop.
        print(
            f"{args.link}: stopped because a push changed the timezone; restart this "
            "rack to pick up the new clock (systemd's Restart=always already does this "
            "on its own)",
            file=sys.stderr,
        )
        return _RECLOCK_EXIT
    return 0


IDENTITY_FILENAME = "identity.json"
"""What `install` calls this rack's identity, beside the pairing.

`install.py` writes `<state dir>/identity.json` and `--link` defaults to
`<state dir>/link.json`, so deriving one from the other keeps a non-default
`--link` -- a test's `tmp_path`, or a second daemon on one machine -- pointing
at its own identity rather than at the service's.
"""


def _only(server: Found) -> list[Found]:
    """`--server URL`'s answer to the question discovery answers by browsing.

    A named function and not a lambda so that `join.join_a_server` sees the
    same shape either way: something it can call again on every lap, because
    the list it gets back is allowed to change (`discovery.discover` finds a
    server that booted after this Pi did; this one never will, which is the
    whole point of naming it).
    """
    return [server]


def _join_client() -> Any:  # pragma: no cover - constructs the real HTTP client
    """The blocking HTTP client the join flow files and polls with.

    Built here rather than inside `ors_daemon.join` so that module takes its
    client as an argument and every test of it runs without a socket, the same
    way `discovery` takes its browse.
    """
    import requests

    return requests.Session()


def join_a_server(args: argparse.Namespace) -> None:
    """Pair a freshly installed rack with no operator having typed a token.

    `_run` calls this exact name, unconditionally, whenever this rack is
    neither paired (`settings is None`) nor handed `--config` and has no usable
    cache: that is row 4 of `_boot`'s docstring, the state `install` leaves a
    machine in, and the daemon may not take a different path through it.

    This is the wiring; `ors_daemon.join` is the protocol. What happens here is
    the three things that need a `Namespace` and a terminal: reading (or
    minting) this rack's identity beside the pairing, turning `--server URL`
    into the record discovery would have answered with, and telling whoever is
    watching the console what short code to compare -- because approving
    without comparing it is approving a stranger's rack (design spec S6.4), and
    a code that only ever appeared in `install`'s output is one nobody has in
    front of them by the time the interface asks.

    It is deliberately not expected to also start the rack: nothing here reads
    a pushed configuration or returns one. Once this returns, `_run` re-reads
    `args.link` and falls through into `_boot` in this same process -- rows 2
    or 3 of the boot table, the same ones a restart would have taken -- rather
    than exiting and trusting the unit's `Restart=` to bring the process back
    already paired. That used to be the contract: `_run` returned 0 the moment
    this returned, and relied on systemd. It cost the one case that mattered
    most for reasoning about this function -- `ors-daemon run` by hand, on a
    terminal, pairs, prints nothing, exits 0, and the panels stay dark, with
    nothing anywhere saying why. Continuing in-process needs nothing new from
    this function beyond what it already promised: block, then write the
    pairing.

    **Returning without a pairing is a shape, not a bug.** Two servers
    answered and neither may be chosen, `--server` names nothing dialable, this
    rack's identity file cannot be read, a key arrived that will not decrypt,
    or somebody pressed Ctrl-C. Each says so on stderr here or in the log, and
    `_run`'s own check turns the silence that follows into a message naming
    `ors-daemon connect` and a non-zero exit. Nothing here raises: the shipped
    unit runs this under `Restart=always` with `StartLimitIntervalSec=0`, so a
    traceback is one written to the journal every `RestartSec=5` forever.
    """
    link = Path(args.link)
    try:
        identity_path = link.with_name(IDENTITY_FILENAME)
    except ValueError:
        # `--link /` and `--link .` name no file, so there is no name to put
        # an identity beside. The same `ValueError` `_cache_beside` exists for.
        print(
            f"{link}: --link has to name a file, so that this rack's identity and its "
            "pairing can live beside each other",
            file=sys.stderr,
        )
        return

    try:
        rack = identity.load_or_create(identity_path)
    except (OSError, ValueError) as exc:
        # `load_or_create` refuses an identity file it cannot parse, and one
        # readable by more than its owner, rather than replacing it -- both
        # deliberately, both recoverable only by a person. See its docstring.
        print(f"cannot read this rack's identity: {exc}", file=sys.stderr)
        return

    # Before `--server` is looked at, not after: the short code is a fact
    # about this rack and not about any server, and a rack that cannot dial
    # anything yet is exactly the one whose operator is about to go and read
    # this line off the console.
    print(
        f"this rack is not paired yet. Its short code is {rack.short_code} -- compare it "
        "in the interface before approving.",
        file=sys.stderr,
    )

    if args.server:
        found = join.server_from_url(args.server)
        if found is None:
            print(
                f"--server {args.server}: not a URL a claim can be filed against. "
                "It needs a scheme and a host, e.g. http://rack.local:8080",
                file=sys.stderr,
            )
            return
        servers: Callable[[], list[Found]] = partial(_only, found)
    else:
        servers = discovery.discover

    try:
        join.join_a_server(
            identity=rack,
            servers=servers,
            link_path=link,
            sleeper=time.sleep,
            http=_join_client(),
        )
    except KeyboardInterrupt:
        # The one thing in here that can interrupt a wait measured in minutes,
        # and the operator's own doing. `_run` installs its signal handlers
        # only once the rack is up, so without this an impatient Ctrl-C during
        # a backoff is a `KeyboardInterrupt` traceback out of `main`.
        print("interrupted; this rack is still not paired.", file=sys.stderr)


FRAMES_JOIN_TIMEOUT = 1.0
"""How long `run` waits for the frame pump once the rack is down, in seconds.

Short, because of what it can be doing and what it cannot. The panels are
already slept and closed by the time this runs, so nothing this thread does can
darken anything; the most it can be holding is one `send` that the link's own
watchdog takes apart after `SEND_DEADLINE_S`, and one WebP encode of a 240x240
panel, which is milliseconds. It is a daemon thread either way, so the process
exits regardless -- this is here so that the ordinary shutdown is tidy rather
than to bound a failure, and it is a second rather than three because it is
spent *after* `SHUTDOWN_BUDGET`, out of the same `TimeoutStopSec` the link's
join comes from.
"""


def _frame_pump(
    frames: FrameStream, supervisor: Supervisor, link: LinkClient | None
) -> FramePump | None:
    """The thread that encodes panels and sends them, if there is anywhere to send.

    None for an unpaired rack: with no link there is nothing that could ask for
    frames and nowhere to put them, so the thread would be a lifetime of parking
    and waking on a machine with four panels to draw.

    A pump that will not start is logged and stepped over, the same bargain
    `_link` makes: a Pi too short of memory for one more thread is still a rack
    that can draw, and the only thing lost is a preview in a browser.
    """
    if link is None:
        return None
    pump = FramePump(frames=frames, send=link.send, stop=supervisor.stop_event)
    try:
        pump.start()
    except Exception:
        log.exception("could not start the frame pump; this rack draws but cannot be watched")
        return None
    return pump


LINK_JOIN_TIMEOUT = 3.0
"""How long `run` waits for the link thread once the rack is down, in seconds.

Out of the same ten seconds `Supervisor.SHUTDOWN_BUDGET` divides, and spent
after it: the panels are already slept and closed by the time this runs, so what
it protects is only the process exiting before `TimeoutStopSec`. Three is above
the link's `RECV_TIMEOUT_S` of one -- which is how long the receive loop can be
mid-`recv` when the event is set -- with room for a `close()` handshake bounded
at two, and it is a daemon thread either way.
"""


def _snapshot_handler(
    supervisor: Supervisor, booted_timezone: str, reclock_requested: threading.Event
) -> Callable[[DaemonConfig, int], None]:
    """Apply a push, unless it would run the rack against the wrong clock.

    `Supervisor.apply` never rebuilds the clock, and `supervisor.py`'s own
    docstring says why: "the clock is built once, in `__main__`... swapping it
    under a running rack is a change to a component this class is handed
    rather than one it owns." `NightWindow` defaults to enabled, 23:00-07:00,
    so a rack pushed a different timezone than the one it booted with -- most
    commonly a freshly paired one, which boots row 3's `DaemonConfig()` default
    of UTC until its first push -- would otherwise evaluate its night window in
    the wrong zone for the rest of the process: a UTC+2 rack sleeping at 01:00
    local and waking at 09:00 local, silently, until someone restarts it.

    **Checked before any of that: can the pushed timezone even be resolved.**
    `DaemonConfig.timezone` is a bare `str` -- nothing on the server or in the
    schema validates it against the host's tzdata, because the server has no
    way to know what tzdata any given rack ships -- so `Europe/Amsterdaam`, one
    letter off, reaches here exactly as easily as a real zone does. Applied the
    way a real mismatch is applied -- stop, let `Restart=always` bring it back
    -- that typo is unrecoverable: the next boot rebuilds the clock from the
    same unresolvable name, `_boot_from_cache` raises `ClockError`, falls back
    to row 3, claims `config_version=None`, the server reads that as "never
    got it" and pushes the same value again -- forever, panels dark, on a rack
    that was healthy before the push landed. So this is raised instead of
    swallowed into a stop: raising here reaches `LinkClient._config`, which
    turns any exception out of `on_snapshot` into a `Nack` naming the reason
    and leaves the supervisor untouched -- the push is refused, the person who
    typed it sees why in the interface, and the rack keeps running the
    configuration it already had.

    A timezone that *does* resolve but differs from `booted_timezone` is a
    different problem with a different fix: not applied here either, but the
    supervisor is stopped instead of the push being refused, which under the
    shipped unit's `Restart=always` is exactly the restart the mismatch needs
    -- `main` rebuilds the clock from the pushed config's own timezone on the
    way back up. Nothing is lost by not applying it first: `LinkClient._dispatch`
    still writes the cache and acks the push once this returns without raising,
    so the next boot reads the new timezone (and everything else in the push)
    straight out of that same snapshot, without needing to be pushed again.
    `reclock_requested` is set first, so `run` can tell this stop apart from an
    ordinary SIGTERM once `run_forever` returns and answer with `_RECLOCK_EXIT`
    rather than 0 -- see that constant and `run`'s own use of it.
    """

    def handle(snapshot: DaemonConfig, pushed: int) -> None:
        if snapshot.timezone != booted_timezone:
            try:
                system_clock(snapshot.timezone)
            except ClockError:
                log.error(
                    "a push named a timezone this host cannot resolve; refusing "
                    "it so the rack keeps running instead of boot-looping into "
                    "the same failure on every restart",
                    extra={"was": booted_timezone, "pushed": snapshot.timezone},
                )
                raise
            log.warning(
                "a push changed the timezone, which a running rack cannot pick up; "
                "stopping so it comes back clocked correctly",
                extra={"was": booted_timezone, "now": snapshot.timezone},
            )
            reclock_requested.set()
            supervisor.stop()
            return
        supervisor.apply(snapshot)

    return handle


def _link(
    args: argparse.Namespace,
    settings: LinkSettings | None,
    supervisor: Supervisor,
    clock: Clock,
    version: int | None,
    frames: FrameStream,
    booted_timezone: str,
    reclock_requested: threading.Event,
) -> LinkClient | None:
    """The link to the server, if this rack has ever been paired. Raises nothing.

    `config_version` is what the rack is *running*, which only this function
    knows: `_boot` returns it as None whenever the cache was not what the panels
    were brought up from, and reading the number out of the file here would claim
    a configuration that is not on the glass. Claiming it wrongly in the
    optimistic direction is a push the server skips and a rack left showing the
    previous configuration forever.

    `booted_timezone` is `config.timezone` as `_run` booted it, and
    `reclock_requested` is `_run`'s own event -- both handed on to
    `_snapshot_handler`, which needs the first for the one thing a push may not
    do to a running rack (change it) and sets the second when that is why it
    stopped. See that function's docstring.

    `version` also decides what the log line says when `settings` is `None`,
    and not only what `config_version` claims. `settings` is `None` here for
    two different reasons that a flat "no pairing" would conflate: a rack that
    really was never paired (in which case `version` is `None` too -- nothing
    to boot from but the file), and a rack whose *pairing* was unreadable
    while a good cached snapshot sat right beside it (`version` is then the
    cache's own, not `None`) -- `_boot_from_cache`'s docstring calls this out
    by name. That second rack is not running from its config file; it may not
    even have one.
    """
    if settings is None:
        if version is not None:
            log.info(
                "this rack's pairing could not be read, but a cached snapshot beside "
                "it was usable; running from that cache, not from a config file",
                extra={"path": str(args.link), "config_version": version},
            )
        else:
            log.info(
                "this rack has no pairing; running from its config file",
                extra={"path": str(args.link)},
            )
        return None
    try:
        client = LinkClient(
            settings=settings,
            settings_path=Path(args.link),
            on_snapshot=_snapshot_handler(supervisor, booted_timezone, reclock_requested),
            stop=supervisor.stop_event,
            clock=clock,
            config_version=version,
            # Without this `_dispatch` logs "nothing is listening for this" and
            # the server's `POST /api/daemons/{id}/command` answers
            # `{"delivered": true}` for a message no supervisor ever sees --
            # which is the button that lies its own route reasons at length
            # about not being. See `_command_handler` for what each of the four
            # commands means against a rack that is already driving panels, and
            # for which three of them mean nothing.
            on_command=_command_handler(supervisor),
            # Handled on the link's own thread, which is what it is for: it
            # replaces a set and clamps a number, and nothing behind it encodes
            # or draws. See `FrameStream.request` on why the message is
            # whole-daemon state rather than one screen's subscription.
            on_frames_request=frames.request,
            # The two that answer. Without them `_answer` logs that nothing here
            # can reply and the server's wait expires, which the operator reads
            # as a rack that did not respond -- the same lie `on_command`'s
            # absence told, arriving as a timeout instead of as a false
            # `delivered: true`. `SPI_ROOT` is passed rather than reached for
            # inside the handler so that no test of this ever reads `/dev`.
            on_detect=detect_handler(supervisor, SPI_ROOT),
            on_probe=probe_handler(supervisor),
            # And the other end of it. A subscription arrives on a socket, so it
            # ends with that socket: without this the pump goes on encoding
            # WebP for a browser nothing can reach, for as long as the server is
            # away. The server asks again on every connect (`_resume_frames`),
            # so nothing is lost by forgetting.
            on_link_down=frames.disable,
        )
        client.start()
    except Exception:
        log.exception("could not start the link; this rack runs from what it already has")
        return None
    return client


def _command_handler(supervisor: Supervisor) -> Callable[[Command], None]:
    """What the running rack does with a `Command`. Raises nothing worth catching.

    A named function taking the supervisor rather than a closure written at the
    call site, so that what each command means is testable against a real rack
    instead of only through a `run` that stands both ends in for.

    **Only `identify` means anything here, and the other three are refused
    rather than approximated.** That is the honest shape of M3a, and the reason
    per command is worth having in one place:

    - `identify` paints each panel's ordinal on it, which is exactly what
      somebody standing at the rack needs in order to map a physical panel to a
      line of the configuration -- and it is the one command the milestone's
      definition of done names. `Supervisor.identify` is the whole of it.

    - `sleep` and `wake` already exist, as configuration. A panel sleeps because
      it is inside `DaemonConfig.night` or a screen's own
      `ScreenConfig.sleep_override`, and `ScreenWorker.tick` decides that on
      every lap from the clock. A transient command would need new state on the
      render loop that the night check consults, and then a rule for which of
      the two wins, whether a push clears it, and whether it survives the
      watchdog replacing a worker. Those are design questions, not wiring, and
      the spec puts sleep in the inspector's Sleep tab -- which is the
      configuration field, not this.

    - `reload` has nothing to reload. A paired rack's configuration comes from
      the server, and re-sending it is `POST /api/daemons/{id}/push`, which
      exists and mints a fresh version precisely so a rack cannot dedupe it
      away. Re-reading the local YAML would put the rack back on a document the
      server is no longer the source of truth for.

    The server refuses all three, so nothing this daemon is paired with can send
    one. This still answers, because the protocol carries four values and a
    newer server, an older cache or a replayed message can produce any of them:
    a warning naming the command is something an operator can act on, where
    silence is the state this function was written to end.
    """

    def handle(command: Command) -> None:
        if command.command == "identify":
            painted = supervisor.identify(command.screen_id)
            log.info(
                "identified this rack's panels",
                extra={"screen": command.screen_id, "panels": painted},
            )
            return
        log.warning(
            "the server sent a command this daemon does not implement; nothing was done",
            extra={"command": command.command, "screen": command.screen_id},
        )

    return handle


def _boot_from_cache(
    args: argparse.Namespace, settings: LinkSettings | None
) -> tuple[tuple[DaemonConfig, list[ResolvedScreen], Clock, int | None] | None, str]:
    """Try the last pushed snapshot. The boot tuple if it is usable, and either
    way the reason it was not -- empty when it was, since nobody needs a reason
    for something that worked.

    Split out of `_boot` so `_run` can try the cache on its own, before rows
    4/5: `load_link_settings` returns `None` for a pairing file it could not
    read exactly as it does for one that was never written -- most commonly
    `sudo ors-daemon connect`, which leaves link.json root-owned while the
    shipped unit reads it as `User=openrackscreen` -- and a corrupt pairing is
    not a reason to abandon a perfectly good `snapshot.json` sitting beside it
    for the join flow (or worse, row 5's refusal). `settings` may therefore be
    `None` here even for a rack that *is* paired, which is exactly why the
    cache path is derived the same way `_boot` derives it rather than only
    from `settings.cache_path`.

    "Usable" means resolved and clocked, not merely validated: a snapshot
    naming a template this build does not ship validates perfectly and then
    resolves to nothing, and one naming a timezone the host cannot resolve is
    one the daemon refuses to start on.
    """
    cache = Path(settings.cache_path) if settings is not None else _cache_beside(Path(args.link))
    if cache is None:
        # `--link /`: a pairing path with no filename, so there is no cache
        # beside it to boot from. See `_cache_beside`.
        return None, f"{args.link}: names no file, so there is no snapshot cache beside it"

    cached = load_cached_snapshot(cache)
    if cached is None:
        return None, f"{cache}: no usable cached snapshot"

    config, version = cached
    try:
        return (config, resolve_screens(config), system_clock(config.timezone), version), ""
    except (ConfigError, ClockError) as exc:
        log.error(
            "the cached snapshot cannot be served; falling back to the config file",
            extra={"path": str(cache), "error": str(exc)},
        )
        return None, f"{cache}: {exc}"


def _boot(
    args: argparse.Namespace, settings: LinkSettings | None
) -> tuple[DaemonConfig, list[ResolvedScreen], Clock, int | None] | None:
    """What to put on the panels, and which version of it this rack is running.

    Answers three of the five-row boot table; the other two -- neither paired
    nor handed `--config`, and no usable cache either -- are decided by `_run`
    before this is ever called, because there is nothing here to fall back to
    in that case: no pairing to prefer a snapshot from, and no file either.
    This function is never reached with `settings`, `args.config` and a usable
    cache all empty at once.

    The cache first (rows 1 and 2), via `_boot_from_cache` -- it is the last
    thing the server pushed, so it is what the rack was showing when it was
    last powered down, and preferring the local file would mean every reboot
    during a server outage silently reverted the rack to a document somebody
    edited months ago. If `--config` was given, an unusable cache falls back
    to it (row 1) -- because a fallback is what the file is for.

    Falling back returns a version of None -- see `_link`.

    A paired rack with an unusable cache and no `--config` to fall back to
    (row 3) is not the same failure as an unusable cache *and* an unusable
    file (the paragraph below): nothing has ever been pushed yet, which is the
    ordinary state of a rack the moment it finishes pairing, so it starts with
    no screens at all and waits -- `Supervisor.apply` brings panels up as soon
    as the first push lands.

    Neither the cache nor a given file being usable is the one case with no
    good answer. Nothing has been opened, so there is nothing to darken and
    nothing to keep alive; both reasons go to stderr, where `journalctl` will
    have them after a `systemctl start` that did not take, and the exit code is
    what makes the unit's `Restart=` keep trying. What must not happen is a
    traceback.
    """
    booted, cache_error = _boot_from_cache(args, settings)
    if booted is not None:
        # Row 1 (a config file was also given) and row 2 (a paired rack whose
        # last push is cached): the snapshot outranks everything either way.
        return booted

    if args.config is not None:
        # Row 1: the cache was absent or unusable and `--config` names a file
        # to fall back to. Unconditional on pairing, and unchanged by this
        # task -- standalone racks are untouched.
        try:
            config = load_config(args.config)
            return config, resolve_screens(config), system_clock(config.timezone), None
        except (ConfigError, ClockError) as exc:
            print(f"nothing to run: {cache_error}; {exc}", file=sys.stderr)
            return None

    # Row 3: paired, but nothing usable has ever been pushed, and there is no
    # file to fall back to -- `_run` only reaches here when `settings` is not
    # None (either because this rack was already paired, or because
    # `join_a_server` just paired it and `_run` re-read `args.link`) or
    # `args.config` is not None, and the branch above already handled the
    # latter, so this rack is paired. Not an error: a freshly paired rack has
    # nothing pushed to it yet by definition, and the panels come up dark
    # rather than the daemon not coming up at all.
    assert settings is not None, "_run calls _boot only when paired or given --config"
    log.info(
        "paired, but nothing has ever been pushed; starting with no screens and waiting",
        extra={"cache_error": cache_error},
    )
    config = DaemonConfig()
    return config, resolve_screens(config), system_clock(config.timezone), None


def _install_signal_handlers(stop: Callable[[], None]) -> bool:
    """Arrange for SIGTERM and SIGINT to blank the panels. True if they were armed.

    The whole point of the daemon having a shutdown at all: systemd sends
    SIGTERM on `stop` and on `restart`, and a rack whose panels stay lit after
    the process is gone is the symptom every other decision here works
    backwards from.

    Three things make this safe, and none of them is optional.

    *It runs on the main thread, and only there.* `signal.signal` raises
    `ValueError` anywhere else, and CPython delivers signals to the main thread
    regardless -- so a daemon started from a worker thread (embedded in
    something else, or under a test harness) must not die on the way to a
    working rack. It gets no handlers and a warning instead, which is the
    honest trade: the panels are the product, and an un-armed SIGTERM only
    costs the blanking that the default disposition was never going to do
    either.

    *The handler is `stop` itself, not a flag the loop reads later.* Blanking
    four panels is four SPI writes after four thread joins; deferring it to the
    next lap of `run_forever` would add up to a whole tick interval of lit
    panels for no gain. `Supervisor.stop` is written for this: it claims its
    shutdown flag first, under a re-entrant lock.

    *A second signal is therefore a no-op.* It lands on the same thread, which
    already holds that lock, and re-enters a `stop` that has already claimed
    each panel it has shut down -- so it does not sleep and close one a second
    time, which on a GC9A01 is a command written to a device that has been torn
    down. An impatient operator pressing Ctrl-C twice is the ordinary case, not
    the exotic one.

    Nothing is restored: `run` installs these for the rest of the process's
    life, because the only thing after `run` is exit. A caller that has to give
    the dispositions back uses `_stopping_on_signals`.
    """
    if threading.current_thread() is not threading.main_thread():
        log.warning(
            "no signal handlers installed: not the main thread; "
            "the panels will not be blanked on SIGTERM"
        )
        return False

    def handle(signum: int, frame: FrameType | None) -> None:
        log.info("stopping", extra={"signal": signal.Signals(signum).name})
        stop()

    for number in _STOP_SIGNALS:
        signal.signal(number, handle)
    return True


@contextlib.contextmanager
def _stopping_on_signals(stop: Callable[[], None]) -> Iterator[None]:
    """Arm the two signals for the block, then put back what was there before.

    For `identify`, which is a command and not a process: `main` returns, so
    whoever owned SIGINT before it has to own it again afterwards. Leaving the
    daemon's handler installed points Ctrl-C at a closure that sets an `Event`
    nobody is waiting on any more -- which under the console script is merely
    untidy, and inside anything that calls `main` and keeps running (an
    embedder, a test runner, the M3 server's own tooling) is a Ctrl-C that does
    nothing at all.

    A disposition that did not come from Python reads as `None`, and
    `signal.signal` will not take it back; there is nothing to restore in that
    case, so it is left alone rather than guessed at.
    """
    previous = {number: signal.getsignal(number) for number in _STOP_SIGNALS}
    installed = _install_signal_handlers(stop)
    try:
        yield
    finally:
        if installed:
            for number, handler in previous.items():
                if handler is not None:
                    signal.signal(number, handler)


def _render(screens: list[ResolvedScreen], out: Path, data_path: Path | None) -> int:
    """Render every screen to a PNG, through the renderer and not through a panel.

    Without `--data` each screen draws the `connecting` scene, which is what a
    cold rack shows and the only honest thing to draw with no readings: the
    templates bind `{{prom.cpu}}` and friends, so rendering their own scenes
    against nothing would paint an empty gauge that looks like a working one
    reading zero.

    One PNG per screen, named after the screen. `ScreenConfig.name` is not
    unique in the schema -- uniqueness is a rule about the set, which nothing
    enforces -- so two screens sharing a name write one file between them, last
    one winning. The virtual display backend keys its output the same way and
    has the same property; both are documented rather than defended, because
    inventing a suffix here would mean a preview whose filenames no longer
    match the config a person is reading it against.
    """
    data: Any = {}
    if data_path is not None:
        try:
            data = json.loads(data_path.read_text())
        except (OSError, ValueError) as exc:
            print(f"cannot read {data_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"{data_path}: expected a JSON object of integration namespaces", file=sys.stderr)
            return 1

    connecting = system_scenes()["connecting"]
    try:
        out.mkdir(parents=True, exist_ok=True)
        for screen in screens:
            scenes = screen.scenes if data else [connecting]
            context = RenderContext(data={**data, "params": screen.params})
            render_screen(scenes, context).save(out / f"{screen.config.name}.png")
    except OSError as exc:
        # `--out` naming an existing file, or a directory this user cannot
        # write: a mistyped path is a user's mistake like any other here, and
        # gets a sentence rather than a traceback.
        print(f"cannot write to {out}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(screens)} panel(s) to {out}")
    return 0


def _identify(
    screens: list[ResolvedScreen], config: DaemonConfig, clock: Clock, hold: float | None
) -> int:
    """Paint each panel's ordinal on it and hold it there. The setup wizard's tool.

    Its whole job is to let someone standing in front of the rack map a
    physical panel to a line of the config, so three things follow from that
    and none of them is decoration.

    *It holds the digits up.* Painting them and returning would blank them
    again in the same breath -- `stop` sleeps every panel -- and nobody can walk
    a rack that fast. So this waits, and the wait ends on the same signal the
    daemon's does: Ctrl-C is what a person at a terminal presses when they are
    done. `--hold` is for a script (and for the tests, which spend nothing).

    *It runs no loop, no poller and no tunnel.* Doing this through a started
    supervisor would launch `kubectl port-forward` and poll Prometheus to
    identify four panels, and -- worse -- each worker's very next tick would
    redraw its real scene straight over the digit, because `identify` is
    deliberately not sticky. A `ScreenWorker` that is never started has neither
    problem, and is used here purely for its `identify`: it applies each
    screen's own `rotation` and `hflip`, so the digit lands on the glass the
    same way up as the readings will.

    *It prints the map it just drew.* The panel shows a number; the terminal
    says which screen and which SPI device that number is, which is the half of
    the mapping the glass cannot show.

    A panel that will not open, *or that opens and then will not take the
    frame*, is reported and left out of the map rather than abandoning the
    rest -- the same bargain the supervisor makes -- and is what the non-zero
    return says. The second case is the one worth spelling out: the whole
    command is a single claim, "that panel is this screen", so a dark panel
    with `3  PODS` printed beside it is not a partial success but a wrong
    answer. `ScreenWorker.identify` absorbs a failed write by design (a render
    loop must survive one), so the outcome is read back off the worker rather
    than caught.

    Panels are keyed by name in the printed map, and `ScreenConfig.name` is not
    unique in the schema -- two screens may share one. They still get their own
    line here, distinguished by their `position`.
    """
    store = SnapshotStore()
    system = system_scenes()
    stop = threading.Event()
    panels: list[tuple[str, DisplayBackend]] = []
    failed: list[str] = []

    for screen in screens:
        name = screen.config.name
        try:
            backend = build_display(screen.config.display, name)
        except Exception as exc:
            # Broad, like the supervisor's: a backend may fail to open with
            # anything its underlying library raises, and this command's job is
            # to say which panel rather than which exception type. Printed and
            # not also logged: the audience is standing at the rack with this in
            # front of them, and the JSON line beside it would say the same
            # thing twice, four times over.
            print(f"{name}: cannot open the panel: {exc}", file=sys.stderr)
            failed.append(name)
            continue
        worker = ScreenWorker(
            screen=screen,
            store=store,
            display=backend,
            system=system,
            night=config.night,
            stop=stop,
            clock=clock,
        )
        worker.identify(str(screen.config.position))
        # Opened is opened, drawn or not: it goes on the list either way, or its
        # serial device outlives the command that opened it.
        panels.append((name, backend))
        if worker.renders:
            print(f"{screen.config.position}  {name}  {_display_label(screen.config.display)}")
        else:
            # `renders` counts frames that reached the glass, so nought after
            # one identify is a write the backend refused -- a ribbon seated
            # well enough to enumerate the device and not well enough to clock a
            # frame out of it. The backend logged why.
            print(f"{name}: the panel opened but would not take a frame", file=sys.stderr)
            failed.append(name)

    # Armed only when the wait can actually block: `--hold 0` is a script asking
    # for a flash of the digits, and it has no business touching the process's
    # signals on its way past. Restored on the way out either way -- see
    # `_stopping_on_signals`.
    if panels and (hold is None or hold > 0):
        with _stopping_on_signals(stop.set):
            if hold is None:
                print("holding; press Ctrl-C to blank the panels", flush=True)
            # `wait(None)` blocks until a signal sets the event; `wait(30)`
            # gives up on its own. One call covers both.
            stop.wait(hold)

    for name, backend in panels:
        _blank(name, backend)
    return 1 if failed else 0


def _blank(name: str, backend: DisplayBackend) -> None:
    """Put one panel to sleep and let go of it. Raises nothing.

    Guarded separately, for the reason `Supervisor._shut_down_panel` gives: a
    `close` skipped because the `sleep` before it failed is a serial device left
    open, and a panel left lit.
    """
    for action, call in (("sleep", backend.sleep), ("close", backend.close)):
        try:
            call()
        except Exception as exc:
            log.warning(
                "could not shut a panel down cleanly",
                extra={"screen": name, "action": action, "error": str(exc)},
            )


def _display_label(display: DisplayConfig) -> str:
    """How a panel is addressed, for the human reading `identify`'s output."""
    if display.backend == "gc9a01":
        return f"gc9a01 SPI{display.spi_bus}.{display.spi_cs} dc={display.dc} rst={display.rst}"
    return f"{display.backend} -> {display.out_dir}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
