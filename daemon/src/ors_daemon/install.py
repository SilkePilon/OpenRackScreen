"""`ors-daemon install`: from a bare Raspberry Pi to a running service.

Everything here is parameterised on `Roots` -- `etc`, `boot`, `state`,
`prefix`, `systemd` -- and every external command goes through an injected
`Runner`. Production wiring (Task 9) passes `Roots` built from `/etc`,
`/boot`, `/var/lib`, `/opt/openrackscreen` and `/etc/systemd/system`, and a
`Runner` that actually calls `subprocess.run`. Nothing in this module ever
names one of those paths itself, and nothing in it shells out directly --
both are exactly what let `daemon/tests/test_install.py` exercise the whole
thing without touching the machine running the tests.

The steps, in order, and why the order is load-bearing:

1. If `--use-current-interpreter` was asked for, check that the service user
   could actually run what is about to be named -- *before* anything else is
   touched. `sudo uv tool install ors-daemon` lands in root's data directory,
   which `User=openrackscreen` cannot read, and the rack then comes up dead
   with a permission error on the interpreter that appears in no daemon log,
   because the daemon never starts. Refusing after writing the unit leaves a
   machine that is configured to fail at every boot; refusing here leaves it
   exactly as it was before this command ran.
2. Directories: `/etc/openrackscreen` and the private `/var/lib/openrackscreen`
   the pairing and the install identity live in.
3. The system user and the groups that reach the panels.
4. The venv `install` builds at a predictable prefix, so the unit can name a
   path that exists no matter who ran this and under what umask -- unless
   step 1 already picked an interpreter instead.
5. The unit itself, from `unit_text`.
6. `systemctl daemon-reload` and `enable --now`.
7. SPI, via `boot_config.enable_spi` -- unless skipped.
8. The install identity, via `identity.load_or_create`, so the short code is
   there to print.
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ors_daemon import boot_config, identity

SERVICE_NAME = "openrackscreen"
SERVICE_USER = "openrackscreen"

_GROUPS = ("spi", "gpio")
"""On Raspberry Pi OS `/dev/spidev*` is group `spi` and `/dev/gpiochip*` is
group `gpio`. Without both, every screen comes up `unavailable`."""

_NOLOGIN = "/usr/sbin/nologin"
"""Raspberry Pi OS (and Debian generally) put it here. A daemon account that
can be logged into is an account that can be logged into -- there is nothing
for a person to do as this user."""


@dataclass(frozen=True)
class Roots:
    """Where `install` looks for everything, standing in for the real
    filesystem roots so a test can point every one of them at `tmp_path`."""

    etc: Path
    boot: Path
    state: Path
    prefix: Path
    systemd: Path


class Runner(Protocol):
    """Everything `install` shells out for goes through one of these.

    A real implementation wraps `subprocess.run`; `FakeRunner` in the test
    suite records `argv` and returns whatever the test decides, which is what
    keeps every mutation the tests exercise off the machine running them.
    """

    def run(self, argv: list[str]) -> int: ...


@dataclass(frozen=True)
class InstallReport:
    """What `install` did, in enough detail to print and to decide an exit
    code from."""

    unit_path: Path
    created_user: bool
    short_code: str
    reboot_needed: bool
    failed: bool
    warnings: list[str] = field(default_factory=list)

    def warnings_text(self) -> str:
        return "\n".join(self.warnings)


@dataclass(frozen=True)
class UninstallReport:
    """What `uninstall` did. No `failed`: short of the filesystem itself
    being unwritable, there is nothing here that fails outright -- a unit
    that was already gone, or a service that was already stopped, is exactly
    the state `uninstall` is trying to reach."""

    warnings: list[str] = field(default_factory=list)

    def warnings_text(self) -> str:
        return "\n".join(self.warnings)


# The unit template, copied verbatim from `daemon/examples/openrackscreen.service`
# -- comments included, because each one records something learned in front of
# a rack, not something worth re-deriving or paraphrasing here. The only
# changes from that file: `ExecStart` collapsed to the one parameterised line
# `unit_text` fills in (the example spells it across two lines with a shell
# continuation for a human editing it by hand; a generated file has no reader
# to wrap it for), and no `--config` -- M3c made it optional, and a unit
# naming a file would make every rack need one.
#
# `daemon/tests/test_install.py::test_the_generated_unit_and_the_example_do_not_drift`
# asserts every non-comment, non-path setting in the example also appears
# here, so the two cannot quietly drift apart again.
_TEMPLATE = """# OpenRackScreen daemon.
#
#   sudo install -m0644 openrackscreen.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now openrackscreen
#
# Adjust ExecStart and User to match where you installed it. Everything else is
# load-bearing; the comments say why.

[Unit]
Description=OpenRackScreen: drives the rack's round SPI panels
Documentation=https://github.com/SilkePilon/OpenRackScreen

# The daemon survives a cluster it cannot reach entirely on its own -- the
# poller backs off, the tunnel relaunches `kubectl port-forward`, and the panels
# show `connecting` and then `NO DATA` rather than going dark -- so nothing here
# waits for the cluster, and no dependency of this unit may ever be able to keep
# the rack unlit. `network-online` is here only so the first kubectl launch
# after a boot is not guaranteed to fail; the daemon is fine if it does.
Wants=network-online.target
After=network-online.target

# systemd's default start rate limit is 5 starts in 10s, after which the unit
# enters `failed` and *stays there* until a human runs `systemctl reset-failed`.
# A daemon that cannot open a panel exits quickly, so five quick exits would
# take the rack dark permanently over something a later retry might fix (an SD
# card still settling, a kubeconfig not yet restored). Disabled: for a screen,
# retrying forever every RestartSec is strictly better than giving up.
StartLimitIntervalSec=0

[Service]
Type=simple
User=openrackscreen
Group=openrackscreen
# How a non-root user reaches the panels: on Raspberry Pi OS /dev/spidev* is
# group `spi` and /dev/gpiochip* is group `gpio`. Without both, every screen
# comes up `unavailable` and the rack stays dark.
SupplementaryGroups=spi gpio

# `--log-level` is the program's, not the subcommand's, so it goes before `run`:
# add `--log-level DEBUG` right after the executable when a panel is misbehaving
# and the INFO lines are not saying enough. INFO is the default and is what a
# healthy rack should be left on -- DEBUG on four screens fills a journal.
#
# /opt/openrackscreen/bin, not .venv/bin: `ors-daemon install` builds this venv
# with `uv venv /opt/openrackscreen`, which puts binaries in bin/. The nested
# .venv this line used to name was a `uv sync` in a checkout, which is not how
# anyone installs this any more.
#
# No config flag. A paired rack's configuration comes from the server, and
# naming a file here would make every rack need one -- the flag became
# optional in M3c for exactly this reason.
ExecStart=__EXEC_START__

# `always`, not `on-failure`: a clean exit is not something this program is
# supposed to do, so exiting 0 is as much a reason to come back as crashing.
Restart=always
RestartSec=5

# Creates /run/openrackscreen owned by User=, on every start, and removes it on
# stop. That is what makes the status path above work after a reboot with no
# tmpfiles.d rule to maintain -- and keeps a file rewritten every second off the
# SD card.
RuntimeDirectory=openrackscreen
RuntimeDirectoryMode=0755

# Creates /var/lib/openrackscreen owned by User=, and unlike RuntimeDirectory it
# survives a reboot -- which is the point. Two files live there and the daemon
# writes both: the pairing (`ors-daemon connect --link`, default
# /var/lib/openrackscreen/link.json) and, beside it, the last snapshot the server
# pushed. Without this the directory does not exist, `Paired` cannot be saved,
# and the rack has to be paired again by hand after every restart -- which needs
# a new token minted in the interface, because the server keeps only a
# fingerprint of the key it handed over.
#
# 0700 because the pairing is the right to reconfigure this rack and draw on its
# panels. The daemon writes the file 0600 as well; this is the other half.
#
# Which is also why `ors-daemon connect` has to be run as the User= above:
#   sudo -u openrackscreen ors-daemon connect --server ... --token ...
# A `sudo ors-daemon connect` writes that file root-owned and 0600, this daemon
# then gets PermissionError on every read of it, and the rack runs unpaired for
# ever -- with the server showing a daemon that never connected. The daemon logs
# that case as an error rather than as "never paired", and `connect` prints a
# note when it sees it is running as root, but the fix is `chown`.
StateDirectory=openrackscreen
StateDirectoryMode=0700

# Derived, not guessed, and derived from four numbers rather than one. The
# daemon spends at most `SHUTDOWN_BUDGET` seconds (`ors_daemon.supervisor`, 10s
# today) on every thread join and tunnel teardown together -- one deadline for
# all of them, so the cost does not grow with the number of panels -- and only
# then blanks, sleeps and closes the panels. A SIGTERM landing while the server
# is pushing a configuration waits for that push first: `stop` blocks on the
# lock `apply` holds and takes its own deadline afterwards, so the apply's
# `APPLY_BUDGET` (3s) and `BUS_GUARD_BUDGET` (1s) are ADDED to the ten rather
# than spent out of them. Then `LINK_JOIN_TIMEOUT` (3s) after the panels are
# off. Sixteen seconds worst case, and this is that with room for the blanking,
# which happens whether or not any budget ran out. Past whatever this says,
# systemd sends SIGKILL, the panels are never slept, and four GC9A01s stay lit
# until the rack is power-cycled -- the one outcome the whole shutdown path
# exists to prevent. Raise it, never lower it, and if any of those four numbers
# goes up, raise this first.
TimeoutStopSec=30

# The tunnel shells out to `kubectl`, which is usually in /usr/local/bin -- not
# on the PATH systemd would otherwise hand a system unit on every distribution.
# `~` in the config's kubeconfig path expands against this user's home, which
# systemd sets from User= (and which the daemon resolves itself if it does not).
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Modest hardening. Deliberately without device isolation: it would hide
# /dev/spidev* and /dev/gpiochip*, and the daemon would come up reporting four
# unavailable screens with a permission error nobody would connect to this line.
NoNewPrivileges=yes
# `full` leaves /dev alone while making /usr, /boot and /etc read-only. The
# config is read, never written.
ProtectSystem=full
# The kubeconfig lives in the daemon user's home and is only ever read.
ProtectHome=read-only
ProtectControlGroups=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes

# One JSON object per line on stdout, which journald keeps as-is:
#   journalctl -u openrackscreen -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def unit_text(exec_start: str) -> str:
    """Render the unit file, with `exec_start` as the whole value after
    `ExecStart=` -- no `--config`, and no line continuation, since a
    generated file has no reader to wrap it for."""
    return _TEMPLATE.replace("__EXEC_START__", exec_start)


def _executable_by_service_user(executable: Path, trusted_root: Path) -> tuple[bool, str]:
    """Whether `SERVICE_USER` -- not whoever runs `install`, usually root --
    could execute `executable`.

    Checked with the file and directory modes directly rather than
    `os.access`: `os.access` answers for the *calling* process, which during
    `install` is root (or, under test, whoever runs pytest) and can read
    anything regardless of mode, so it would report success right up until
    the daemon itself tried and failed. `SERVICE_USER` is in no group any of
    these paths would grant through, so what matters is purely the `other`
    bits -- on the file itself, and on every directory between it and
    `trusted_root` that has to be traversed to reach it.

    The climb stops at `trusted_root` rather than the real filesystem root.
    In production that loses nothing: `trusted_root` is `Roots.etc`, and a
    real `/etc` is always traversable or `ProtectSystem=full` above would
    already be broken for every unit on the machine. In the test suite it is
    required: `tmp_path` itself is created mode 0700 (`pytest-of-<user>` and
    its children, a security default pytest ships), so climbing further would
    fail the very test that expects success, on every machine.
    """
    mode = stat.S_IMODE(executable.stat().st_mode)
    if not (mode & stat.S_IROTH and mode & stat.S_IXOTH):
        return False, (
            f"{executable} is mode {mode:04o} and not readable+executable by "
            f"{SERVICE_USER} (o+rx). chmod it, or drop --use-current-interpreter "
            "and let install build a venv instead."
        )
    current = executable.parent
    while True:
        mode = stat.S_IMODE(current.stat().st_mode)
        if not (mode & stat.S_IXOTH):
            return False, (
                f"{current} is mode {mode:04o} and not searchable by "
                f"{SERVICE_USER} (o+x); {executable} is unreachable no matter "
                "its own mode."
            )
        if current == trusted_root or current == current.parent:
            return True, ""
        current = current.parent


def install(
    roots: Roots,
    runner: Runner,
    *,
    version: str,
    enable_spi_step: bool = True,
    use_current_interpreter: bool = False,
    now: str,
    executable: Path | None = None,
) -> InstallReport:
    """From a bare Raspberry Pi to a running service. Safe to re-run -- it is
    the upgrade path -- and every write it makes is idempotent on its own:
    directories, the user, the groups, the unit, SPI and the identity all
    settle to the same state no matter how many times this runs.
    """
    unit_path = roots.systemd / f"{SERVICE_NAME}.service"
    warnings: list[str] = []

    # A prior successful `install` against these same `roots` is what makes
    # `useradd`'s exit code alone unreliable for `created_user`: a fresh
    # `Runner` (a new process, same as a real second `ors-daemon install`)
    # has no memory of the first run, so a fake -- or a real `useradd` a test
    # forgot to seed with `9` -- can answer `0` again. `etc` is what
    # `install` itself creates and nothing else touches, so its presence
    # beforehand is this rack's own record of "installed before".
    already_installed = (roots.etc / SERVICE_NAME).is_dir()

    if use_current_interpreter:
        if executable is None:
            raise ValueError("use_current_interpreter=True requires executable")
        ok, reason = _executable_by_service_user(executable, roots.etc)
        if not ok:
            warnings.append(reason)
            return InstallReport(
                unit_path=unit_path,
                created_user=False,
                short_code="",
                reboot_needed=False,
                failed=True,
                warnings=warnings,
            )
        bin_path = str(executable)
    else:
        bin_path = f"{roots.prefix}/bin/ors-daemon"

    failed = False

    # -- directories ----------------------------------------------------
    (roots.etc / SERVICE_NAME).mkdir(parents=True, exist_ok=True)
    state_dir = roots.state / SERVICE_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    # 0700 unconditionally, on every run: matches `identity.py`'s own
    # precedent, and a mode a `chmod -R` or a restore loosened would
    # otherwise stay loose forever, with nothing to notice.
    state_dir.chmod(0o700)

    # -- the system user --------------------------------------------------
    created_user = False
    useradd_code = runner.run(
        [
            "useradd",
            "--system",
            "--no-create-home",
            "--shell",
            _NOLOGIN,
            SERVICE_USER,
        ]
    )
    if useradd_code == 0:
        created_user = not already_installed
    elif useradd_code == 9:
        # Already exists. Not a failure -- the second `install` is the
        # upgrade path, and failing it here would break that.
        created_user = False
    else:
        failed = True
        warnings.append(f"useradd exited {useradd_code}; {SERVICE_USER} may not exist")

    # -- the groups that reach the panels ---------------------------------
    for group in _GROUPS:
        code = runner.run(["usermod", "-aG", group, SERVICE_USER])
        if code == 6:
            # The group is missing. It comes with the udev rules that make
            # it mean anything -- inventing one with no rules behind it
            # produces a rack that comes up with an unavailable screen and a
            # configuration that looks correct, so this warns rather than
            # `groupadd`s.
            warnings.append(
                f"group '{group}' does not exist; {SERVICE_USER} was not added to "
                f"it. The udev rules that create it are missing -- panels reached "
                f"through it will come up unavailable."
            )
        elif code != 0:
            failed = True
            warnings.append(f"usermod -aG {group} exited {code}")

    # -- the venv, unless an existing interpreter was named ----------------
    if not use_current_interpreter:
        runner.run(["uv", "venv", str(roots.prefix)])
        runner.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                f"{roots.prefix}/bin/python",
                f"ors-daemon[hardware]=={version}",
            ]
        )

    # -- the unit -----------------------------------------------------------
    exec_start = f"{bin_path} run --status /run/openrackscreen/status.json"
    roots.systemd.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_text(exec_start))

    # -- enable and start ----------------------------------------------------
    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", "--now", f"{SERVICE_NAME}.service"])

    # -- SPI ------------------------------------------------------------------
    reboot_needed = False
    if enable_spi_step:
        spi_result = boot_config.enable_spi(roots.boot, now)
        reboot_needed = bool(spi_result.added)

    # -- the install identity ---------------------------------------------
    ident = identity.load_or_create(state_dir / "identity.json")

    return InstallReport(
        unit_path=unit_path,
        created_user=created_user,
        short_code=ident.short_code,
        reboot_needed=reboot_needed,
        failed=failed,
        warnings=warnings,
    )


def uninstall(roots: Roots, runner: Runner, *, purge: bool = False) -> UninstallReport:
    """Stop, disable and remove the unit. Leaves the state directory --
    the pairing and the install identity -- alone unless `purge`, and never
    touches `config.txt`: disabling SPI is not obviously desirable, and the
    backup `install` made is on disk with a name that says where it came
    from, for whoever decides it is.
    """
    warnings: list[str] = []
    unit_path = roots.systemd / f"{SERVICE_NAME}.service"

    runner.run(["systemctl", "stop", SERVICE_NAME])
    runner.run(["systemctl", "disable", SERVICE_NAME])
    unit_path.unlink(missing_ok=True)
    runner.run(["systemctl", "daemon-reload"])

    if purge:
        state_dir = roots.state / SERVICE_NAME
        if state_dir.exists():
            shutil.rmtree(state_dir)
        warnings.append(
            "--purge removed the pairing and the install identity. This rack "
            "will need to be re-approved in the interface the next time it is "
            "installed."
        )

    return UninstallReport(warnings=warnings)
