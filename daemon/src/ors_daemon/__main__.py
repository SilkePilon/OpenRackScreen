"""The daemon's front door: `run`, `connect`, `validate`, `render`, `identify`.

Five commands, one config path, and a deliberate split down the middle of them:
`validate` and `render` touch no hardware at all, so they are what a person runs
on a laptop before a config ever reaches the Pi; `run` and `identify` open
panels, so they are what runs in front of the rack. `connect` touches neither --
it writes the pairing a later `run` dials with.

*Where a running rack's configuration comes from.* `run` boots from the last
snapshot the server pushed if there is a usable one, and from the local file
otherwise -- in that order, because the server is the source of truth once a
rack is paired and the file is the fallback that keeps a server outage from
darkening anything. Neither being usable is the one case with no good answer:
there is nothing to draw and no panel is open yet, so it is a message and a
non-zero exit rather than a traceback or a rack that pretends.

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
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from ors_render import RenderContext, render_screen
from ors_schema.daemon import DaemonConfig, DisplayConfig
from ors_schema.link import Command

from ors_daemon.clock import Clock, ClockError, system_clock
from ors_daemon.config import (
    ConfigError,
    ResolvedScreen,
    load_cached_snapshot,
    load_config,
    resolve_screens,
    system_scenes,
)
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.frames import FramePump, FrameStream
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

    # Added last and to every subparser that has a config to read, so `--config`
    # reads the same wherever it appears rather than being positional in one
    # command and not another. `connect` is the exception and deliberately so:
    # pairing happens before a rack has a configuration, and afterwards the
    # configuration comes from the server -- requiring it here would make the
    # first thing an operator runs the one thing they cannot yet supply.
    for name, sub in subparsers.choices.items():
        if name != "connect":
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

    # Both before the config is read, and for opposite reasons: `connect` has no
    # config to read, and `run` decides for itself what its configuration is --
    # a pushed snapshot outranks the file, and neither being readable is a
    # different answer from the one below.
    if args.command == "connect":
        return _connect(args)
    if args.command == "run":
        return _run(args)

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
    """
    # Read once and handed on, so the pairing that decides where the cache is
    # and the pairing the link dials with cannot be two different readings of a
    # file somebody is editing.
    settings = load_link_settings(args.link)
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
    link = _link(args, settings, supervisor, clock, version, frames)
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
    return 0


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


def _link(
    args: argparse.Namespace,
    settings: LinkSettings | None,
    supervisor: Supervisor,
    clock: Clock,
    version: int | None,
    frames: FrameStream,
) -> LinkClient | None:
    """The link to the server, if this rack has ever been paired. Raises nothing.

    `config_version` is what the rack is *running*, which only this function
    knows: `_boot` returns it as None whenever the cache was not what the panels
    were brought up from, and reading the number out of the file here would claim
    a configuration that is not on the glass. Claiming it wrongly in the
    optimistic direction is a push the server skips and a rack left showing the
    previous configuration forever.
    """
    if settings is None:
        log.info(
            "this rack has no pairing; running from its config file", extra={"path": str(args.link)}
        )
        return None
    try:
        client = LinkClient(
            settings=settings,
            settings_path=Path(args.link),
            on_snapshot=lambda snapshot, pushed: supervisor.apply(snapshot),
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


def _boot(
    args: argparse.Namespace, settings: LinkSettings | None
) -> tuple[DaemonConfig, list[ResolvedScreen], Clock, int | None] | None:
    """What to put on the panels, and which version of it this rack is running.

    The cache first. It is the last thing the server pushed, so it is what the
    rack was showing when it was last powered down, and preferring the local file
    would mean every reboot during a server outage silently reverted the rack to
    a document somebody edited months ago.

    "Usable" means resolved and clocked, not merely validated: a snapshot naming
    a template this build does not ship validates perfectly and then resolves to
    nothing, and one naming a timezone the host cannot resolve is one the daemon
    refuses to start on. Both fall back to the file, because a fallback is what
    the file is for.

    Falling back returns a version of None -- see `_link`.

    Neither being usable is the one case with no good answer. Nothing has been
    opened, so there is nothing to darken and nothing to keep alive; both reasons
    go to stderr, where `journalctl` will have them after a `systemctl start`
    that did not take, and the exit code is what makes the unit's `Restart=`
    keep trying. What must not happen is a traceback.
    """
    cache = Path(settings.cache_path) if settings is not None else _cache_beside(Path(args.link))

    cached = load_cached_snapshot(cache) if cache is not None else None
    if cache is None:
        # `--link /`: a pairing path with no filename, so there is no cache
        # beside it to boot from. Not fatal -- there is a config file, and this
        # rack is not paired either way. See `_cache_beside`.
        cache_error = f"{args.link}: names no file, so there is no snapshot cache beside it"
    elif cached is not None:
        config, version = cached
        try:
            return config, resolve_screens(config), system_clock(config.timezone), version
        except (ConfigError, ClockError) as exc:
            log.error(
                "the cached snapshot cannot be served; falling back to the config file",
                extra={"path": str(cache), "error": str(exc)},
            )
            cache_error = f"{cache}: {exc}"
    else:
        cache_error = f"{cache}: no usable cached snapshot"

    try:
        config = load_config(args.config)
        return config, resolve_screens(config), system_clock(config.timezone), None
    except (ConfigError, ClockError) as exc:
        print(f"nothing to run: {cache_error}; {exc}", file=sys.stderr)
        return None


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
