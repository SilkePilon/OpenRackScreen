from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from ors_server import __version__
from ors_server.app import DEFAULT_PORT, AppSettings, create_app
from ors_server.install import InstallReport, Roots, install, uninstall
from ors_server.logging import setup_logging

WS_MAX_MESSAGE_BYTES = 512 * 1024
"""The largest WebSocket message this server will read. Derived, not chosen.

uvicorn's `ws_max_size` defaults to 16,777,216, and inheriting that was the
whole problem: it is a number about websockets in general and has nothing to do
with a rack. Every message on this link is small and the largest by far is a
`Frame`, whose payload is bounded at `MAX_FRAME_BYTES` -- but base64 is 4/3 of
what it carries, so a 256 KiB panel is 349,528 bytes on the wire before the JSON
envelope around it. Half a mebibyte is a little over 1.5 times that, which
leaves room for the envelope, for the heartbeat's `status` object, and for a
field somebody adds later, while being a thirty-second of what uvicorn would
otherwise allow.

What it buys is the difference between a bound and a field validator. `Frame`'s
`max_length` counts the *decoded* payload, so it can only refuse a message the
server has already read and base64-decoded: at the default, any peer holding a
valid daemon key -- or reaching an unauthenticated `/ws/daemon` socket at all --
can make this process allocate 16 MiB per message, as fast as it can write them.
This bound is enforced by `websockets` before a byte reaches the application.

It is deliberately above `MAX_FRAME_BYTES` rather than equal to it: a limit set
to the schema's number would close the socket over a frame the schema accepts,
which is the reconnect loop `MAX_FRAME_BYTES` exists to prevent, arriving from
the transport instead of from the encoder.
"""


def packaged_web_dir() -> Path:
    """The built interface, as shipped inside the wheel.

    `Path(__file__).parent` and not `importlib.resources`: the directory is
    handed to starlette's `StaticFiles`, which wants a real path on a real
    filesystem, and this project is never installed from a zipimport.
    """
    return Path(__file__).resolve().parent / "web"


def resolve_web_dir() -> Path:
    """Where the built interface is. The environment first, the wheel second.

    In that order because the container sets `ORS_WEB_DIR` deliberately, and a
    resolution that preferred the packaged copy would make that setting dead
    everywhere it is used. A checkout serving its own build sets it to
    `web/dist`; `create_app` warns once and serves the API alone when the
    directory holds no build, which stays the ordinary developer state.
    """
    from_environment = os.environ.get("ORS_WEB_DIR")
    return Path(from_environment) if from_environment else packaged_web_dir()


def resolve_data_dir() -> Path:
    """The database, the secret key and the stored credentials.

    `~/.local/state/openrackscreen` and not `/var/lib/openrackscreen`, which
    needs root: the point of publishing to an index is that `uv tool install
    ors-server && ors-server` works, and a default only root can write makes
    the first boot a PermissionError for everyone who has not read the
    environment table. Both deployments that *should* use `/var/lib` -- the
    container and the generated unit -- set `ORS_DATA_DIR` explicitly, which
    keeps that path visible where it is chosen rather than implicit here.
    """
    from_environment = os.environ.get("ORS_DATA_DIR")
    if from_environment:
        return Path(from_environment)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "openrackscreen"


def resolve_port() -> int:
    """The port to listen on, read once and used twice.

    Once for uvicorn, which binds it, and once for `AppSettings`, which is
    where the mDNS announcement gets the number it tells racks to dial. Two
    reads of `ORS_PORT` would be two numbers that have to agree, and the way
    they stop agreeing is a server announcing 8080 while listening on 8443 --
    which no test on this machine could see and every rack on the network
    would.
    """
    return int(os.environ.get("ORS_PORT", str(DEFAULT_PORT)))


_USAGE_EXIT = 2
"""The conventional shell exit code for "you typed it wrong", and argparse's."""

_DEFAULT_PREFIX = Path("/opt/ors-server")
"""Where `install` builds the venv the unit runs from, unless `--prefix` says so.

Deliberately not `/opt/openrackscreen`, which is `ors-daemon install`'s default:
`uv venv` on an existing prefix rebuilds it, so a Pi running both halves against
one shared prefix would have each install quietly replace the other's
interpreter and packages. It is also what `uninstall` hands `_real_roots`, which
needs a prefix to build `Roots` and never reads it.
"""


def _parser() -> argparse.ArgumentParser:
    """`ors-server`, `ors-server install`, `ors-server uninstall`. Nothing else.

    The subcommand is deliberately **not** `required`, and that single word is
    the whole design of this parser. `ors-server` with no arguments runs the
    server: it is what `deploy/Dockerfile`'s `CMD ["ors-server"]` invokes, what
    `server/README.md` documents for a `uv tool install`, and what the
    `ExecStart=` line `install` writes into the unit names -- there is no
    `serve` subcommand for any of the three to say instead. A `required=True`
    added here, or a `print_usage()` when `command` is `None`, breaks all of
    them at once, and the container's symptom would be a restarting service
    with a usage message in its log rather than anything that reads as a
    mistake in this file. `test_bare_ors_server_with_no_arguments_still_runs_
    the_server` is what holds that shut.
    """
    parser = argparse.ArgumentParser(
        prog="ors-server",
        description=(
            "Own the rack's configuration and serve the interface. "
            "With no subcommand, run the server."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    install_command = subparsers.add_parser(
        "install", help="write a systemd unit that runs the server at boot, and start it"
    )
    install_command.add_argument(
        "--prefix",
        type=Path,
        default=_DEFAULT_PREFIX,
        help="where to build the venv the service runs from (default: %(default)s)",
    )
    install_command.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="the port the unit binds, which is also the port the mDNS announcement "
        "tells racks to dial (default: %(default)s)",
    )

    uninstall_command = subparsers.add_parser(
        "uninstall", help="stop the service and remove the systemd unit"
    )
    uninstall_command.add_argument(
        "--purge",
        action="store_true",
        help="also delete /var/lib/ors-server, which holds the admin password, the "
        "secret key and every stored integration credential. Nothing anywhere keeps "
        "a second copy of that key.",
    )
    return parser


@dataclass(frozen=True)
class _SubprocessRunner:
    """The real `install.Runner`: every argv goes through `subprocess.run`.

    The only implementation of `install.Runner` this project ships that touches
    the machine it runs on -- every test under `server/tests/` substitutes
    `FakeRunner`, which is what keeps `install`'s own suite off the machine
    running it.
    """

    def run(self, argv: list[str]) -> int:
        return subprocess.run(argv).returncode


def _real_roots(prefix: Path) -> Roots:
    """`Roots` built from the real filesystem, for the CLI's own use.

    Nothing else in either module names `/var/lib` or `/etc/systemd/system` --
    `install.install` and `install.uninstall` are parameterised on `Roots`
    precisely so that only this one function, called from production code and
    never from a test, has to. Which is also why it needs a test of its own:
    every other test in `test_install.py` patches it away, so without
    `test_real_roots_names_the_actual_machine_paths` the three paths a real
    install lands on would be pinned by nothing at all.

    Pure: it reads nothing and creates nothing.
    """
    return Roots(
        state=Path("/var/lib"),
        prefix=prefix,
        systemd=Path("/etc/systemd/system"),
    )


def _print_install_report(report: InstallReport) -> None:
    """Where the unit went, whether the user was made, and one line that says
    whether the thing is up.

    `report.failed` alone does not say which of two shapes a failure is, and
    printing the same way for both would be wrong in one direction or the other
    -- see `InstallReport.refused`. `refused=True` is the port check, which
    returns before a directory, a user, a venv or a unit is touched, so
    `unit:`, `service user:` and a healthcheck would all describe work that
    never happened -- and the healthcheck would name the very port that was
    just refused for being impossible. Only the warnings are true there, so
    only the warnings are printed. `refused=False` with `failed=True` is a
    failure found mid-run (a `useradd` exiting something other than 0 or 9):
    `install()` keeps going past it, so the unit really was written and
    `systemctl` really was called, and withholding those lines would tell the
    person debugging a half-finished install over SSH the least.
    """
    if report.failed and report.refused:
        if report.warnings:
            print(report.warnings_text(), file=sys.stderr)
        print("install did not finish cleanly; see the warnings above.", file=sys.stderr)
        return
    if report.failed:
        print(
            "install: PARTIAL -- it failed partway through. Everything below is real, "
            "not undone, and reflects what actually happened."
        )
    print(f"unit: {report.unit_path}")
    print(f"service user: {'created' if report.created_user else 'already existed'}")
    print(f"data directory: {report.data_dir}")
    # From `report.port`, never a literal, so the number an operator is told to
    # probe cannot drift from the one the unit binds.
    print(f"health: {report.health_command}")
    if report.warnings:
        print(report.warnings_text(), file=sys.stderr)
    if report.failed:
        print("install did not finish cleanly; see the warnings above.", file=sys.stderr)


def _install(args: argparse.Namespace) -> int:
    """Build the real `Roots` and a real `Runner`, then run `install.install`."""
    report = install(
        _real_roots(args.prefix),
        _SubprocessRunner(),
        version=__version__,
        port=args.port,
    )
    _print_install_report(report)
    # A failed install that returned 0 makes `ors-server install && reboot`
    # proceed on a machine that was never actually configured.
    return 1 if report.failed else 0


def _uninstall(args: argparse.Namespace) -> int:
    """Build the real `Roots` and a real `Runner`, then run `install.uninstall`.

    `_DEFAULT_PREFIX` and not a flag: `uninstall` removes the unit and (with
    `--purge`) the data directory, and `install.uninstall` reads `roots.prefix`
    for neither. Asking for a `--prefix` here would be asking a question whose
    answer is discarded.
    """
    report = uninstall(_real_roots(_DEFAULT_PREFIX), _SubprocessRunner(), purge=args.purge)
    if report.warnings:
        print(report.warnings_text(), file=sys.stderr)
    print(
        "uninstalled: the service is stopped, disabled and its unit removed. "
        "The venv this installed (if any) is left on disk; remove its prefix by "
        "hand if you want it gone too."
    )
    return 0


def _serve() -> int:
    # First, before anything that can log: `create_app` reports a rebuilt
    # schema, and until this runs the root logger has no handler and every
    # record the server writes is discarded where nobody can see it.
    setup_logging(os.environ.get("ORS_LOG_LEVEL", "INFO"))
    settings = AppSettings(
        data_dir=resolve_data_dir(),
        secret_key=os.environ.get("ORS_SECRET_KEY"),
        web_dir=resolve_web_dir(),
        port=resolve_port(),
    )
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("ORS_HOST", "0.0.0.0"),
        port=settings.port,
        # Both of these are settled here rather than left to a default, and for
        # opposite reasons.
        #
        # `ws_max_size` because the default has nothing to do with this
        # application: see `WS_MAX_MESSAGE_BYTES` for what 16 MiB per message
        # costs a server that anyone can open a socket to.
        ws_max_size=WS_MAX_MESSAGE_BYTES,
        # `proxy_headers` because it is *already* the default -- uvicorn 0.52's
        # signature is `proxy_headers: bool = True` -- and passing it makes that
        # legible instead of load-bearing and invisible. The deploy notes tell
        # an operator to set `FORWARDED_ALLOW_IPS` when the server sits behind a
        # reverse proxy, and that environment variable is read only while this
        # is on (uvicorn falls back to `127.0.0.1` when it is unset, so the
        # default is already the safe one). Written down because a future
        # uvicorn flipping it would silently turn every `X-Forwarded-For` this
        # deployment relies on back into the proxy's own address, and the only
        # symptom would be an audit log naming one machine for every request.
        proxy_headers=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse and dispatch. Returns a shell exit code, always.

    `argv` defaults to `None` -- meaning `sys.argv[1:]` -- because that is what
    the console script hands this function, and the no-argument case has to
    reach `_serve()` through exactly that path rather than through a shortcut a
    test would have to know about.

    Nothing here raises for a mistake somebody typed: argparse exits where this
    function has promised to return an `int`, for `--help` as much as for a bad
    flag, and a caller handed `SystemExit` out of a function annotated `-> int`
    has been lied to. `raise SystemExit(main())` at the bottom of this module
    puts the same code back on the process. Before M3c there was no parser at
    all, which is why `ors-server --help` used to bind port 8080 and serve the
    interface instead of printing anything.
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else _USAGE_EXIT

    if args.command in ("install", "uninstall"):
        # Checked here rather than inside `install.install`, which is a pure
        # function over injected roots and must stay callable from a test that
        # is not root -- which is every test in that suite. Writing into
        # /etc/systemd/system and creating a system user are not things a
        # non-root process can do halfway and then report honestly about.
        if os.geteuid() != 0:
            print(f"ors-server {args.command} has to run as root.", file=sys.stderr)
            return _USAGE_EXIT
        return _install(args) if args.command == "install" else _uninstall(args)

    # `args.command is None`: no subcommand, which is `ors-server` typed bare.
    # See `_parser` for why that runs the server rather than printing usage.
    return _serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
