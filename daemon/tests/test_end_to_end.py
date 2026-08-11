"""The whole daemon, from the config the rack actually runs to four PNGs.

Every other test file proves one part in isolation. This one proves the parts
still fit: `examples/rack.yaml` is loaded unedited except for the two things a
machine with no rack cannot honour -- the GC9A01 panels become virtual ones,
and the `kubectl` tunnel is removed -- and the supervisor is then asked to
drive it. The screens, their templates, their PromQL-bound parameters, the
night window and the timezone are the shipped file's own.

Nothing here sleeps to wait for time to pass. A frame is waited for on the
condition the panel wrapper signals, and a worker's own lock is what says the
tick behind that frame has finished writing down what it drew.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from ors_daemon.clock import FakeClock
from ors_daemon.config import resolve_screens
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor
from ors_schema.daemon import DaemonConfig, ScreenConfig
from PIL import Image

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rack.yaml"

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
"""The shipped config's own timezone, so these clocks read as the rack's do."""

NOON = datetime(2026, 8, 11, 12, 0, tzinfo=AMSTERDAM)
MIDNIGHT = datetime(2026, 8, 11, 23, 30, tzinfo=AMSTERDAM)
"""Inside the shipped night window (23:00-07:00), where NOON is outside it."""

WAIT = 5.0
"""Generous on purpose: a passing test never spends it, only a broken one does."""

PANELS = ("CPU", "MEM", "PODS", "HEALTH")

HEALTHY = {
    "cpu": 42.4,
    "cpu_hot": {"node": ".5", "value": 71.2},
    "mem": 61.2,
    "mem_used_gb": 19.4,
    "mem_total_gb": 32.0,
    "mem_hot": {"node": ".7", "value": 78.0},
    "pods_run": 38,
    "pods_tot": 41,
    "nodes_ready": 3,
    "nodes_total": 3,
    "alerts": 0,
}
"""One poll of the author's cluster, in the shape `prometheus.py` publishes."""


class SignallingDisplay:
    """A real backend, plus a way to wait for a frame instead of polling for one.

    Wrapping rather than replacing keeps the config's own backend choice on the
    path: the virtual backend underneath still writes the PNGs these tests open.
    """

    def __init__(self, inner: DisplayBackend) -> None:
        self._inner = inner
        self._condition = threading.Condition()
        self.frames = 0
        self.sleeps = 0
        self.closes = 0

    def show(self, image: Image.Image) -> None:
        self._inner.show(image)
        with self._condition:
            self.frames += 1
            self._condition.notify_all()

    def sleep(self) -> None:
        self._inner.sleep()
        with self._condition:
            self.sleeps += 1
            self._condition.notify_all()

    def wake(self) -> None:
        self._inner.wake()

    def close(self) -> None:
        self._inner.close()
        self.closes += 1

    def wait_for_frames(self, count: int) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self.frames >= count, timeout=WAIT)

    def wait_for_sleep(self) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self.sleeps >= 1, timeout=WAIT)


def rack(
    tmp_path: Path, now: datetime
) -> tuple[Supervisor, SnapshotStore, dict[str, SignallingDisplay]]:
    """The shipped rack on virtual panels, with no tunnel and no poller."""
    raw = yaml.safe_load(EXAMPLE.read_text())
    for screen in raw["screens"]:
        screen["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    for integration in raw["integrations"]:
        # Removed rather than faked: a test does not spawn `kubectl`.
        integration.pop("tunnel", None)
    config = DaemonConfig.model_validate(raw)

    store = SnapshotStore()
    displays: dict[str, SignallingDisplay] = {}

    def display_factory(screen_config: ScreenConfig, name: str) -> SignallingDisplay:
        displays[name] = SignallingDisplay(build_display(screen_config.display, name))
        return displays[name]

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(now),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        # `None` starts no poller, so no socket is opened. The data arrives
        # through the store instead, which is the only thing a poller does.
        poller_factory=lambda integration_config, url_provider: None,
    )
    return supervisor, store, displays


def settle(supervisor: Supervisor) -> None:
    """Block until every worker's current tick has finished. Nothing polls.

    A `ScreenWorker` holds its own lock across a whole tick -- select, render,
    show, and then the counters and scene name the status file reports -- and
    `show` is what the wrapper above signals from. Taking that lock afterwards
    therefore returns only once the tick that drew the frame has finished
    writing down what it drew, which is what makes the assertions below exact
    rather than merely very likely.
    """
    for worker in supervisor.workers:
        with worker._lock:
            pass


def status_of(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "status.json").read_text())


def test_a_cold_rack_shows_connecting_on_every_panel(tmp_path: Path) -> None:
    """Before any data arrives, all four panels say so -- none of them is blank."""
    supervisor, _, displays = rack(tmp_path, NOON)
    supervisor.start()
    try:
        assert not supervisor.tunnels, "the shipped tunnel must not be launched by a test"
        assert all(display.wait_for_frames(1) for display in displays.values())
        settle(supervisor)
        supervisor.tick()
    finally:
        supervisor.stop()

    screens = {screen["name"]: screen for screen in status_of(tmp_path)["screens"]}
    assert set(screens) == set(PANELS)
    assert all(screen["scene"] == "connecting" for screen in screens.values())
    assert status_of(tmp_path)["integrations"][0] == {
        "name": "prom",
        "state": "connecting",
        "stale": False,
        "latency_ms": None,
        "last_success": None,
        "last_error": None,
    }


def test_the_whole_rack_renders_from_the_example_config(tmp_path: Path) -> None:
    """One poll of data in; four panels and a status file that describes them out."""
    supervisor, store, displays = rack(tmp_path, NOON)
    supervisor.start()
    try:
        assert all(display.wait_for_frames(1) for display in displays.values())
        store.put("prom", HEALTHY, latency_ms=5.0, now=NOON)
        assert all(display.wait_for_frames(2) for display in displays.values())
        settle(supervisor)
        supervisor.tick()
    finally:
        supervisor.stop()

    panels = sorted((tmp_path / "panels").glob("*.png"))
    assert [panel.name for panel in panels] == ["CPU.png", "HEALTH.png", "MEM.png", "PODS.png"]
    for panel in panels:
        assert Image.open(panel).size == (240, 240)
    # Four different readings drawn by three different templates: identical
    # bytes would mean the parameters never reached the glass.
    assert len({panel.read_bytes() for panel in panels}) == 4

    status = status_of(tmp_path)
    screens = {screen["name"]: screen for screen in status["screens"]}
    assert {name: screen["scene"] for name, screen in screens.items()} == {
        "CPU": "default",
        "MEM": "default",
        "PODS": "default",
        "HEALTH": "nodes",
    }
    for screen in screens.values():
        assert screen["state"] == "awake"
        assert screen["error"] is None
        # Exactly two: `connecting`, then the reading. The clock does not move,
        # so nothing else can have asked for a frame.
        assert screen["renders"] == 2
    assert status["integrations"][0]["state"] == "healthy"
    assert status["integrations"][0]["latency_ms"] == 5.0
    assert status["config_version"] == 1


def test_the_rack_sleeps_inside_the_shipped_night_window(tmp_path: Path) -> None:
    """23:30 is inside the config's own 23:00-07:00, so nothing is drawn at all."""
    supervisor, _, displays = rack(tmp_path, MIDNIGHT)
    supervisor.start()
    try:
        assert all(display.wait_for_sleep() for display in displays.values())
        settle(supervisor)
        supervisor.tick()
    finally:
        supervisor.stop()

    assert list((tmp_path / "panels").glob("*.png")) == [], "a sleeping rack draws nothing"
    for screen in status_of(tmp_path)["screens"]:
        assert screen["state"] == "asleep"
        assert screen["scene"] is None
        assert screen["renders"] == 0


def test_shutting_down_sleeps_and_closes_every_panel(tmp_path: Path) -> None:
    """What SIGTERM buys: no panel is left lit once the daemon is gone."""
    supervisor, _, displays = rack(tmp_path, NOON)
    supervisor.start()
    assert all(display.wait_for_frames(1) for display in displays.values())

    supervisor.stop()

    assert [(display.sleeps, display.closes) for display in displays.values()] == [(1, 1)] * 4
