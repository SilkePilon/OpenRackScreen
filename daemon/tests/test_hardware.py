"""What a rack can say about its own hardware, and what proving one panel costs.

Detection is enumeration plus a guided probe, and the split is a fact about the
hardware rather than a design choice: a Pi can list `/dev/spidev*`, so it knows
which buses and chip selects exist, but a GC9A01 has no readable id over 4-wire
SPI and its DC and RST lines were chosen with a screwdriver. A panel cannot
introduce itself. So the operator supplies the wiring and a probe proves it, by
lighting the glass in front of them.

Nothing here touches SPI, sleeps to wait for time to pass, binds a port or reads
`/dev`. The panels are fakes handed in through the display factory,
`enumerate_panels` takes its root so a test can make up a device tree, and the
probe's hold is an injected sleeper that records the number it was asked for
instead of spending it.

**The fixture is deliberately non-identical.** Every number differs from every
other and from its own list index -- two buses, three chip selects, six GPIO
lines, two screen ids and two positions -- because the mistakes this file guards
against are a bus read as a chip select and a claim attributed to the wrong
screen, and a fixture where those coincide cannot see either. The two configured
screens are wired (bus 0, cs 1) and (bus 1, cs 3), so a build that swapped the
pair would address a device neither of them is on. `(0, 0)` is never a subject:
it is `DisplayConfig`'s own default, so a probe that ignored its arguments
entirely would land there and pass.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ors_daemon.__main__ import _link
from ors_daemon.clock import FakeClock
from ors_daemon.config import resolve_screens, system_scenes
from ors_daemon.displays import DisplayError
from ors_daemon.frames import FrameStream
from ors_daemon.hardware import detect_handler, enumerate_panels, probe_handler
from ors_daemon.link import LinkSettings
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import _BUS_GUARD_PER_SCREEN as BUS_GUARD_PER_SCREEN
from ors_daemon.supervisor import PROBE_HOLD_BUDGET, ProbeRefused, Supervisor
from ors_render import RenderContext, render_scene
from ors_schema.daemon import DaemonConfig, ScreenConfig
from ors_schema.link import (
    MAX_PANEL_CANDIDATES,
    MAX_PROBE_ERROR,
    MAX_PROBE_HOLD_S,
    MAX_SCREEN_NAME,
    DetectRequest,
    DetectResult,
    ProbeRequest,
    ProbeResult,
    parse_daemon_message,
)
from PIL import Image

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

CPU_BUS, CPU_CS = 0, 1
"""The device the screen named CPU is driving: /dev/spidev0.1."""
MEM_BUS, MEM_CS = 1, 3
"""And MEM, on the other bus and a different chip select."""
PROBE_BUS, PROBE_CS = 0, 2
"""The candidate: free, and a bus-mate of CPU -- which is what the guard is for."""
PROBE_DC, PROBE_RST = 25, 17
PROBE_HZ = 32_000_000
"""Not `DisplayConfig`'s default of 40 MHz, so a probe that dropped the clock
the operator chose opens a device this test can see is wrong."""
HOLD_S = 2.5
"""Shorter than `PROBE_HOLD_BUDGET`, so an honoured hold and a capped one differ."""
REQUEST_ID = "detect-7f3c"
SCREEN_NAMES = ("CPU", "MEM")


def screen(name: str, screen_id: int, position: int, bus: int, cs: int, dc: int, rst: int) -> Any:
    return {
        "id": screen_id,
        "name": name,
        "position": position,
        "display": {
            "backend": "gc9a01",
            "spi_bus": bus,
            "spi_cs": cs,
            "dc": dc,
            "rst": rst,
        },
        "template": "text-only",
        "params": {"big": name},
    }


def config_dict() -> dict[str, Any]:
    return {
        "version": 1,
        "timezone": "UTC",
        "night": {"enabled": False},
        "integrations": [],
        "screens": [
            screen("CPU", screen_id=41, position=6, bus=CPU_BUS, cs=CPU_CS, dc=22, rst=27),
            screen("MEM", screen_id=52, position=3, bus=MEM_BUS, cs=MEM_CS, dc=13, rst=19),
        ],
    }


def devices(root: Path, *names: str) -> Path:
    """A directory of character-device-shaped names. Empty files: nothing opens one."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).touch()
    return root


def spidevs(root: Path) -> Path:
    """The device tree of the rack this file's fixture describes, plus one spare."""
    return devices(
        root / "dev",
        f"spidev{CPU_BUS}.{CPU_CS}",
        f"spidev{PROBE_BUS}.{PROBE_CS}",
        f"spidev{MEM_BUS}.{MEM_CS}",
    )


class RecordingPanel:
    """A panel that records what it was asked to do, and in what order."""

    def __init__(self, on_show: Any = None) -> None:
        self.calls: list[str] = []
        self.images: list[Image.Image] = []
        self.show_error: Exception | None = None
        self.on_show = on_show

    def show(self, image: Image.Image) -> None:
        self.calls.append("show")
        self.images.append(image)
        if self.on_show is not None:
            self.on_show()
        if self.show_error is not None:
            raise self.show_error

    def sleep(self) -> None:
        self.calls.append("sleep")

    def wake(self) -> None:
        self.calls.append("wake")

    def close(self) -> None:
        self.calls.append("close")


class FakeMonotonic:
    """A monotonic clock a test can spend a bus guard on without waiting.

    Started away from nought so that "how much has been spent" is a subtraction
    rather than a reading.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StuckWorker:
    """A worker wedged inside an SPI write: it never comes off its panel.

    Which is the case the bus guard exists for and the one a healthy rack hides.
    `pause` takes the tick lock, so a worker stuck on the wire holds it until the
    timeout runs out and then answers False -- and spending exactly what it was
    granted is what makes the bound something a test can add up without any of it
    being real time. `resume` raises, because resuming a worker that never paused
    is the pairing bug that freezes three panels for the life of the process.
    """

    def __init__(self, clock: FakeMonotonic, name: str) -> None:
        self._clock = clock
        self.screen_name = name
        self.granted: list[float] = []
        self.held_off = False

    def pause(self, timeout: float) -> bool:
        self.granted.append(timeout)
        self._clock.advance(timeout)
        return False

    def resume(self) -> None:
        raise AssertionError("a worker that refused to pause must never be resumed")

    def join(self, timeout: float | None = None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


class Rack:
    """A real `Supervisor` over two fake panels, and what it opened for whom.

    A real one rather than a double, because every question here is about what
    `probe` does to a rack that is *already driving panels*: which workers came
    off the bus, whether the device was closed again, and what a claim is. A
    recording supervisor could only restate the test.
    """

    def __init__(
        self,
        tmp_path: Path,
        raw: dict[str, Any] | None = None,
        shutdown_clock: Any = None,
        open_error: Exception | None = None,
        show_error: Exception | None = None,
        on_probe_show: Any = None,
    ) -> None:
        self.raw = raw if raw is not None else config_dict()
        config = DaemonConfig.model_validate(self.raw)
        self.opened: list[tuple[ScreenConfig, str]] = []
        self.probe_panels: list[RecordingPanel] = []
        self.panels: dict[str, RecordingPanel] = {}
        self.holds: list[float] = []
        self.open_error = open_error
        self.show_error = show_error
        self.on_probe_show = on_probe_show
        extra: dict[str, Any] = {} if shutdown_clock is None else {"shutdown_clock": shutdown_clock}
        self.supervisor = Supervisor(
            config=config,
            screens=resolve_screens(config),
            store=SnapshotStore(),
            clock=FakeClock(NOW),
            status_path=tmp_path / "status.json",
            display_factory=self._open,
            sleeper=self.holds.append,
            **extra,
        )

    def _open(self, screen_config: ScreenConfig, name: str) -> RecordingPanel:
        self.opened.append((screen_config, name))
        probing = name not in SCREEN_NAMES
        if probing and self.open_error is not None:
            raise self.open_error
        panel = RecordingPanel(on_show=self.on_probe_show if probing else None)
        if probing:
            panel.show_error = self.show_error
            self.probe_panels.append(panel)
        self.panels[name] = panel
        return panel

    @property
    def probed(self) -> list[ScreenConfig]:
        """Every panel opened for something other than a configured screen."""
        return [config for config, name in self.opened if name not in SCREEN_NAMES]

    def held_off(self) -> list[bool]:
        return [worker.held_off for worker in self.supervisor.workers]

    def slot(self, name: str) -> Any:
        """The slot driving one named screen.

        By name and never by index: `resolve_screens` orders the rack by
        `position`, and this fixture's positions (6 and 3) are deliberately not
        the order the screens are written in -- a test that reached for
        `_slots[0]` would wedge the wrong panel, on the wrong bus, and pass or
        fail for a reason that has nothing to do with what it says.
        """
        return next(slot for slot in self.supervisor._slots if slot.screen.config.name == name)

    def probe(self, bus: int = PROBE_BUS, cs: int = PROBE_CS, hold_s: float = HOLD_S) -> None:
        self.supervisor.probe(
            bus=bus, cs=cs, dc=PROBE_DC, rst=PROBE_RST, hz=PROBE_HZ, hold_s=hold_s
        )


def probe_request(bus: int = PROBE_BUS, cs: int = PROBE_CS, hold_s: float = HOLD_S) -> ProbeRequest:
    return ProbeRequest(
        request_id=REQUEST_ID,
        bus=bus,
        cs=cs,
        dc=PROBE_DC,
        rst=PROBE_RST,
        hz=PROBE_HZ,
        hold_s=hold_s,
    )


def claims(result: DetectResult) -> list[tuple[int, int, str | None]]:
    return [(panel.bus, panel.cs, panel.claimed_by) for panel in result.panels]


# --- enumeration ------------------------------------------------------------


def test_enumeration_reads_the_devices_that_exist(tmp_path: Path) -> None:
    for name in ("spidev0.0", "spidev0.1", "spidev1.0"):
        (tmp_path / name).touch()
    assert enumerate_panels(tmp_path) == [(0, 0), (0, 1), (1, 0)]


def test_enumeration_names_only_spi_devices(tmp_path: Path) -> None:
    """`/dev` on a Pi holds several hundred entries, and three of them are these."""
    devices(
        tmp_path,
        "spidev0.1",
        "spidevfoo.bar",
        "spidev2",
        "spi0.1",
        "null",
        "i2c-1",
        "spidev1.3.old",
    )

    assert enumerate_panels(tmp_path) == [(0, 1)]


def test_enumeration_orders_by_number_and_not_by_name(tmp_path: Path) -> None:
    """`spidev10.0` sorts before `spidev2.0` as text, and the interface renders
    this list in the order it arrives in."""
    devices(tmp_path, "spidev10.0", "spidev2.0", "spidev2.10", "spidev2.2")

    assert enumerate_panels(tmp_path) == [(2, 0), (2, 2), (2, 10), (10, 0)]


def test_a_machine_with_no_device_tree_at_all_enumerates_nothing(tmp_path: Path) -> None:
    """An empty list is a real answer -- "this rack has no SPI" -- and a raise
    here would be a detect that times out on a machine that simply has none."""
    assert enumerate_panels(tmp_path / "not-a-directory") == []


# --- what the rack already claims -------------------------------------------


def test_a_device_the_rack_is_already_driving_is_named(tmp_path: Path) -> None:
    """`claimed_by` is the screen's name, so the wizard can say why it is unavailable."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        assert rack.supervisor.claimed_devices() == {
            (CPU_BUS, CPU_CS): "CPU",
            (MEM_BUS, MEM_CS): "MEM",
        }

        result = detect_handler(rack.supervisor, spidevs(tmp_path))(
            DetectRequest(request_id=REQUEST_ID)
        )

        assert result.request_id == REQUEST_ID
        assert claims(result) == [
            (CPU_BUS, CPU_CS, "CPU"),
            (PROBE_BUS, PROBE_CS, None),
            (MEM_BUS, MEM_CS, "MEM"),
        ]
    finally:
        rack.supervisor.stop()


def test_a_device_nothing_is_driving_is_free_and_says_so_with_null(tmp_path: Path) -> None:
    """Never `""`. It is falsy, so every `if candidate.claimed_by:` downstream
    reads an empty claim as free -- and `PanelCandidate.claimed_by` refuses one,
    which would take the whole `DetectResult` with it and answer a timeout."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        result = detect_handler(rack.supervisor, spidevs(tmp_path))(
            DetectRequest(request_id=REQUEST_ID)
        )

        free = [panel for panel in result.panels if (panel.bus, panel.cs) == (PROBE_BUS, PROBE_CS)]
        assert [panel.claimed_by for panel in free] == [None]
        assert all(panel.claimed_by != "" for panel in result.panels)
    finally:
        rack.supervisor.stop()


def test_a_screen_that_drives_no_spi_device_claims_none(tmp_path: Path) -> None:
    """A virtual panel is a directory of PNGs. Claiming SPI0.0 for it -- the
    unset default of `spi_bus` and `spi_cs` -- would hide a real device."""
    raw = config_dict()
    raw["screens"][1]["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    rack = Rack(tmp_path, raw=raw)
    rack.supervisor.start()
    try:
        assert rack.supervisor.claimed_devices() == {(CPU_BUS, CPU_CS): "CPU"}
    finally:
        rack.supervisor.stop()


def test_a_name_too_long_for_the_wire_is_shortened_rather_than_refused(tmp_path: Path) -> None:
    """`ScreenConfig.name` has no upper bound and `PanelCandidate.claimed_by`
    has one, so a hand-written YAML with a paragraph for a name would make every
    `DetectResult` unparseable and the wizard would show a timeout."""
    raw = config_dict()
    raw["screens"][0]["name"] = "C" * (MAX_SCREEN_NAME + 40)
    rack = Rack(tmp_path, raw=raw)
    rack.supervisor.start()
    try:
        result = detect_handler(rack.supervisor, spidevs(tmp_path))(
            DetectRequest(request_id=REQUEST_ID)
        )

        assert isinstance(parse_daemon_message(result.model_dump_json()), DetectResult)
        assert [panel.claimed_by for panel in result.panels] == [
            "C" * MAX_SCREEN_NAME,
            None,
            "MEM",
        ]
    finally:
        rack.supervisor.stop()


def test_more_devices_than_the_wire_carries_are_cut_rather_than_refused(tmp_path: Path) -> None:
    """The same bargain: a rack whose overlays expose more devices than
    `MAX_PANEL_CANDIDATES` must answer with the ones it can name."""
    root = devices(tmp_path / "dev", *[f"spidev0.{cs}" for cs in range(MAX_PANEL_CANDIDATES + 6)])
    rack = Rack(tmp_path)

    result = detect_handler(rack.supervisor, root)(DetectRequest(request_id=REQUEST_ID))

    assert len(result.panels) == MAX_PANEL_CANDIDATES
    assert isinstance(parse_daemon_message(result.model_dump_json()), DetectResult)


# --- the probe --------------------------------------------------------------


def test_probing_a_claimed_device_is_refused_rather_than_fought_over(tmp_path: Path) -> None:
    """A live worker owns that SPI device. Taking it would be a torn frame at
    best and a wedged bus at worst."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        with pytest.raises(ProbeRefused) as refusal:
            rack.probe(bus=CPU_BUS, cs=CPU_CS)

        assert "CPU" in str(refusal.value), "the refusal has to say which screen has it"
        assert rack.probed == [], "the device was opened out from under its worker"
        assert rack.holds == [], "and nothing was held up for it"
        assert rack.held_off() == [False, False], "no worker was taken off its panel either"
    finally:
        rack.supervisor.stop()


def test_a_probe_holds_every_worker_on_that_bus_off_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE M2 LESSON. Interleaving an init sequence with a bus-mate's frame
    produced a pale grey rectangle, non-deterministically, and it stayed wrong
    because the init registers were wrong rather than the framebuffer.

    Asserted at the moment it matters -- when the candidate is written to --
    rather than afterwards, and per screen with the bound `apply` uses.
    """
    granted: list[float] = []
    paused = ScreenWorker.pause

    def pause(self: ScreenWorker, timeout: float) -> bool:
        granted.append(timeout)
        return paused(self, timeout)

    monkeypatch.setattr(ScreenWorker, "pause", pause)

    during: list[list[bool]] = []
    rack = Rack(tmp_path)
    rack.on_probe_show = lambda: during.append(rack.held_off())
    rack.supervisor.start()
    try:
        rack.probe()

        assert during == [[True, True]], "the rack was on the bus while the candidate was lit"
        assert granted == [BUS_GUARD_PER_SCREEN, BUS_GUARD_PER_SCREEN], (
            "each kept worker is promised a real hold, and the same one an apply promises"
        )
        assert rack.held_off() == [False, False], "and every one of them got its panel back"
    finally:
        rack.supervisor.stop()


def test_a_worker_that_will_not_come_off_the_bus_refuses_the_probe(tmp_path: Path) -> None:
    """Better a refused probe than a corrupted panel.

    `_off_the_bus` answers with the buses it could not guard, which is the same
    signal `apply` reads before it decides not to open a fresh screen. A probe
    is that decision made deliberately, so it has to read the same answer.
    """
    clock = FakeMonotonic()
    rack = Rack(tmp_path, shutdown_clock=clock)
    rack.supervisor.start()
    try:
        wedged = StuckWorker(clock, "CPU")
        rack.slot("CPU").worker = wedged

        with pytest.raises(ProbeRefused) as refusal:
            rack.probe()

        assert "bus" in str(refusal.value), "the reason is the bus, not the candidate"
        assert rack.probed == [], "a panel was initialised on a bus still being drawn on"
        assert rack.holds == []
        assert wedged.granted == [BUS_GUARD_PER_SCREEN], "it was asked, with the apply's bound"
    finally:
        rack.supervisor.stop()


def test_a_probe_closes_the_device_afterwards(tmp_path: Path) -> None:
    """Otherwise a probed panel stays claimed and the next apply cannot open it."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        rack.probe()

        assert [panel.calls for panel in rack.probe_panels] == [["show", "sleep", "close"]]
    finally:
        rack.supervisor.stop()


def test_a_probe_closes_the_device_even_when_the_paint_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close is on every path out, not on the happy one.

    A render that raises is a bug rather than a state -- `ScreenWorker._show`
    absorbs a backend that refuses a frame, and nothing absorbs this -- so it
    travels, and it must not travel over a serial device this daemon still holds.
    """

    def explode(self: ScreenWorker, ordinal: str, timeout: float | None = None) -> bool:
        raise RuntimeError("the renderer is on fire")

    monkeypatch.setattr(ScreenWorker, "identify", explode)
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        with pytest.raises(RuntimeError):
            rack.probe()

        assert [panel.calls for panel in rack.probe_panels] == [["sleep", "close"]]
        assert rack.held_off() == [False, False], "and the rack got its bus back"
    finally:
        rack.supervisor.stop()


def test_a_probe_that_cannot_open_the_device_reports_why(tmp_path: Path) -> None:
    """Which is the answer the operator ran the probe to get: a `dc` on the wrong
    header pin, a ribbon that is not seated, a bus the overlay never enabled."""
    rack = Rack(tmp_path, open_error=DisplayError("no such device: /dev/spidev0.2"))
    rack.supervisor.start()
    try:
        result = probe_handler(rack.supervisor)(probe_request())

        assert result.ok is False
        assert result.request_id == REQUEST_ID
        assert result.error is not None and "spidev0.2" in result.error
        assert rack.held_off() == [False, False], "the guard let go of a probe that failed"
    finally:
        rack.supervisor.stop()


def test_a_panel_that_opens_and_will_not_take_the_frame_is_not_a_pass(tmp_path: Path) -> None:
    """A ribbon seated well enough to enumerate the device and not well enough to
    clock a frame out of it. `ok` means the pattern was written, and it was not."""
    rack = Rack(tmp_path, show_error=DisplayError("the ribbon is not seated"))
    rack.supervisor.start()
    try:
        result = probe_handler(rack.supervisor)(probe_request())

        assert result.ok is False
        assert result.error is not None and "frame" in result.error
        assert [panel.calls for panel in rack.probe_panels] == [["show", "sleep", "close"]]
    finally:
        rack.supervisor.stop()


def test_a_probe_opens_the_device_the_request_named(tmp_path: Path) -> None:
    """Every one of the five numbers, because every one of them is plausible on
    its own: bus 1 and chip select 1 exist on a Pi, GPIO 17 is a real line, and
    40 MHz -- the schema default this deliberately is not -- opens perfectly."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        rack.probe()

        assert [
            (
                config.display.backend,
                config.display.spi_bus,
                config.display.spi_cs,
                config.display.dc,
                config.display.rst,
                config.display.hz,
            )
            for config in rack.probed
        ] == [("gc9a01", PROBE_BUS, PROBE_CS, PROBE_DC, PROBE_RST, PROBE_HZ)]
    finally:
        rack.supervisor.stop()


def test_a_probe_paints_the_pattern_identify_paints(tmp_path: Path) -> None:
    """One painter, not two. Somebody standing at the rack has seen this pattern
    already -- it is what the identify button draws -- and a second one drifting
    from the first is two answers to "which panel is that"."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        rack.probe()

        expected = render_scene(
            system_scenes()["identify"],
            RenderContext(data={"params": {"ordinal": f"{PROBE_BUS}.{PROBE_CS}"}}),
        )
        painted = [image for panel in rack.probe_panels for image in panel.images]
        assert [image.tobytes() for image in painted] == [expected.tobytes()]
    finally:
        rack.supervisor.stop()


def test_a_probe_that_lit_the_panel_says_so_and_carries_no_reason(tmp_path: Path) -> None:
    """`error` is commentary on a failure. A success that carried one would have
    every reader deciding for itself which of the two fields to believe."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        result = probe_handler(rack.supervisor)(probe_request())

        assert (result.ok, result.error) == (True, None)
        assert isinstance(parse_daemon_message(result.model_dump_json()), ProbeResult)
    finally:
        rack.supervisor.stop()


def test_a_probe_holds_the_pattern_up_for_as_long_as_it_was_asked(tmp_path: Path) -> None:
    """Nobody can walk a rack in the time a paint takes, so the wait is the point."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        rack.probe(hold_s=HOLD_S)

        assert rack.holds == [HOLD_S]
    finally:
        rack.supervisor.stop()


def test_a_hold_longer_than_this_rack_can_afford_is_cut_to_what_it_can(tmp_path: Path) -> None:
    """The wire allows thirty seconds; this Pi does not.

    The hold happens with the bus guard held and `_shutdown_lock` taken, so it is
    a rack-wide stall *and* time a SIGTERM waits out -- systemd's patience is
    `TimeoutStopSec`, and the shutdown that follows still needs its own ten
    seconds. Two bounds on purpose: `MAX_PROBE_HOLD_S` owns what the message may
    say, and this owns what the hardware can be asked for.
    """
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        assert PROBE_HOLD_BUDGET < MAX_PROBE_HOLD_S, "the wire may ask for more than this rack has"

        rack.probe(hold_s=MAX_PROBE_HOLD_S)

        assert rack.holds == [PROBE_HOLD_BUDGET]
    finally:
        rack.supervisor.stop()


@pytest.mark.parametrize(
    ("asked", "held"),
    [(0.0, 0.0), (-1.0, 0.0), (float("nan"), 0.0), (float("inf"), PROBE_HOLD_BUDGET)],
)
def test_a_hold_that_is_not_a_duration_is_not_waited_out(
    tmp_path: Path, asked: float, held: float
) -> None:
    """Zero is legitimate -- a script does not look at the glass -- and the other
    three cannot arrive off the wire, because `ProbeRequest.hold_s` refuses them.

    Answered here anyway, and NaN is the one that needs saying: it compares False
    against everything, so `min` hands it straight back and every bound written as
    a comparison lets it through. `frames._capped` records the same trap costing a
    Pi's CPU; here it would be a wait this module has no answer for, taken with
    four panels frozen behind it. `probe` is a public method, and a guard that
    depends on its caller having validated the argument is not a guard.
    """
    rack = Rack(tmp_path)
    rack.supervisor.start()
    try:
        rack.probe(hold_s=asked)

        assert rack.holds == [held]
    finally:
        rack.supervisor.stop()


def test_probing_a_rack_that_is_shutting_down_touches_no_panel(tmp_path: Path) -> None:
    """The panels are slept and their serial devices closed by then, and on a
    GC9A01 a command written to one that has been torn down is a write to
    nothing. A probe arrives on the link thread, which is where the stop event is
    consulted, so the message really can land here."""
    rack = Rack(tmp_path)
    rack.supervisor.start()
    rack.supervisor.stop()

    result = probe_handler(rack.supervisor)(probe_request())

    assert result.ok is False
    assert result.error is not None and "stopping" in result.error
    assert rack.probed == []


def test_a_reason_too_long_for_the_wire_is_shortened_rather_than_refused(tmp_path: Path) -> None:
    """A refused reply is strictly worse than a shortened one: the server drops
    the message, its wait expires, and a probe that failed for a reason this
    daemon knew answers with a timeout instead."""
    rack = Rack(tmp_path, open_error=DisplayError("E" * (MAX_PROBE_ERROR * 4)))
    rack.supervisor.start()
    try:
        result = probe_handler(rack.supervisor)(probe_request())

        assert result.error is not None and len(result.error) == MAX_PROBE_ERROR
        assert isinstance(parse_daemon_message(result.model_dump_json()), ProbeResult)
    finally:
        rack.supervisor.stop()


def test_a_failure_with_nothing_to_say_still_says_something(tmp_path: Path) -> None:
    """`ProbeResult.error` refuses `""` -- it is falsy, so a refusal with a
    zero-length reason reads as no reason at all -- and plenty of driver
    exceptions carry no message. The class name is what is left, and it is more
    than a timeout."""
    rack = Rack(tmp_path, open_error=DisplayError())
    rack.supervisor.start()
    try:
        result = probe_handler(rack.supervisor)(probe_request())

        assert result.ok is False
        assert result.error == "DisplayError"
        assert isinstance(parse_daemon_message(result.model_dump_json()), ProbeResult)
    finally:
        rack.supervisor.stop()


# --- the wiring -------------------------------------------------------------


class _RecordingLink:
    """A link that dials nothing and records how it was built."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = 0

    def start(self) -> None:
        self.started += 1


def test_the_detect_and_probe_handlers_are_wired_into_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M3a defect, which was an argument and not a handler.

    `_link` built `LinkClient` with `on_snapshot`, `on_frames_request` and
    `on_link_down` and never passed `on_command`: four commands were inert while
    the route answered `delivered: true`. A double standing in for the handler
    would have passed against that build, because the handler worked -- it was
    never handed over. So this asks the *call* what it was given, and then asks
    the thing it was given to do the real work against a real rack.
    """
    built: list[_RecordingLink] = []

    def link_client(**kwargs: Any) -> _RecordingLink:
        built.append(_RecordingLink(**kwargs))
        return built[-1]

    monkeypatch.setattr("ors_daemon.__main__.LinkClient", link_client)
    monkeypatch.setattr("ors_daemon.__main__.SPI_ROOT", spidevs(tmp_path))
    rack = Rack(tmp_path)
    rack.supervisor.start()
    frames = FrameStream()
    try:
        client = _link(
            argparse.Namespace(link=tmp_path / "link.json"),
            LinkSettings(
                server_url="http://server:8080", cache_path=tmp_path / "cache.json", key="k9"
            ),
            rack.supervisor,
            FakeClock(NOW),
            None,
            frames,
        )

        assert client is not None
        kwargs = built[0].kwargs
        assert kwargs["on_detect"] is not None, (
            "a detect nothing answers is a request that times out"
        )
        assert kwargs["on_probe"] is not None, "and so is a probe"

        detected = kwargs["on_detect"](DetectRequest(request_id=REQUEST_ID))
        assert claims(detected) == [
            (CPU_BUS, CPU_CS, "CPU"),
            (PROBE_BUS, PROBE_CS, None),
            (MEM_BUS, MEM_CS, "MEM"),
        ]
        proved = kwargs["on_probe"](probe_request())
        assert (proved.ok, proved.request_id) == (True, REQUEST_ID)
        assert [panel.calls for panel in rack.probe_panels] == [["show", "sleep", "close"]]
    finally:
        frames.close()
        rack.supervisor.stop()
