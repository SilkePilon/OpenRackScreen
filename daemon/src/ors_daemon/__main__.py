"""The daemon's front door: `run`, `validate`, `render`, `identify`.

Four commands, one config path, and a deliberate split down the middle of them:
`validate` and `render` touch no hardware at all, so they are what a person runs
on a laptop before a config ever reaches the Pi; `run` and `identify` open
panels, so they are what runs in front of the rack.

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
import json
import logging
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from ors_render import RenderContext, render_screen
from ors_schema.daemon import DaemonConfig, DisplayConfig

from ors_daemon.clock import Clock, ClockError, system_clock
from ors_daemon.config import (
    ConfigError,
    ResolvedScreen,
    load_config,
    resolve_screens,
    system_scenes,
)
from ors_daemon.displays import DisplayBackend, build_display
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

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
"""Constrained by argparse rather than handed to `setup_logging` unchecked: it
passes the string to `Logger.setLevel`, which raises on anything else -- and a
typo in a systemd unit would then be a traceback at boot instead of a usage
error the moment the unit was written."""

_USAGE_EXIT = 2
"""The conventional shell exit code for "you typed it wrong", and argparse's."""


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

    # Added last and to every subparser, so `--config` reads the same wherever
    # it appears rather than being positional in one command and not another.
    for sub in subparsers.choices.values():
        sub.add_argument("--config", required=True, type=Path, help="path to rack.yaml")
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
    if args.command == "identify":
        return _identify(screens, config, clock, args.hold)
    # The parser's `choices` is what makes this exhaustive; the assert is how
    # that reaches a reader, and how a fifth subcommand added without a branch
    # here fails loudly instead of quietly driving the rack.
    assert args.command == "run"
    return _run(config, screens, clock, args.status)


def _run(
    config: DaemonConfig, screens: list[ResolvedScreen], clock: Clock, status_path: Path
) -> int:
    """Drive the rack until a signal arrives."""
    supervisor = Supervisor(
        config=config,
        screens=screens,
        store=SnapshotStore(),
        clock=clock,
        status_path=status_path,
    )
    _install_signal_handlers(supervisor.stop)
    # `run_forever` shuts down from its own `finally`, including when `start`
    # fails partway -- so there is nothing to unwind here.
    supervisor.run_forever()
    return 0


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
    already holds that lock, and re-enters a `stop` that has already claimed the
    shutdown -- so it returns immediately rather than sleeping and closing every
    panel a second time, which on a GC9A01 is a command written to a device that
    has been torn down. An impatient operator pressing Ctrl-C twice is the
    ordinary case, not the exotic one.
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

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, handle)
    return True


def _render(screens: list[ResolvedScreen], out: Path, data_path: Path | None) -> int:
    """Render every screen to a PNG, through the renderer and not through a panel.

    Without `--data` each screen draws the `connecting` scene, which is what a
    cold rack shows and the only honest thing to draw with no readings: the
    templates bind `{{prom.cpu}}` and friends, so rendering their own scenes
    against nothing would paint an empty gauge that looks like a working one
    reading zero.
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

    A panel that will not open is reported and skipped rather than abandoning
    the rest -- the same bargain the supervisor makes -- and is what the
    non-zero return says: the operator is standing there, and a silent three
    out of four is worse than useless to them.
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
        panels.append((name, backend))
        print(f"{screen.config.position}  {name}  {_display_label(screen.config.display)}")

    # Only when the wait can actually block. `--hold 0` is a script asking for
    # a flash of the digits, and it has no business changing the disposition of
    # two signals on its way past -- which would outlive this command inside
    # anything that called `main` rather than the process.
    if panels and (hold is None or hold > 0):
        _install_signal_handlers(stop.set)
        if hold is None:
            print("holding; press Ctrl-C to blank the panels", flush=True)
        # `wait(None)` blocks until a signal sets the event; `wait(30)` gives up
        # on its own. One call covers both.
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
