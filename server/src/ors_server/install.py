"""`ors-server install`: a server that survives a reboot without Docker.

Spec §3's third path, beside the image and `uv tool install ors-server`. Same
five steps as the daemon's -- user, directory, venv, unit, enable -- and, on
purpose, **no code shared with it.**

*Why this duplicates `ors_daemon.install` and must keep duplicating it.* The
temptation is obvious and it is wrong twice over. First, packaging:
`ors-daemon` and `ors-server` are separately installable distributions, so a
shared module would have to live in `ors-schema` (or a sixth package), which
would mean every `pip install ors-server` pulling in a library whose reason for
existing is the *daemon's* systemd unit -- a dependency edge in the wrong
direction, added to a published distribution, to save a file. Second, and more
decisive: the two units differ in every line that carries any weight.

`SupplementaryGroups=spi gpio` -- the daemon requires it, and without it every
panel comes up unavailable. Absent here: this process opens no `/dev/spidev*`,
and adding a network-facing service to those groups hands it the rack's
hardware for nothing.

`PrivateDevices=yes` -- the daemon **must not** set it: it hides
`/dev/spidev*` and `/dev/gpiochip*` and takes the rack dark. It **is** set
here, there being no device to lose.

`TimeoutStopSec=30` -- the daemon's is derived from four numbers in its
shutdown path, so that four GC9A01s are slept rather than SIGKILLed lit.
Absent here: nothing holds hardware, and thirty seconds is thirty seconds a
reboot waits for nothing.

`ORS_DATA_DIR` -- not a thing on the daemon side. Mandatory here; see
`unit_text`.

`Environment=PATH=...` -- the daemon needs it, because the tunnel shells out to
`kubectl`. Absent here: this process shells out to nothing.

A shared template would therefore be a template of the parts nobody argues
about, wrapped in two sets of conditionals for the parts that matter, and the
first person to add a fifth difference would find that harder than either file
is today. Two files, each one readable on its own, is the cheaper shape. A
later reader who reaches for the merge is asked to read those five
differences first.

Everything here is parameterised on `Roots` -- `state`, `prefix`, `systemd` --
and every external command goes through an injected `Runner`. The CLI
(`ors_server.__main__._real_roots`) passes `Roots` built from `/var/lib`,
`/opt/ors-server` and `/etc/systemd/system`, and a `Runner` that actually calls
`subprocess.run`. Nothing in this module ever names one of those paths itself
and nothing in it shells out directly -- both are what let
`server/tests/test_install.py` exercise the whole thing without touching the
machine running the tests.

The steps, in order:

1. Refuse a port no socket could bind, *before* anything is touched. Every
   later step would otherwise have to be undone by hand.
2. The data directory, 0700: `ors.db`, `secret.key`, and every integration
   credential the second one encrypts.
3. The system user. `openrackscreen`, the same account the daemon's install
   creates, so a Pi running both halves has one service account -- but a
   *different* state directory, because `/var/lib/openrackscreen` holds the
   rack's pairing and `ors.db` has no business landing on top of it.
4. The venv at a predictable prefix, so the unit can name a path that exists
   no matter who ran this.
5. The unit itself, from `unit_text`.
6. `systemctl daemon-reload`, `enable --now`, and `try-restart` for the
   upgrade case.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ors_server.app import DEFAULT_PORT

SERVICE_NAME = "ors-server"
"""The unit is `ors-server.service`, and the data directory is
`/var/lib/ors-server`.

Deliberately not `openrackscreen`, which is the *daemon's* unit name and the
daemon's `StateDirectory`. On a Pi running both halves those would be the same
unit file and the same directory: `ors.db` would be written into the directory
holding the rack's pairing -- the one file that cannot be handed out again --
and `server/README.md` already warns about exactly that collision for anyone
bind-mounting `/var/lib/openrackscreen` into the container. The service *user*
is shared (below); the paths are not.
"""

SERVICE_USER = "openrackscreen"
"""One service account for both halves, per spec §3.

`useradd` exits 9 when it already exists, which is what happens on the second
`install` and on a Pi where `ors-daemon install` ran first -- treated as
success in both cases, which is what makes either order work.
"""

_NOLOGIN = "/usr/sbin/nologin"
"""Debian (and Raspberry Pi OS) put it here. A service account that can be
logged into is an account that can be logged into -- there is nothing for a
person to do as this user."""


@dataclass(frozen=True)
class Roots:
    """Where `install` writes, standing in for the real filesystem roots so a
    test can point every one of them at `tmp_path`.

    Three fields and not the daemon's five: there is no `/boot` here because
    nothing about this install touches SPI, and no `/etc` because the server
    has no configuration file -- its settings live in the database it creates,
    and the two an operator can choose reach it through the unit's
    `Environment=` lines.
    """

    state: Path
    prefix: Path
    systemd: Path


class Runner(Protocol):
    """Everything `install` shells out for goes through one of these.

    A real implementation wraps `subprocess.run`; `FakeRunner` in the test
    suite records `argv` and returns whatever the test decides, which is what
    keeps every step these tests exercise off the machine running them.
    """

    def run(self, argv: list[str]) -> int: ...


@dataclass(frozen=True)
class InstallReport:
    """What `install` did, in enough detail to print and to decide an exit code
    from.

    `failed` alone conflates two different shapes, and `refused` is the field
    that tells them apart. The port check is the one place `install()` returns
    *before* touching anything -- no directory, no user, no venv, no unit -- so
    `refused=True` means every line describing those steps would be reporting
    work that never happened, and the healthcheck line would name the very port
    that was just refused. Any other failure (a `useradd` exiting something
    other than 0 or 9) is discovered *after* the unit has been written and
    `systemctl` called: `refused=False` there, because the state on disk is
    genuinely mixed and the steps that did run are worth reporting honestly,
    clearly marked as partial.
    """

    unit_path: Path
    data_dir: Path
    port: int
    created_user: bool
    failed: bool
    refused: bool = False
    # `tuple`, not `list`: `frozen=True` makes a dataclass hashable by default,
    # and a `list` field breaks that silently -- the class statement succeeds
    # either way, and only calling `hash()` on an instance raises `TypeError`.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def health_command(self) -> str:
        """One line an operator can paste to see whether the thing is up.

        Built from `self.port` rather than written out with a number in it, so
        that the port the unit binds and the port this names cannot disagree.
        `/api/health` and deliberately not `/api/daemons`: it reads no rows and
        needs no session, which is the same reason the image's `HEALTHCHECK`
        asks for it (`server/README.md`).
        """
        return f"curl -fsS http://127.0.0.1:{self.port}/api/health"

    def warnings_text(self) -> str:
        return "\n".join(self.warnings)


@dataclass(frozen=True)
class UninstallReport:
    """What `uninstall` did. No `failed`: short of the filesystem itself being
    unwritable, there is nothing here that fails outright -- a unit that was
    already gone, or a service that was already stopped, is exactly the state
    `uninstall` is trying to reach."""

    warnings: tuple[str, ...] = field(default_factory=tuple)

    def warnings_text(self) -> str:
        return "\n".join(self.warnings)


_TEMPLATE = """# OpenRackScreen server, as `ors-server install` writes it.
#
# Generated, and rewritten in full by every later `ors-server install` -- which
# is the upgrade path, so an edit made here is an edit lost at the next
# upgrade. Change what the command is given (`--prefix`, `--port`) instead, or
# take this file over: `systemctl disable ors-server` and hand-author your own.

[Unit]
Description=OpenRackScreen server: owns the rack's configuration and serves the interface
Documentation=https://github.com/SilkePilon/OpenRackScreen

# Nothing here waits for a rack: daemons dial *this* process, not the other way
# round, and a server that came up before them is exactly what they reconnect
# to. `network-online` is here so the mDNS announcement below has an address to
# put in its A records at the first attempt; failing to announce is a warning
# and never a refusal to start.
Wants=network-online.target
After=network-online.target

# systemd's default start rate limit is 5 starts in 10s, after which the unit
# enters `failed` and *stays there* until a human runs `systemctl reset-failed`.
# The failures this server has at boot are the ones that pass: a data directory
# on a disk that is not mounted yet, a port something else is still releasing.
# Five quick exits over either would leave the rack's whole configuration
# unreachable until somebody SSHes in, so retrying forever every RestartSec is
# strictly better.
StartLimitIntervalSec=0

[Service]
Type=simple
User=__SERVICE_USER__
Group=__SERVICE_USER__

# No subcommand, and there is none to name: `ors-server` with no arguments runs
# the server. That is what the container's `CMD ["ors-server"]` does and what
# `server/README.md` documents; `install` and `uninstall` are the only two
# words this program accepts, and a unit naming a `serve` that does not exist
# would fail at every boot with a usage message.
#
# The prefix, not a `uv tool` path: `sudo uv tool install ors-server` lands in
# *root's* data directory, which User= above cannot read, and the unit then
# dies at every boot on a permission error naming the console script rather
# than the install that chose it.
ExecStart=__EXEC_START__

# `always`, not `on-failure`: a clean exit is not something this program is
# supposed to do, so exiting 0 is as much a reason to come back as crashing.
Restart=always
RestartSec=5

# Explicit, and the load-bearing line of this file. The code default is
# `$XDG_STATE_HOME/openrackscreen` (or `~/.local/state/openrackscreen`),
# resolved against *this unit's* user -- a `--system --no-create-home` account
# whose home is not a place a database should be, and may not be writable at
# all. The dangerous half is not the crash: it is the case where it works. A
# server pointed at a fresh directory comes up healthy, answers /api/health,
# and asks for a new admin password because it is a new database -- with the
# real one still on disk, untouched, somewhere else. Nothing logs that.
Environment=ORS_DATA_DIR=__DATA_DIR__

# The port uvicorn binds *and* the port the mDNS announcement tells racks to
# dial -- `resolve_port` reads this once and hands the same number to both, so
# they cannot disagree.
Environment=ORS_PORT=__PORT__

# On, and stated rather than left to the default, because the image sets the
# opposite (`ORS_ANNOUNCE=0`) and anyone comparing the two files deserves an
# answer here. The image is silent because it runs on a Docker bridge, which
# does not carry mDNS at all -- the announcement never reaches the LAN and the
# address in it would be the bridge's. This server is on the host's own link,
# where announcing is the entire point: it is how a rack that has never been
# paired finds a server to ask to join, with nobody typing a token.
Environment=ORS_ANNOUNCE=1

# ORS_HOST is deliberately not set. Its code default is already 0.0.0.0, and
# unlike ORS_DATA_DIR that default did not move in M3c. The image sets it
# because 127.0.0.1 *inside a container* is the container and nothing else; on
# a host there is no such trap.

# Creates /var/lib/ors-server owned by User= on every start and, unlike
# RuntimeDirectory, it survives a reboot -- which is the point: `ors.db` and
# `secret.key` live there. 0700 because `secret.key` is the only thing that can
# decrypt the integration credentials stored in `ors.db` right beside it, and
# the server refuses to start if that file is readable by anyone else. `install`
# sets the same mode once; this is what re-asserts it after a restore or a
# `chmod -R` loosened it.
StateDirectory=ors-server
StateDirectoryMode=0700

# Modest hardening. Everything here is what the daemon's unit has, plus the one
# line it cannot have.
NoNewPrivileges=yes
# PrivateDevices=yes, which `daemon/examples/openrackscreen.service`
# deliberately does NOT set: there it would hide /dev/spidev* and
# /dev/gpiochip* and the rack would come up with four unavailable screens. This
# process opens no device at all, so the setting costs nothing here -- and for
# the same reason there is no SupplementaryGroups line and no TimeoutStopSec:
# both of those are about panels, which this half of the project never touches.
PrivateDevices=yes
# `full` leaves /dev alone while making /usr, /boot and /etc read-only. Nothing
# this server writes lives there; the database is under StateDirectory above.
ProtectSystem=full
# Nothing is read out of a home directory either -- unlike the daemon, whose
# kubeconfig lives in one.
ProtectHome=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes

# One JSON object per line on stdout, which journald keeps as-is:
#   journalctl -u ors-server -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def unit_text(exec_start: str, data_dir: Path, port: int) -> str:
    """Render the unit file.

    Three parameters and not one, because two of them are the whole difference
    between this unit and a broken one: `data_dir` becomes
    `Environment=ORS_DATA_DIR=`, without which the server silently comes up
    against a different database at every boot, and `port` reaches both
    `Environment=ORS_PORT=` here and the healthcheck line `InstallReport`
    prints, so the number an operator is told to probe is the number the
    process binds.
    """
    return (
        _TEMPLATE.replace("__EXEC_START__", exec_start)
        .replace("__DATA_DIR__", str(data_dir))
        .replace("__PORT__", str(port))
        .replace("__SERVICE_USER__", SERVICE_USER)
    )


def install(
    roots: Roots,
    runner: Runner,
    *,
    version: str,
    port: int = DEFAULT_PORT,
) -> InstallReport:
    """From a machine with nothing on it to a running service. Safe to re-run
    -- it is the upgrade path -- and every write it makes is idempotent on its
    own: the directory, the user, the venv and the unit all settle to the same
    state no matter how many times this runs.
    """
    unit_path = roots.systemd / f"{SERVICE_NAME}.service"
    data_dir = roots.state / SERVICE_NAME
    warnings: list[str] = []

    # -- the one refusal, before anything is touched -----------------------
    #
    # `--port 70000` is a typo, and a unit written with it in would be enabled,
    # started, and would die at every boot on `Invalid argument` -- with a user
    # created, a venv built and a unit file left behind for somebody to find
    # and remove. Refusing here leaves the machine exactly as it was.
    if not 1 <= port <= 65535:
        warnings.append(
            f"--port {port} is not a port a socket can bind (1-65535). Nothing "
            "was changed on this machine."
        )
        return InstallReport(
            unit_path=unit_path,
            data_dir=data_dir,
            port=port,
            created_user=False,
            failed=True,
            refused=True,
            warnings=tuple(warnings),
        )

    # A prior successful `install` against these same `roots` is what makes
    # `useradd`'s exit code alone unreliable for `created_user`: a fresh
    # `Runner` (a new process, same as a real second `ors-server install`) has
    # no memory of the first run, so a fake -- or a real `useradd` on a machine
    # where the account was removed by hand -- can answer `0` again. The data
    # directory is this machine's own record of "installed before". Nothing
    # downstream branches on it besides the printed report, so a wrong guess
    # here costs a wrong line of output and never a wrong action.
    already_installed = data_dir.is_dir()

    failed = False

    # -- the data directory ------------------------------------------------
    data_dir.mkdir(parents=True, exist_ok=True)
    # 0700 unconditionally, on every run: it holds `secret.key`, which the
    # server itself refuses to start against if it is readable by anyone else,
    # and a mode a `chmod -R` or a restore loosened would otherwise stay loose
    # with nothing to notice.
    data_dir.chmod(0o700)

    # -- the system user ----------------------------------------------------
    created_user = False
    useradd_code = runner.run(
        ["useradd", "--system", "--no-create-home", "--shell", _NOLOGIN, SERVICE_USER]
    )
    if useradd_code == 0:
        created_user = not already_installed
    elif useradd_code == 9:
        # Already exists -- the second `install`, or a Pi where `ors-daemon
        # install` created the shared account first. Not a failure: failing
        # here would break the upgrade path and the both-halves-one-machine
        # install at once.
        created_user = False
    else:
        failed = True
        warnings.append(f"useradd exited {useradd_code}; {SERVICE_USER} may not exist")

    # -- the venv -----------------------------------------------------------
    #
    # A prefix of its own, not the daemon's `/opt/openrackscreen`: `uv venv` on
    # an existing prefix rebuilds it, so one shared prefix would mean each
    # half's install quietly replacing the other's interpreter and packages.
    venv_code = runner.run(["uv", "venv", str(roots.prefix)])
    if venv_code != 0:
        failed = True
        warnings.append(
            f"uv venv exited {venv_code}; there is no interpreter under "
            f"{roots.prefix} for the unit's ExecStart to run"
        )
    pip_code = runner.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            f"{roots.prefix}/bin/python",
            f"ors-server=={version}",
        ]
    )
    if pip_code != 0:
        failed = True
        warnings.append(
            f"uv pip install ors-server=={version} exited {pip_code}; the server is "
            f"not installed under {roots.prefix} and the unit's ExecStart names a "
            "binary that does not exist"
        )

    # -- the unit -----------------------------------------------------------
    roots.systemd.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_text(f"{roots.prefix}/bin/ors-server", data_dir, port))

    # -- enable and start ---------------------------------------------------
    #
    # Skipped outright when the package did not install. The unit above is
    # `Type=simple` with `Restart=always`, `RestartSec=5` and
    # `StartLimitIntervalSec=0`: systemd's start job for a `Type=simple`
    # service completes at fork, so an `ExecStart` naming a binary that does
    # not exist fails asynchronously (203/EXEC) and `enable --now` exits 0
    # anyway -- and with no start limit the unit can never latch into `failed`
    # either. It sits in `activating (auto-restart)`, re-execs every five
    # seconds forever, and `systemctl is-failed` answers no. Enabling that into
    # the boot path is strictly worse than leaving the service off, so a failed
    # `uv pip install` stops here and says so.
    if pip_code != 0:
        warnings.append(
            f"{SERVICE_NAME}.service was written but deliberately not enabled or "
            "started, because the package it runs was not installed. Fix the reason "
            "above and re-run `ors-server install`."
        )
    else:
        reload_code = runner.run(["systemctl", "daemon-reload"])
        if reload_code != 0:
            failed = True
            warnings.append(
                f"systemctl daemon-reload exited {reload_code}; systemd has not read {unit_path}"
            )
        enable_code = runner.run(["systemctl", "enable", "--now", f"{SERVICE_NAME}.service"])
        if enable_code != 0:
            failed = True
            warnings.append(
                f"systemctl enable --now {SERVICE_NAME}.service exited {enable_code}; "
                "the service is not running and will not start at boot"
            )
        # `enable --now` is a no-op on a unit that is already active, so on an
        # upgrade -- where the venv step above just replaced the code underneath a
        # running server -- nothing here would make the new version take effect
        # before somebody rebooted the machine by hand. `try-restart` is exactly
        # its complement: it restarts the unit if it is running and does nothing
        # (does not start it) if it is not, so a first install is unaffected.
        # Its own exit code is left alone on purpose: a `try-restart` of a unit
        # that is not running is a no-op, not a failure.
        runner.run(["systemctl", "try-restart", f"{SERVICE_NAME}.service"])

    return InstallReport(
        unit_path=unit_path,
        data_dir=data_dir,
        port=port,
        created_user=created_user,
        failed=failed,
        refused=False,
        warnings=tuple(warnings),
    )


def uninstall(roots: Roots, runner: Runner, *, purge: bool = False) -> UninstallReport:
    """Stop, disable and remove the unit. Leaves the data directory -- the
    database, the key and every stored credential -- alone unless `purge`.
    """
    warnings: list[str] = []
    unit_path = roots.systemd / f"{SERVICE_NAME}.service"

    runner.run(["systemctl", "stop", f"{SERVICE_NAME}.service"])
    runner.run(["systemctl", "disable", f"{SERVICE_NAME}.service"])
    unit_path.unlink(missing_ok=True)
    runner.run(["systemctl", "daemon-reload"])

    if purge:
        data_dir = roots.state / SERVICE_NAME
        if data_dir.exists():
            # `exists()` first: `rmtree` on a missing path raises, and a second
            # `uninstall --purge` -- or one on a machine where `install` never
            # got as far as the directory -- would answer a person doing
            # cleanup with a traceback.
            shutil.rmtree(data_dir)
        warnings.append(
            "--purge removed the data directory: the admin password, the "
            "secret key and every stored integration credential went with it. "
            "Nothing keeps a second copy of that key, so the credentials in "
            "any backup of ors.db taken beforehand can no longer be decrypted "
            "either -- they have to be entered again by hand."
        )

    return UninstallReport(warnings=tuple(warnings))
