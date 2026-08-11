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
from ors_daemon.config import config_fingerprint, resolve_screens
from ors_daemon.displays import DisplayBackend, build_display
from ors_daemon.integrations.prometheus import PrometheusIntegration
from ors_daemon.poller import Poller
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
"""One poll of the author's cluster, in the shape `prometheus.py` publishes.

Not a claim: `test_the_namespace_prometheus_publishes_is_the_one_the_screens_bind`
puts a real `PrometheusIntegration` and a real `Poller` in front of a store and
asserts they produce exactly this. Without that, a change to what `reduce: top`
publishes would leave every test in this file passing against a shape the daemon
no longer writes.
"""

CLUSTER = {
    # What Prometheus answers each of the shipped config's queries with, keyed by
    # the field the query belongs to. Strings, because that is what a Prometheus
    # sample is on the wire -- `[timestamp, "42.4"]` -- and turning them into
    # numbers is part of what the integration is being tested for here.
    "cpu": ("42.4", None),
    # Two nodes, so the `top` reduction has something to choose between and the
    # `last_octet` strip has a real `instance` label to shorten. The peak is not
    # the first series, so a reduction that took `results[0]` would fail.
    "cpu_hot": (None, (("192.168.1.4:9100", "40.1"), ("192.168.1.5:9100", "71.2"))),
    "mem": ("61.2", None),
    "mem_used_gb": ("19.4", None),
    "mem_total_gb": ("32", None),
    "mem_hot": (None, (("192.168.1.3:9100", "51.5"), ("192.168.1.7:9100", "78"))),
    "pods_run": ("38", None),
    "pods_tot": ("41", None),
    "nodes_ready": ("3", None),
    "nodes_total": ("3", None),
    "alerts": ("0", None),
}


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakePrometheus:
    """A Prometheus's HTTP surface, and nothing else in the chain.

    The session is the only fake in this seam: the integration parses these
    envelopes for real, the poller publishes for real, and the store hands the
    result to four real screen workers. Queries are looked up by the *config's*
    own query strings, so a field renamed or a query edited in `rack.yaml`
    surfaces here as a missing key rather than as a silently different reading.
    """

    def __init__(self, config: Any) -> None:
        self._by_query = {spec.query: (name, CLUSTER[name]) for name, spec in config.fields.items()}
        self.urls: list[str] = []

    def get(self, url: str, params: dict[str, str], timeout: float) -> FakeResponse:
        self.urls.append(url)
        _, (value, series) = self._by_query[params["query"]]
        rows = (
            [{"metric": {}, "value": [0, value]}]
            if series is None
            else [
                {"metric": {"instance": instance}, "value": [0, sample]}
                for instance, sample in series
            ]
        )
        return FakeResponse({"status": "success", "data": {"resultType": "vector", "result": rows}})


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


def config_of(tmp_path: Path) -> DaemonConfig:
    """`examples/rack.yaml`, with the two things a rackless machine cannot honour.

    The GC9A01 panels become virtual ones and the `kubectl` tunnel is removed --
    the second removed rather than faked, because a test does not spawn kubectl.
    Everything else is the shipped file's own, including the integration's `url`,
    which is what the daemon polls once the tunnel it would otherwise defer to is
    gone.
    """
    raw = yaml.safe_load(EXAMPLE.read_text())
    for screen in raw["screens"]:
        screen["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    for integration in raw["integrations"]:
        integration.pop("tunnel", None)
    return DaemonConfig.model_validate(raw)


def rack(
    tmp_path: Path, now: datetime
) -> tuple[Supervisor, SnapshotStore, dict[str, SignallingDisplay]]:
    """The shipped rack on virtual panels, with no tunnel and no poller."""
    config = config_of(tmp_path)

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
    assert status["config_schema_version"] == 1
    # The schema version is a constant on every rack that has ever validated;
    # this is the field that says *which* config the Pi is running.
    assert status["config_fingerprint"] == config_fingerprint(config_of(tmp_path))


def test_the_namespace_prometheus_publishes_is_the_one_the_screens_bind(tmp_path: Path) -> None:
    """The seam the rest of this file assumes: Prometheus, poller, store, panels.

    Every other end-to-end test injects `HEALTHY` into the store directly and
    claims in a comment that it is what `prometheus.py` publishes. Nothing
    checked that, so a change to what `reduce: top` returns -- the nested
    `{"node": ..., "value": ...}` four `{{prom.cpu_hot.node}}` bindings read
    through -- would have left all of them passing while the rack drew `--`.

    Only the HTTP session is faked here. The integration is the real one built
    from the shipped config's own PromQL, the poller is the real one, the store
    is the real one, and the assertion is that what comes out the far end is
    exactly the namespace the shipped screens bind against.
    """
    config = config_of(tmp_path)
    integration_config = config.integrations[0]
    server = FakePrometheus(integration_config)
    supervisor, store, displays = rack(tmp_path, NOON)
    poller = Poller(
        integration=PrometheusIntegration(integration_config, session=server),
        store=store,
        interval=integration_config.poll_interval,
        stop=threading.Event(),
        clock=FakeClock(NOON),
    )

    supervisor.start()
    try:
        assert all(display.wait_for_frames(1) for display in displays.values())
        # One cycle, on this thread: the poller's loop is what a started thread
        # would add, and it would add a wait rather than a fact.
        poller.poll_once()
        assert all(display.wait_for_frames(2) for display in displays.values())
        settle(supervisor)
        supervisor.tick()
    finally:
        supervisor.stop()

    assert store.read().data["prom"] == HEALTHY, "the literal the other tests inject"
    assert server.urls == [f"{integration_config.url}/api/v1/query"] * len(
        integration_config.fields
    ), "with no tunnel, the configured `url` is what is polled"

    status = status_of(tmp_path)
    assert status["integrations"][0]["state"] == "healthy"
    # And the panels drew their own scenes from it rather than falling back to
    # `stale`, which is what a namespace of the wrong shape would produce.
    screens = {screen["name"]: screen["scene"] for screen in status["screens"]}
    assert screens == {"CPU": "default", "MEM": "default", "PODS": "default", "HEALTH": "nodes"}


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
