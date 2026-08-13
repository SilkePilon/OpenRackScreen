"""The four things a person does with the daemon from a shell.

Nothing here may open SPI, a GPIO line, a socket or a kubeconfig. `validate` and
`render` are the commands you run on a laptop and are exercised against the
*shipped* config, pins and all, precisely because neither may reach the
hardware those pins describe; `identify` and `run` are driven through virtual
panels and through the signal handler the CLI installs, called directly rather
than raised.
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from ors_daemon.__main__ import DEFAULT_STATUS_PATH, _install_signal_handlers, main
from ors_daemon.clock import FakeClock
from ors_daemon.config import load_config, resolve_screens
from ors_daemon.displays import DisplayError
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor
from ors_schema.daemon import ScreenConfig
from PIL import Image

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rack.yaml"
NOON = datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
"""Outside the shipped night window, so the panels in these tests are awake."""

WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""


def write_virtual_config(tmp_path: Path) -> Path:
    """The shipped rack, rewired to write PNGs instead of driving glass."""
    config = yaml.safe_load(EXAMPLE.read_text())
    for screen in config["screens"]:
        screen["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    for integration in config["integrations"]:
        # No kubectl, and a URL nothing will ever be asked for: `run` is only
        # ever reached here with a supervisor that starts no pollers.
        integration.pop("tunnel", None)
        integration["url"] = "http://127.0.0.1:1"
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    """A minimal one-screen config, with `overrides` applied at the top level."""
    config: dict[str, Any] = {
        "version": 1,
        "screens": [
            {
                "name": "S1",
                "position": 1,
                "display": {"backend": "virtual", "out_dir": str(tmp_path / "panels")},
                "template": "ring-gauge",
                "params": {"title": "S1"},
            }
        ],
    }
    config.update(overrides)
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


class CountingDisplay:
    """A panel that counts what it was asked to do, and can act during a sleep.

    `on_sleep` is how a test lands a second signal in the middle of the first
    one's shutdown, which is the one interleaving a real rack can produce: a
    signal handler runs on the thread that is already inside `stop`.
    """

    def __init__(self) -> None:
        self.sleeps = 0
        self.closed = 0
        self.on_sleep: Callable[[], None] | None = None

    def show(self, image: Image.Image) -> None:
        pass

    def sleep(self) -> None:
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep()

    def wake(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1


class RecordingSupervisor:
    """Stands in for the real one so `run` returns instead of running forever."""

    instances: list[RecordingSupervisor] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.stops = 0
        self.ran = 0
        self.handlers_when_run: set[int] = set()
        RecordingSupervisor.instances.append(self)

    def run_forever(self) -> None:
        self.ran += 1
        # Read *here*, not after `main` returns: a handler installed once the
        # loop is already over is a handler no signal can ever reach.
        self.handlers_when_run = set(INSTALLED)

    def stop(self) -> None:
        self.stops += 1


INSTALLED: dict[int, Callable[[int, FrameType | None], None]] = {}
"""Handlers the CLI installed, once `capture_signals` is in force."""


def capture_signals(monkeypatch: Any) -> dict[int, Callable[[int, FrameType | None], None]]:
    """Record what the CLI installs instead of arming the real signals.

    A test that armed them for real would either kill the pytest process (the
    default SIGTERM disposition) or abort the session (SIGINT), depending on
    which one the code under test forgot.
    """
    INSTALLED.clear()

    def fake_signal(number: int, handler: Callable[[int, FrameType | None], None]) -> None:
        INSTALLED[number] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    return INSTALLED


class DeadDisplay(CountingDisplay):
    """A panel that opens and then refuses every frame.

    A ribbon seated well enough to enumerate the device and not well enough to
    clock a frame out of it, which is a real state of a rack and the one the
    `identify` map must not report as a lit panel.
    """

    def show(self, image: Image.Image) -> None:
        raise DisplayError("no ack from the panel")


def explode(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("this command must not build a display backend")


def capture_firing_signals(monkeypatch: Any) -> list[tuple[int, Any]]:
    """Record every disposition change, and fire the *first* one per signal.

    Firing the arming is a signal landing during a hold, which is what ends it
    -- so a passing test spends none of its `--hold`. Only the arming: firing a
    *restore* would call whatever was there before, which is pytest's own SIGINT
    handler or `SIG_DFL`, and the latter is not callable at all.

    `callable` is what enforces that, and it is not belt and braces. This patch
    is live for the whole test, and pytest-timeout disarms its own alarm with
    `signal.signal(SIGALRM, SIG_DFL)` -- a number this fixture has never seen,
    so the first-time rule would let it through and call an integer. That raises
    `TypeError` inside pytest's own teardown, which surfaces as an
    INTERNALERROR: every real failure in the run is replaced by a traceback
    about signals. It only happens on an already-interrupted run, which is
    exactly when the real failures are the thing worth reading.
    """
    calls: list[tuple[int, Any]] = []
    fired: set[int] = set()

    def fake_signal(number: int, handler: Any) -> None:
        calls.append((number, handler))
        if number not in fired and callable(handler):
            fired.add(number)
            handler(number, None)

    monkeypatch.setattr(signal, "signal", fake_signal)
    return calls


def virtual_supervisor(
    tmp_path: Path, on_build: Callable[[str], None] | None = None
) -> tuple[Supervisor, list[CountingDisplay]]:
    """The real supervisor over the shipped config, on panels that are counters.

    `on_build` runs while a panel is being opened, which is how a test lands a
    signal in the middle of `start()` -- on the very thread doing the opening,
    exactly as a real one does.
    """
    config = load_config(write_virtual_config(tmp_path))
    displays: list[CountingDisplay] = []

    def display_factory(screen_config: ScreenConfig, name: str) -> CountingDisplay:
        displays.append(CountingDisplay())
        if on_build is not None:
            on_build(name)
        return displays[-1]

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=SnapshotStore(),
        clock=FakeClock(NOON),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    return supervisor, displays


def test_the_shipped_example_config_validates() -> None:
    """CI's guard on `examples/rack.yaml`, and it must need no rack to run.

    The file names GPIO pins and a kubeconfig that exist only on the Pi, so
    this passing is also the statement that `validate` opens neither.
    """
    assert main(["validate", "--config", str(EXAMPLE)]) == 0


def test_validating_never_opens_a_panel(monkeypatch: Any) -> None:
    """The pins in the shipped config are a document, not a device."""
    monkeypatch.setattr("ors_daemon.__main__.build_display", explode)

    assert main(["validate", "--config", str(EXAMPLE)]) == 0


def test_validate_reports_a_broken_config_without_a_traceback(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "night": {"start": "99:99"}}))

    assert main(["validate", "--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert "night" in captured.err
    assert "Traceback" not in captured.err


def test_validate_reports_a_missing_config_file(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "nope.yaml"

    assert main(["validate", "--config", str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err


def test_validate_reports_a_template_no_screen_can_resolve(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "rack.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "screens": [
                    {
                        "name": "S1",
                        "position": 1,
                        "display": {"backend": "virtual", "out_dir": str(tmp_path)},
                        "template": "no-such-template",
                    }
                ],
            }
        )
    )

    assert main(["validate", "--config", str(path)]) == 1
    assert "no-such-template" in capsys.readouterr().err


def test_validate_reports_a_timezone_the_host_cannot_resolve(tmp_path: Path, capsys: Any) -> None:
    """A validated config the daemon then refuses to start on is not validated.

    Night mode is computed in the configured zone, so a typo here is a rack
    that never sleeps -- and it is exactly the class of mistake this command
    exists to catch before the config reaches the Pi.
    """
    path = write_config(tmp_path, timezone="Mars/Olympus")

    assert main(["validate", "--config", str(path)]) == 1
    assert "Mars/Olympus" in capsys.readouterr().err


def test_render_writes_one_png_per_screen_without_touching_hardware(tmp_path: Path) -> None:
    path = write_virtual_config(tmp_path)

    assert main(["render", "--config", str(path), "--out", str(tmp_path / "out")]) == 0
    written = sorted(item.name for item in (tmp_path / "out").glob("*.png"))
    assert written == ["CPU.png", "HEALTH.png", "MEM.png", "PODS.png"]


def test_render_needs_no_backend_even_for_the_gc9a01_rack(tmp_path: Path) -> None:
    """`render` goes through `ors_render`, not through a display.

    So the shipped config -- four GC9A01 panels on two SPI buses -- renders on
    a laptop with no luma installed, which is the whole point of the command.
    """
    out = tmp_path / "out"

    assert main(["render", "--config", str(EXAMPLE), "--out", str(out)]) == 0
    assert len(list(out.glob("*.png"))) == 4
    assert Image.open(out / "CPU.png").size == (240, 240)


def test_render_never_opens_a_panel(tmp_path: Path, monkeypatch: Any) -> None:
    """Pinned, not inferred from luma being absent.

    The test above passes on CI for the wrong reason too -- there is no luma to
    open a panel with. On the Pi, where the hardware extra is installed, a
    regression that reached for a backend would light four panels and still be
    green without this.
    """
    monkeypatch.setattr("ors_daemon.__main__.build_display", explode)

    assert main(["render", "--config", str(EXAMPLE), "--out", str(tmp_path / "out")]) == 0


def test_render_accepts_a_data_file_so_a_screen_can_be_checked_offline(tmp_path: Path) -> None:
    path = write_virtual_config(tmp_path)
    data = tmp_path / "data.json"
    data.write_text(
        json.dumps({"prom": {"cpu": 42.4, "nodes_ready": 3, "nodes_total": 3, "alerts": 0}})
    )

    assert (
        main(
            [
                "render",
                "--config",
                str(path),
                "--out",
                str(tmp_path / "out"),
                "--data",
                str(data),
            ]
        )
        == 0
    )
    assert (tmp_path / "out" / "CPU.png").exists()


def test_render_draws_the_data_rather_than_the_connecting_scene(tmp_path: Path) -> None:
    """Two renders of one screen, with and without data, must differ.

    Otherwise `--data` is a flag that reads a file and changes nothing, which
    is indistinguishable from working when every panel is a black circle.
    """
    path = write_virtual_config(tmp_path)
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"prom": {"cpu": 42.4}}))

    assert main(["render", "--config", str(path), "--out", str(tmp_path / "cold")]) == 0
    assert (
        main(
            ["render", "--config", str(path), "--out", str(tmp_path / "live"), "--data", str(data)]
        )
        == 0
    )
    cold = (tmp_path / "cold" / "CPU.png").read_bytes()
    live = (tmp_path / "live" / "CPU.png").read_bytes()
    assert cold != live


def test_render_reports_a_data_file_that_is_not_json(tmp_path: Path, capsys: Any) -> None:
    path = write_virtual_config(tmp_path)
    data = tmp_path / "data.json"
    data.write_text("{not json")

    assert (
        main(["render", "--config", str(path), "--out", str(tmp_path / "out"), "--data", str(data)])
        == 1
    )
    assert "Traceback" not in capsys.readouterr().err


def test_render_reports_an_out_path_it_cannot_write(tmp_path: Path, capsys: Any) -> None:
    path = write_virtual_config(tmp_path)
    out = tmp_path / "out"
    out.write_text("a file, not a directory")

    assert main(["render", "--config", str(path), "--out", str(out)]) == 1
    assert str(out) in capsys.readouterr().err


def test_identify_lights_every_panel_with_its_own_ordinal(tmp_path: Path) -> None:
    """The setup wizard's tool: a digit per panel, and no two the same.

    `--hold 0` is what makes it testable -- the operator's default is to hold
    the digits up until they interrupt it, which is the point of the command.
    """
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 0
    panels = sorted((tmp_path / "panels").glob("*.png"))
    assert [panel.name for panel in panels] == ["CPU.png", "HEALTH.png", "MEM.png", "PODS.png"]
    assert len({panel.read_bytes() for panel in panels}) == 4


def test_identify_prints_the_map_from_ordinal_to_screen(tmp_path: Path, capsys: Any) -> None:
    """Someone in front of the rack reads the panel; the map is how they use it."""
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    for ordinal, name in ((1, "CPU"), (2, "MEM"), (3, "PODS"), (4, "HEALTH")):
        assert any(line.startswith(f"{ordinal}  {name}") for line in lines), lines


def test_identify_blanks_the_panels_it_lit(tmp_path: Path, monkeypatch: Any) -> None:
    """A wizard that leaves four panels lit has left the rack worse than it found it."""
    displays: list[CountingDisplay] = []

    def display_factory(config: Any, name: str) -> CountingDisplay:
        displays.append(CountingDisplay())
        return displays[-1]

    monkeypatch.setattr("ors_daemon.__main__.build_display", display_factory)
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 0
    assert [(display.sleeps, display.closed) for display in displays] == [(1, 1)] * 4


def test_identify_holds_the_ordinals_until_a_signal_arrives(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The operator's own path: the digits stay up, and Ctrl-C is what ends them.

    The fake fires each handler the instant it is armed, which is a signal
    landing during the hold. A version that armed nothing would spend the whole
    `--hold` and then fail on the handlers, rather than hanging the suite --
    which is why the hold here is a number and not the real default of `None`.
    """
    calls = capture_firing_signals(monkeypatch)
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", str(WAIT)]) == 0
    assert {number for number, _ in calls[:2]} == {signal.SIGTERM, signal.SIGINT}
    assert len(list((tmp_path / "panels").glob("*.png"))) == 4


def test_identify_puts_the_signal_handlers_back(tmp_path: Path, monkeypatch: Any) -> None:
    """`identify` is a command, not a process.

    `main` returns, so whatever owned SIGINT before it -- a shell wrapper, an
    embedder, a test runner -- has to own it afterwards. A hold that *elapses*
    rather than being cut short by a signal would otherwise leave both
    dispositions pointing at a closure setting an `Event` nobody is waiting on:
    Ctrl-C would then do nothing at all. `run` is the opposite case and keeps
    its handlers, because the only thing after `run` is exit.
    """
    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    calls = capture_firing_signals(monkeypatch)
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", str(WAIT)]) == 0
    assert len(calls) == 4, calls
    assert dict(calls[-2:]) == before


def test_identify_leaves_the_processs_signals_alone_when_it_does_not_wait(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`--hold 0` paints and returns; nothing about that needs a SIGINT handler."""
    handlers = capture_signals(monkeypatch)
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 0
    assert handlers == {}


def test_identify_reports_a_panel_it_cannot_open(tmp_path: Path, capsys: Any) -> None:
    """One unopenable panel is three lit ones plus a message, not a crash."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    config = yaml.safe_load(EXAMPLE.read_text())
    for index, screen in enumerate(config["screens"]):
        out_dir = blocked / "panels" if index == 0 else tmp_path / "panels"
        screen["display"] = {"backend": "virtual", "out_dir": str(out_dir)}
    for integration in config["integrations"]:
        integration.pop("tunnel", None)
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 1
    assert "CPU" in capsys.readouterr().err
    assert len(list((tmp_path / "panels").glob("*.png"))) == 3


def test_identify_reports_a_panel_that_opens_but_will_not_take_a_frame(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """A dark panel with a line in the map beside it is worse than no line.

    The whole command is one claim -- "that panel is this screen" -- so a panel
    that took no frame must not be in the map, must be on stderr, and must not
    let the command exit 0.
    """
    displays: dict[str, CountingDisplay] = {}

    def build(config: Any, name: str) -> CountingDisplay:
        displays[name] = DeadDisplay() if name == "PODS" else CountingDisplay()
        return displays[name]

    monkeypatch.setattr("ors_daemon.__main__.build_display", build)
    path = write_virtual_config(tmp_path)

    assert main(["identify", "--config", str(path), "--hold", "0"]) == 1
    captured = capsys.readouterr()
    assert "PODS" in captured.err
    assert "3  PODS" not in captured.out
    assert "1  CPU" in captured.out
    # Opened is opened: a panel that would not draw is still closed on the way
    # out, or its serial device outlives the command that opened it.
    assert [(display.sleeps, display.closed) for display in displays.values()] == [(1, 1)] * 4


def test_run_installs_a_handler_for_both_signals_before_it_loops(
    tmp_path: Path, monkeypatch: Any
) -> None:
    handlers = capture_signals(monkeypatch)
    monkeypatch.setattr("ors_daemon.__main__.Supervisor", RecordingSupervisor)
    RecordingSupervisor.instances.clear()
    path = write_virtual_config(tmp_path)

    argv = ["run", "--config", str(path), "--status", str(tmp_path / "status.json")]
    # `--link` is pinned rather than left at its default, so this test says the
    # same thing on a Pi that really is paired as it does on a build machine.
    assert main([*argv, "--link", str(tmp_path / "link.json")]) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert supervisor.ran == 1
    assert supervisor.handlers_when_run == {signal.SIGTERM, signal.SIGINT}

    for number in (signal.SIGTERM, signal.SIGINT):
        handlers[number](number, None)
    assert supervisor.stops == 2


def test_run_writes_its_status_where_the_unit_file_expects_it_by_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    capture_signals(monkeypatch)
    monkeypatch.setattr("ors_daemon.__main__.Supervisor", RecordingSupervisor)
    RecordingSupervisor.instances.clear()
    path = write_virtual_config(tmp_path)

    assert main(["run", "--config", str(path), "--link", str(tmp_path / "link.json")]) == 0
    supervisor = RecordingSupervisor.instances[-1]
    assert supervisor.kwargs["status_path"] == DEFAULT_STATUS_PATH
    assert len(supervisor.kwargs["screens"]) == 4


def test_a_signal_blanks_and_sleeps_every_panel(tmp_path: Path, monkeypatch: Any) -> None:
    handlers = capture_signals(monkeypatch)
    supervisor, displays = virtual_supervisor(tmp_path)
    _install_signal_handlers(supervisor.stop)
    supervisor.start()

    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert [(display.sleeps, display.closed) for display in displays] == [(1, 1)] * 4


def test_a_second_signal_during_shutdown_does_not_sleep_a_panel_twice(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The interleaving a real rack produces: a signal lands on the thread that
    is already inside `stop`, so the handler re-enters it rather than racing it.
    A second sleep is a command written to a panel that is already off, and a
    second close is a command written to a torn-down device."""
    handlers = capture_signals(monkeypatch)
    supervisor, displays = virtual_supervisor(tmp_path)
    _install_signal_handlers(supervisor.stop)
    supervisor.start()
    displays[0].on_sleep = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)

    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert [(display.sleeps, display.closed) for display in displays] == [(1, 1)] * 4


def test_a_signal_landing_while_the_rack_is_coming_up_leaves_no_panel_lit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The window the README's own bring-up step walks the operator into.

    `ors-daemon run`, then Ctrl-C while the four GC9A01s are still opening --
    each one a hardware reset and a fifty-command init. The handler runs on the
    thread that is inside `start()`, so `stop` walks a slot list the panels
    still being opened are not in yet. Every panel that got as far as being
    opened has to be slept and closed regardless of when the signal landed.
    """
    handlers = capture_signals(monkeypatch)

    def signal_on(name: str) -> None:
        if name == "MEM":
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    supervisor, displays = virtual_supervisor(tmp_path, on_build=signal_on)
    _install_signal_handlers(supervisor.stop)

    supervisor.start()

    assert displays, "the test proves nothing if no panel was ever opened"
    assert [(display.sleeps, display.closed) for display in displays] == [(1, 1)] * len(displays)


def test_handlers_are_not_installed_off_the_main_thread() -> None:
    """`signal.signal` raises there, and a daemon embedded in something else --
    or a test -- must not die of it on the way to a working rack."""
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(_install_signal_handlers(lambda: None)))
    thread.start()
    thread.join(WAIT)

    assert result == [False]


def test_an_unknown_subcommand_exits_nonzero() -> None:
    assert main(["frobnicate"]) != 0


def test_no_subcommand_at_all_exits_nonzero() -> None:
    assert main([]) != 0


def test_help_exits_zero(capsys: Any) -> None:
    """`--help` is argparse exiting, not the daemon failing."""
    assert main(["--help"]) == 0
    assert "identify" in capsys.readouterr().out
