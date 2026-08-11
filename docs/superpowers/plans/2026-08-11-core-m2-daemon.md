# Core M2 — Standalone Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daemon that runs on a Raspberry Pi from a hand-written YAML config, polls Prometheus, renders four screens with `ors-render`, drives GC9A01 panels over SPI, and sleeps at night — with no server involved.

**Architecture:** One process, threads only. One thread per integration, one per screen, one supervisor. The single shared structure is a lock-protected snapshot store holding data namespaces, a monotonic version counter, and per-integration health; screen workers wait on its condition variable and re-render when something actually changed. Display backends are pure transport behind a four-method protocol, so the whole daemon runs headless on a laptop.

**Tech Stack:** Python 3.11+, `uv` workspace, pydantic v2, PyYAML, requests, luma.lcd/luma.core, Pillow, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-11-core-m2-daemon-design.md`
**Depends on:** M1, merged — `ors-render` (`render_screen`, `select_scene`, `RenderContext`, `load_builtin_templates`, `Geometry`) and `ors-schema` (scene/element/template models, `Template.bind_params`).

## Global Constraints

- **Research before implementing.** Spec §0 applies to every task. Verify luma.lcd's current `spi()` signature and framebuffer API, GC9A01 sleep/wake timing (the datasheet requires ≥120 ms after sleep-out), Prometheus's `/api/v1/query` response shapes including how `NaN` appears on the wire, `kubectl port-forward` behaviour on connection loss, and whether `tzdata` must be installed explicitly on Raspberry Pi OS. Where research contradicts this plan, the research wins — raise it, then implement.
- **TDD.** Failing test first, watch it fail for the expected reason, minimal implementation, watch it pass, commit. No exceptions.
- **No test may sleep to wait for time to pass.** The clock is injected everywhere. Night transitions, backoff and pacing are tested by advancing a fake clock. A test that calls `time.sleep` to let a thread progress is a plan failure — use `threading.Event` handshakes.
- Everything except the hardware checklist runs in CI on x86 Linux with no Pi, no SPI, no GPIO, and no network.
- Python `>=3.11`. Every public function annotated. `uv run ruff check --fix . && uv run ruff format .` before every commit, then `uv run ruff check .` and `uv run ruff format --check .` must pass.
- **Rendering and polling degrade, never crash.** No schema-valid config, no upstream response and no hardware failure may take the process down. A failure is scoped to one integration or one screen; siblings keep running.
- `ors-daemon` may import `ors-render` and `ors-schema`. Neither of those may ever import `ors-daemon`.
- Rotation and h-flip are applied by the screen worker **before** `show()`, never inside a backend.
- Config keys use the exact names in the spec's §5 YAML. The example config at `daemon/examples/rack.yaml` is validated by CI and is the config the author's rack runs.

---

### Task 1: Daemon package scaffolding

**Files:**
- Create: `daemon/pyproject.toml`
- Create: `daemon/src/ors_daemon/__init__.py`
- Create: `daemon/src/ors_daemon/logging.py`
- Create: `daemon/tests/__init__.py`
- Modify: `pyproject.toml` (workspace members, dependencies, testpaths)
- Test: `daemon/tests/test_logging.py`

**Interfaces:**
- Consumes: nothing
- Produces: importable `ors_daemon` with `__version__: str`; `setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None`; `uv run pytest` collects `daemon/tests`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_logging.py`:

```python
import io
import json
import logging

from ors_daemon.logging import setup_logging


def test_package_importable():
    import ors_daemon

    assert isinstance(ors_daemon.__version__, str)


def test_records_are_json_lines_with_the_fields_journald_needs():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logging.getLogger("ors_daemon.test").info("panel online", extra={"screen": "CPU"})

    line = json.loads(stream.getvalue().strip())
    assert line["message"] == "panel online"
    assert line["level"] == "INFO"
    assert line["logger"] == "ors_daemon.test"
    assert line["screen"] == "CPU"
    assert "time" in line


def test_debug_is_suppressed_at_info_level():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logging.getLogger("ors_daemon.test").debug("noisy")

    assert stream.getvalue() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon'`

- [ ] **Step 3: Write minimal implementation**

`daemon/pyproject.toml`:

```toml
[project]
name = "ors-daemon"
version = "0.1.0"
description = "OpenRackScreen daemon: drives rack panels from a local config"
requires-python = ">=3.11"
dependencies = [
    "ors-schema",
    "ors-render",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "requests>=2.32",
]

[project.scripts]
ors-daemon = "ors_daemon.__main__:main"

[project.optional-dependencies]
hardware = ["luma.lcd>=2.11", "numpy>=1.26"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ors_daemon"]

[tool.uv.sources]
ors-schema = { workspace = true }
ors-render = { workspace = true }
```

`daemon/src/ors_daemon/__init__.py`:

```python
__version__ = "0.1.0"
```

`daemon/src/ors_daemon/logging.py`:

```python
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TextIO

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so journald and the log shipper agree."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Install the JSON handler on the ors_daemon logger, replacing any prior one."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("ors_daemon")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
```

In the root `pyproject.toml`: add `"daemon"` to `[tool.uv.workspace] members`, add `"ors-daemon"` to `[project] dependencies`, add `ors-daemon = { workspace = true }` under `[tool.uv.sources]`, and add `"daemon/tests"` to `[tool.pytest.ini_options] testpaths`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv sync --all-packages && uv run pytest daemon/tests -v`
Expected: PASS — 3 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon pyproject.toml uv.lock
git commit -m "chore(daemon): package scaffolding and structured logging"
```

---

### Task 2: Daemon config models

**Files:**
- Create: `packages/ors-schema/src/ors_schema/daemon.py`
- Modify: `packages/ors-schema/src/ors_schema/__init__.py`
- Test: `packages/ors-schema/tests/test_daemon_config.py`

**Interfaces:**
- Consumes: `ors_schema.scene.Template`
- Produces:
  - `NightWindow(enabled: bool, start: str, end: str)` — `start`/`end` are `HH:MM`
  - `TunnelConfig(kubeconfig, namespace, service, remote_port, local_port)`
  - `FieldSpec(query: str, reduce: Literal["scalar","top"], label: str, strip: Literal["none","last_octet"])`
  - `PrometheusConfig(type, name, poll_interval, url, timeout, tunnel, fields)`
  - `IntegrationConfig` — discriminated union on `type`
  - `DisplayConfig(backend, spi_bus, spi_cs, dc, rst, hz, out_dir)`
  - `ScreenConfig(name, position, display, rotation, hflip, enabled, template, params, sleep_override)`
  - `DaemonConfig(version, timezone, night, integrations, screens, templates)`

- [ ] **Step 1: Write the failing test**

`packages/ors-schema/tests/test_daemon_config.py`:

```python
import pytest
from pydantic import ValidationError

from ors_schema.daemon import (
    DaemonConfig,
    DisplayConfig,
    FieldSpec,
    NightWindow,
    PrometheusConfig,
    ScreenConfig,
)

MINIMAL = {
    "version": 1,
    "timezone": "Europe/Amsterdam",
    "integrations": [
        {
            "name": "prom",
            "type": "prometheus",
            "url": "http://localhost:19090",
            "fields": {"cpu": {"query": "up"}},
        }
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/panels"},
            "template": "ring-gauge",
            "params": {"title": "CPU"},
        }
    ],
}


def test_minimal_config_parses_with_documented_defaults():
    config = DaemonConfig.model_validate(MINIMAL)
    assert config.night.enabled is True
    assert (config.night.start, config.night.end) == ("23:00", "07:00")

    integration = config.integrations[0]
    assert isinstance(integration, PrometheusConfig)
    assert integration.poll_interval == 5.0
    assert integration.timeout == 4.0
    assert integration.tunnel is None
    assert integration.fields["cpu"].reduce == "scalar"

    screen = config.screens[0]
    assert screen.rotation == 0
    assert screen.hflip is False
    assert screen.enabled is True
    assert screen.sleep_override is None


def test_field_spec_carries_a_top_reduction():
    spec = FieldSpec.model_validate(
        {"query": "x", "reduce": "top", "label": "instance", "strip": "last_octet"}
    )
    assert (spec.reduce, spec.label, spec.strip) == ("top", "instance", "last_octet")


def test_tunnel_parses_and_defaults_service_to_auto():
    config = DaemonConfig.model_validate(
        {
            **MINIMAL,
            "integrations": [
                {
                    **MINIMAL["integrations"][0],
                    "tunnel": {
                        "kubeconfig": "~/k8s-monitor.yaml",
                        "namespace": "monitoring",
                        "remote_port": 9090,
                        "local_port": 19090,
                    },
                }
            ],
        }
    )
    tunnel = config.integrations[0].tunnel
    assert tunnel is not None
    assert tunnel.service == "auto"


@pytest.mark.parametrize("bad", ["24:00", "7:00", "0700", "", "23:60", "midnight"])
def test_night_window_rejects_a_time_that_is_not_hh_mm(bad):
    with pytest.raises(ValidationError):
        NightWindow(start=bad)


def test_night_window_accepts_the_boundaries_of_the_day():
    assert NightWindow(start="00:00", end="23:59").start == "00:00"


@pytest.mark.parametrize("rotation", [45, 360, -90, 1])
def test_screen_rejects_a_rotation_the_worker_cannot_apply(rotation):
    with pytest.raises(ValidationError):
        ScreenConfig.model_validate({**MINIMAL["screens"][0], "rotation": rotation})


def test_unknown_integration_type_is_rejected():
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate(
            {**MINIMAL, "integrations": [{"name": "x", "type": "influxdb", "url": "u"}]}
        )


def test_unknown_key_is_rejected_rather_than_ignored():
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate({**MINIMAL, "tiemzone": "UTC"})


def test_virtual_display_requires_an_out_dir():
    with pytest.raises(ValidationError):
        DisplayConfig.model_validate({"backend": "virtual"})


def test_config_round_trips_through_json():
    config = DaemonConfig.model_validate(MINIMAL)
    assert DaemonConfig.model_validate(config.model_dump(exclude_none=True)) == config
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-schema/tests/test_daemon_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_schema.daemon'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-schema/src/ors_schema/daemon.py`:

```python
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ors_schema.scene import Template

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class NightWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    start: HHMM = "23:00"
    end: HHMM = "07:00"


class TunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kubeconfig: str
    namespace: str
    service: str = "auto"
    remote_port: int = Field(ge=1, le=65535)
    local_port: int = Field(ge=1, le=65535)


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    reduce: Literal["scalar", "top"] = "scalar"
    label: str = "instance"
    strip: Literal["none", "last_octet"] = "none"


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["prometheus"] = "prometheus"
    name: str
    poll_interval: float = Field(default=5.0, gt=0)
    url: str
    timeout: float = Field(default=4.0, gt=0)
    tunnel: TunnelConfig | None = None
    fields: dict[str, FieldSpec] = Field(min_length=1)


IntegrationConfig = Annotated[Union[PrometheusConfig], Field(discriminator="type")]


class DisplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["gc9a01", "virtual"]
    spi_bus: int = 0
    spi_cs: int = 0
    dc: int = 0
    rst: int = 0
    hz: int = 40_000_000
    out_dir: str | None = None

    @model_validator(mode="after")
    def _virtual_needs_a_directory(self) -> DisplayConfig:
        if self.backend == "virtual" and not self.out_dir:
            raise ValueError("a virtual display needs out_dir")
        return self


class ScreenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    position: int
    display: DisplayConfig
    rotation: Literal[0, 90, 180, 270] = 0
    hflip: bool = False
    enabled: bool = True
    template: str
    params: dict[str, Any] = Field(default_factory=dict)
    sleep_override: NightWindow | None = None


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    timezone: str = "UTC"
    night: NightWindow = Field(default_factory=NightWindow)
    integrations: list[IntegrationConfig] = Field(default_factory=list)
    screens: list[ScreenConfig] = Field(default_factory=list)
    templates: dict[str, Template] = Field(default_factory=dict)
```

Extend `packages/ors-schema/src/ors_schema/__init__.py` with the new names in its imports and `__all__`: `DaemonConfig`, `DisplayConfig`, `FieldSpec`, `IntegrationConfig`, `NightWindow`, `PrometheusConfig`, `ScreenConfig`, `TunnelConfig`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-schema/tests -v`
Expected: PASS — all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-schema
git commit -m "feat(schema): daemon config models"
```

---

### Task 3: Injectable clock and night-window arithmetic

**Files:**
- Create: `daemon/src/ors_daemon/clock.py`
- Test: `daemon/tests/test_clock.py`

**Interfaces:**
- Consumes: `ors_schema.daemon.NightWindow`
- Produces:
  - `Clock = Callable[[], datetime]` — always returns a timezone-aware datetime
  - `system_clock(timezone: str) -> Clock`
  - `FakeClock(start: datetime)` with `.advance(seconds: float) -> None` and `__call__() -> datetime`
  - `in_window(now: datetime, window: NightWindow) -> bool`
  - `seconds_until_boundary(now: datetime, window: NightWindow) -> float`
  - `ClockError(Exception)` — raised by `system_clock` for an unknown timezone

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_clock.py`:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from ors_schema.daemon import NightWindow

from ors_daemon.clock import ClockError, FakeClock, in_window, seconds_until_boundary, system_clock

AMS = ZoneInfo("Europe/Amsterdam")
WRAPS = NightWindow(start="23:00", end="07:00")
SAME_DAY = NightWindow(start="01:00", end="06:00")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, tzinfo=AMS)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(23, 0), True),
        (at(23, 30), True),
        (at(2, 0), True),
        (at(6, 59), True),
        (at(7, 0), False),
        (at(12, 0), False),
        (at(22, 59), False),
    ],
)
def test_a_window_that_wraps_midnight(now, expected):
    assert in_window(now, WRAPS) is expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [(at(0, 59), False), (at(1, 0), True), (at(5, 59), True), (at(6, 0), False)],
)
def test_a_window_inside_one_day(now, expected):
    assert in_window(now, SAME_DAY) is expected


def test_a_disabled_window_is_never_night():
    assert in_window(at(2, 0), NightWindow(enabled=False)) is False


def test_a_zero_length_window_is_never_night():
    assert in_window(at(3, 0), NightWindow(start="03:00", end="03:00")) is False


@pytest.mark.parametrize(
    ("now", "expected_seconds"),
    [
        (at(22, 0), 3600),
        (at(23, 30), 27000),
        (at(6, 59), 60),
        (at(7, 0), 57600),
    ],
)
def test_seconds_until_the_next_boundary(now, expected_seconds):
    assert seconds_until_boundary(now, WRAPS) == pytest.approx(expected_seconds)


def test_a_disabled_window_has_no_boundary():
    assert seconds_until_boundary(at(2, 0), NightWindow(enabled=False)) == float("inf")


def test_fake_clock_advances_only_when_told():
    clock = FakeClock(at(12, 0))
    assert clock() == at(12, 0)
    clock.advance(90)
    assert clock() == at(12, 0) + timedelta(seconds=90)


def test_system_clock_is_timezone_aware_and_rejects_a_bad_zone():
    assert system_clock("Europe/Amsterdam")().tzinfo is not None
    with pytest.raises(ClockError):
        system_clock("Mars/Olympus_Mons")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.clock'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/clock.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ors_schema.daemon import NightWindow

Clock = Callable[[], datetime]
"""Returns the current time, always timezone-aware. Injected so no test sleeps."""


class ClockError(Exception):
    """Raised for a timezone the host cannot resolve."""


def system_clock(timezone: str) -> Clock:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ClockError(f"unknown timezone {timezone!r}: {exc}") from exc
    return lambda: datetime.now(zone)


class FakeClock:
    """A clock that moves only when a test moves it."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def in_window(now: datetime, window: NightWindow) -> bool:
    """True when `now` falls inside the window. A start after the end wraps midnight."""
    if not window.enabled:
        return False
    start, end = _parse(window.start), _parse(window.end)
    if start == end:
        return False
    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_boundary(now: datetime, window: NightWindow) -> float:
    """Seconds until the window is next entered or left. `inf` when disabled."""
    if not window.enabled:
        return float("inf")
    start, end = _parse(window.start), _parse(window.end)
    if start == end:
        return float("inf")
    target = end if in_window(now, window) else start
    candidate = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return (candidate - now).total_seconds()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_clock.py -v`
Expected: PASS — all pass.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): injectable clock and night-window arithmetic"
```

---

### Task 4: Snapshot store

**Files:**
- Create: `daemon/src/ors_daemon/snapshot.py`
- Test: `daemon/tests/test_snapshot.py`

**Interfaces:**
- Consumes: `ors_daemon.clock.Clock`
- Produces:
  - `Health` enum: `CONNECTING`, `HEALTHY`, `UNHEALTHY`
  - `IntegrationHealth(state, reason, consecutive_failures, last_success, latency_ms, stale)` — frozen dataclass
  - `Snapshot(data: Mapping[str, Any], version: int, health: Mapping[str, IntegrationHealth])` — frozen dataclass
  - `SnapshotStore(stale_after: int = 3)` with `register(name)`, `put(name, fields, latency_ms, now)`, `fail(name, reason, now)`, `read() -> Snapshot`, `wait_for_change(version: int, timeout: float) -> bool`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_snapshot.py`:

```python
import threading
from datetime import datetime, timezone

from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def test_a_registered_integration_starts_connecting_with_no_data():
    store = SnapshotStore()
    store.register("prom")

    snap = store.read()
    assert snap.version == 0
    assert snap.data == {}
    assert snap.health["prom"].state is Health.CONNECTING
    assert snap.health["prom"].stale is False


def test_a_successful_poll_publishes_data_and_bumps_the_version():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=12.5, now=NOW)

    snap = store.read()
    assert snap.data["prom"] == {"cpu": 42.0}
    assert snap.version == 1
    assert snap.health["prom"].state is Health.HEALTHY
    assert snap.health["prom"].latency_ms == 12.5
    assert snap.health["prom"].last_success == NOW


def test_read_returns_a_copy_a_caller_cannot_use_to_mutate_the_store():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)

    snap = store.read()
    snap.data["prom"]["cpu"] = 99.0
    assert store.read().data["prom"]["cpu"] == 42.0


def test_failures_accumulate_and_mark_stale_only_at_the_threshold():
    store = SnapshotStore(stale_after=3)
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)

    for expected_stale in (False, False, True):
        store.fail("prom", "timeout", now=NOW)
        health = store.read().health["prom"]
        assert health.state is Health.UNHEALTHY
        assert health.reason == "timeout"
        assert health.stale is expected_stale


def test_a_failure_leaves_the_last_good_data_in_place():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    version = store.read().version
    store.fail("prom", "timeout", now=NOW)

    snap = store.read()
    assert snap.data["prom"] == {"cpu": 42.0}
    assert snap.version == version, "a failure must not look like new data"


def test_recovery_clears_staleness_and_the_failure_count():
    store = SnapshotStore(stale_after=2)
    store.register("prom")
    store.fail("prom", "timeout", now=NOW)
    store.fail("prom", "timeout", now=NOW)
    assert store.read().health["prom"].stale is True

    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    health = store.read().health["prom"]
    assert health.state is Health.HEALTHY
    assert health.stale is False
    assert health.consecutive_failures == 0


def test_wait_for_change_returns_immediately_when_already_behind():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)

    assert store.wait_for_change(version=0, timeout=0.0) is True


def test_wait_for_change_wakes_on_a_publish_from_another_thread():
    store = SnapshotStore()
    store.register("prom")
    woke = threading.Event()

    def waiter() -> None:
        if store.wait_for_change(version=0, timeout=5.0):
            woke.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    store.put("prom", {"cpu": 1.0}, latency_ms=1.0, now=NOW)
    thread.join(timeout=5.0)

    assert woke.is_set()


def test_wait_for_change_times_out_when_nothing_happens():
    store = SnapshotStore()
    assert store.wait_for_change(version=0, timeout=0.01) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.snapshot'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/snapshot.py`:

```python
from __future__ import annotations

import copy
import enum
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


class Health(enum.Enum):
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class IntegrationHealth:
    state: Health = Health.CONNECTING
    reason: str | None = None
    consecutive_failures: int = 0
    last_success: datetime | None = None
    latency_ms: float | None = None
    stale: bool = False


@dataclass(frozen=True)
class Snapshot:
    data: dict[str, Any]
    version: int
    health: Mapping[str, IntegrationHealth]


class SnapshotStore:
    """The one structure daemon threads share.

    Pollers write, screen workers read and wait. `version` bumps only on new
    *data*, so a failing poll never looks like a reason to redraw.
    """

    def __init__(self, stale_after: int = 3) -> None:
        self._condition = threading.Condition()
        self._data: dict[str, Any] = {}
        self._health: dict[str, IntegrationHealth] = {}
        self._version = 0
        self._stale_after = stale_after

    def register(self, name: str) -> None:
        with self._condition:
            self._health.setdefault(name, IntegrationHealth())

    def put(self, name: str, fields: dict[str, Any], latency_ms: float, now: datetime) -> None:
        with self._condition:
            self._data[name] = copy.deepcopy(fields)
            self._version += 1
            self._health[name] = IntegrationHealth(
                state=Health.HEALTHY,
                reason=None,
                consecutive_failures=0,
                last_success=now,
                latency_ms=latency_ms,
                stale=False,
            )
            self._condition.notify_all()

    def fail(self, name: str, reason: str, now: datetime) -> None:
        with self._condition:
            previous = self._health.get(name, IntegrationHealth())
            failures = previous.consecutive_failures + 1
            self._health[name] = replace(
                previous,
                state=Health.UNHEALTHY,
                reason=reason,
                consecutive_failures=failures,
                stale=failures >= self._stale_after,
            )
            self._condition.notify_all()

    def read(self) -> Snapshot:
        with self._condition:
            return Snapshot(
                data=copy.deepcopy(self._data),
                version=self._version,
                health=dict(self._health),
            )

    def wait_for_change(self, version: int, timeout: float) -> bool:
        """Block until the version moves past `version`. True if it did."""
        with self._condition:
            if self._version != version:
                return True
            self._condition.wait(timeout=timeout)
            return self._version != version
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_snapshot.py -v`
Expected: PASS — 9 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): snapshot store with version counter and health"
```

---

### Task 5: Integration contract and the Prometheus client

**Files:**
- Create: `daemon/src/ors_daemon/integrations/__init__.py`
- Create: `daemon/src/ors_daemon/integrations/prometheus.py`
- Test: `daemon/tests/test_prometheus.py`

**Interfaces:**
- Consumes: `ors_schema.daemon.PrometheusConfig`, `FieldSpec`
- Produces:
  - `Integration` protocol: `name: str`, `open() -> None`, `poll() -> dict[str, Any]`, `close() -> None`
  - `IntegrationError(Exception)` — what `poll` raises on any failure
  - `UrlProvider = Callable[[], str]`
  - `build_integration(config: IntegrationConfig, url_provider: UrlProvider | None = None) -> Integration`
  - `PrometheusIntegration(config, url_provider=None, session=None)`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_prometheus.py`:

```python
import json

import pytest
from ors_schema.daemon import PrometheusConfig

from ors_daemon.integrations import IntegrationError, build_integration
from ors_daemon.integrations.prometheus import PrometheusIntegration


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records queries and replies from a scripted map, or raises."""

    def __init__(self, replies=None, error=None):
        self.replies = replies or {}
        self.error = error
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params["query"], timeout))
        if self.error is not None:
            raise self.error
        return self.replies[params["query"]]


def scalar(value):
    return FakeResponse(
        {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, value]}]}}
    )


def vector(*pairs):
    return FakeResponse(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"instance": instance}, "value": [0, value]} for instance, value in pairs
                ],
            },
        }
    )


def config(**fields):
    return PrometheusConfig(name="prom", url="http://prom:9090", fields=fields)


def test_a_scalar_field_becomes_a_float():
    session = FakeSession({"up": scalar("42.4")})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    assert integration.poll() == {"cpu": 42.4}
    assert session.calls[0][0] == "http://prom:9090/api/v1/query"
    assert session.calls[0][2] == 4.0


def test_an_empty_result_yields_none_rather_than_raising():
    session = FakeSession({"up": FakeResponse({"status": "success", "data": {"result": []}})})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    assert integration.poll() == {"cpu": None}


def test_nan_is_dropped_because_prometheus_really_emits_it():
    session = FakeSession({"up": scalar("NaN")})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    assert integration.poll() == {"cpu": None}


def test_a_top_reduction_returns_the_highest_series_with_its_label():
    session = FakeSession({"q": vector(("192.168.1.5:9100", "71.2"), ("192.168.1.7:9100", "12.0"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top", "label": "instance"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "192.168.1.5:9100", "value": 71.2}}


def test_strip_last_octet_shortens_the_label_to_what_a_240px_panel_can_show():
    session = FakeSession({"q": vector(("192.168.1.5:9100", "71.2"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top", "label": "instance", "strip": "last_octet"}),
        session=session,
    )

    assert integration.poll() == {"hot": {"node": ".5", "value": 71.2}}


def test_a_top_reduction_over_nothing_yields_none():
    session = FakeSession({"q": FakeResponse({"status": "success", "data": {"result": []}})})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top"}), session=session
    )

    assert integration.poll() == {"hot": None}


def test_one_bad_field_does_not_discard_the_others():
    session = FakeSession({"good": scalar("1.0"), "bad": FakeResponse(None, text="<html>502</html>")})
    integration = PrometheusIntegration(
        config(good={"query": "good"}, bad={"query": "bad"}), session=session
    )

    assert integration.poll() == {"good": 1.0, "bad": None}


def test_a_transport_failure_raises_integration_error():
    session = FakeSession(error=OSError("connection refused"))
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    with pytest.raises(IntegrationError, match="connection refused"):
        integration.poll()


def test_a_5xx_on_every_field_raises_rather_than_publishing_all_nones():
    session = FakeSession({"up": FakeResponse({"status": "error"}, status_code=503)})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    with pytest.raises(IntegrationError):
        integration.poll()


def test_the_url_provider_wins_over_the_configured_url():
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(
        config(cpu={"query": "up"}), url_provider=lambda: "http://localhost:19090", session=session
    )
    integration.poll()

    assert session.calls[0][0] == "http://localhost:19090/api/v1/query"


def test_build_integration_dispatches_on_type():
    assert isinstance(build_integration(config(cpu={"query": "up"})), PrometheusIntegration)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_prometheus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.integrations'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/integrations/__init__.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from ors_schema.daemon import IntegrationConfig

UrlProvider = Callable[[], str]
"""Supplies the base URL at poll time, so a tunnel can move underneath."""


class IntegrationError(Exception):
    """A poll failed. The poller owns what happens next."""


@runtime_checkable
class Integration(Protocol):
    """A pure fetcher.

    It owns no interval, no retry policy, no health state and no threading --
    the poller owns all of that. Raising `IntegrationError` is how a failure is
    reported, and is the only failure channel.
    """

    name: str

    def open(self) -> None: ...

    def poll(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


def build_integration(
    config: IntegrationConfig, url_provider: UrlProvider | None = None
) -> Integration:
    from ors_daemon.integrations.prometheus import PrometheusIntegration

    builders = {"prometheus": PrometheusIntegration}
    builder = builders.get(config.type)
    if builder is None:  # pragma: no cover - the schema's discriminator rejects this first
        raise IntegrationError(f"no client for integration type {config.type!r}")
    return builder(config, url_provider=url_provider)
```

`daemon/src/ors_daemon/integrations/prometheus.py`:

```python
from __future__ import annotations

import logging
import math
from typing import Any

import requests
from ors_schema.daemon import FieldSpec, PrometheusConfig

from ors_daemon.integrations import IntegrationError, UrlProvider

log = logging.getLogger(__name__)


class PrometheusIntegration:
    """Reads a configured set of PromQL queries into one namespace."""

    def __init__(
        self,
        config: PrometheusConfig,
        url_provider: UrlProvider | None = None,
        session: Any | None = None,
    ) -> None:
        self.name = config.name
        self._config = config
        self._url_provider = url_provider
        self._session = session

    def open(self) -> None:
        if self._session is None:
            self._session = requests.Session()

    def close(self) -> None:
        if isinstance(self._session, requests.Session):
            self._session.close()
        self._session = None

    def poll(self) -> dict[str, Any]:
        self.open()
        base = (self._url_provider() if self._url_provider else self._config.url).rstrip("/")
        url = f"{base}/api/v1/query"

        fields: dict[str, Any] = {}
        failures = 0
        for name, spec in self._config.fields.items():
            try:
                fields[name] = self._one(url, spec)
            except IntegrationError:
                raise
            except Exception as exc:  # a malformed field is not a dead source
                log.warning("field failed", extra={"integration": self.name, "field": name, "error": str(exc)})
                fields[name] = None
                failures += 1

        if failures == len(self._config.fields):
            raise IntegrationError(f"every field failed against {base}")
        return fields

    def _one(self, url: str, spec: FieldSpec) -> Any:
        try:
            response = self._session.get(  # type: ignore[union-attr]
                url, params={"query": spec.query}, timeout=self._config.timeout
            )
        except Exception as exc:
            raise IntegrationError(str(exc)) from exc

        if response.status_code >= 500:
            raise IntegrationError(f"HTTP {response.status_code} from {url}")

        results = response.json().get("data", {}).get("result", [])
        if not results:
            return None
        if spec.reduce == "top":
            return self._top(results, spec)
        return _number(results[0]["value"][1])

    @staticmethod
    def _top(results: list[dict[str, Any]], spec: FieldSpec) -> dict[str, Any] | None:
        best_label, best_value = None, None
        for item in results:
            value = _number(item.get("value", [None, None])[1])
            if value is None:
                continue
            if best_value is None or value > best_value:
                best_label = item.get("metric", {}).get(spec.label, "")
                best_value = value
        if best_value is None:
            return None
        return {"node": _strip(best_label or "", spec.strip), "value": best_value}


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(value) else value


def _strip(label: str, mode: str) -> str:
    if mode != "last_octet":
        return label
    host = label.split(":")[0]
    parts = host.split(".")
    return f".{parts[-1]}" if len(parts) > 1 else host
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_prometheus.py -v`
Expected: PASS — 11 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): integration contract and Prometheus client"
```

---

### Task 6: Poller thread

**Files:**
- Create: `daemon/src/ors_daemon/poller.py`
- Test: `daemon/tests/test_poller.py`

**Interfaces:**
- Consumes: `Integration`, `IntegrationError`, `SnapshotStore`, `Clock`
- Produces:
  - `Poller(threading.Thread)` constructed with `(integration, store, interval, stop, clock, backoff_cap=60.0, sleeper=None)`
  - `.heartbeat: float` — `time.monotonic()` of the last loop iteration
  - `.poll_once() -> None` — one cycle, synchronous, the unit the tests drive
  - `.next_delay: float` — seconds the loop will wait before the next cycle

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_poller.py`:

```python
import threading
from datetime import datetime, timezone

from ors_daemon.integrations import IntegrationError
from ors_daemon.poller import Poller
from ors_daemon.snapshot import Health, SnapshotStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class FakeIntegration:
    def __init__(self, name="prom", results=None):
        self.name = name
        self.results = list(results or [])
        self.opened = 0
        self.closed = 0
        self.polls = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def poll(self):
        self.polls += 1
        outcome = self.results.pop(0) if self.results else {"cpu": 1.0}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(integration, **kwargs):
    store = SnapshotStore(stale_after=kwargs.pop("stale_after", 3))
    store.register(integration.name)
    poller = Poller(
        integration=integration,
        store=store,
        interval=kwargs.pop("interval", 5.0),
        stop=threading.Event(),
        clock=lambda: NOW,
        **kwargs,
    )
    return poller, store


def test_a_successful_cycle_publishes_and_stays_on_the_configured_interval():
    poller, store = make(FakeIntegration(results=[{"cpu": 42.0}]))
    poller.poll_once()

    assert store.read().data["prom"] == {"cpu": 42.0}
    assert store.read().health["prom"].state is Health.HEALTHY
    assert poller.next_delay == 5.0


def test_a_failed_cycle_records_the_reason_without_publishing():
    poller, store = make(FakeIntegration(results=[IntegrationError("timeout")]))
    poller.poll_once()

    snap = store.read()
    assert snap.data == {}
    assert snap.health["prom"].state is Health.UNHEALTHY
    assert snap.health["prom"].reason == "timeout"


def test_an_unexpected_exception_is_treated_as_a_failure_not_a_crash():
    poller, store = make(FakeIntegration(results=[RuntimeError("boom")]))
    poller.poll_once()

    assert store.read().health["prom"].state is Health.UNHEALTHY


def test_backoff_doubles_on_repeated_failure_and_is_capped():
    poller, _ = make(
        FakeIntegration(results=[IntegrationError("x")] * 6), interval=5.0, backoff_cap=30.0
    )
    delays = []
    for _ in range(6):
        poller.poll_once()
        delays.append(poller.next_delay)

    assert delays == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]


def test_a_success_resets_the_backoff():
    poller, _ = make(
        FakeIntegration(results=[IntegrationError("x"), IntegrationError("x"), {"cpu": 1.0}])
    )
    poller.poll_once()
    poller.poll_once()
    poller.poll_once()

    assert poller.next_delay == 5.0


def test_staleness_arrives_at_the_configured_threshold():
    poller, store = make(FakeIntegration(results=[IntegrationError("x")] * 3), stale_after=3)
    for _ in range(2):
        poller.poll_once()
    assert store.read().health["prom"].stale is False

    poller.poll_once()
    assert store.read().health["prom"].stale is True


def test_the_run_loop_polls_until_stopped_and_closes_the_integration():
    integration = FakeIntegration()
    store = SnapshotStore()
    store.register("prom")
    stop = threading.Event()
    polled_twice = threading.Event()

    def sleeper(seconds: float) -> None:
        if integration.polls >= 2:
            polled_twice.set()
            stop.set()

    poller = Poller(
        integration=integration,
        store=store,
        interval=0.0,
        stop=stop,
        clock=lambda: NOW,
        sleeper=sleeper,
    )
    poller.start()
    polled_twice.wait(timeout=5.0)
    poller.join(timeout=5.0)

    assert integration.polls >= 2
    assert integration.closed == 1
    assert poller.heartbeat > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_poller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.poller'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/poller.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ors_daemon.clock import Clock
from ors_daemon.integrations import Integration, IntegrationError
from ors_daemon.snapshot import SnapshotStore

log = logging.getLogger(__name__)


class Poller(threading.Thread):
    """Owns everything an integration deliberately does not: interval, backoff, health."""

    def __init__(
        self,
        integration: Integration,
        store: SnapshotStore,
        interval: float,
        stop: threading.Event,
        clock: Clock,
        backoff_cap: float = 60.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(name=f"poller-{integration.name}", daemon=True)
        self._integration = integration
        self._store = store
        self._interval = interval
        self._stop = stop
        self._clock = clock
        self._backoff_cap = backoff_cap
        self._sleeper = sleeper or (lambda seconds: stop.wait(seconds))
        self.next_delay = interval
        self.heartbeat = 0.0

    def poll_once(self) -> None:
        self.heartbeat = time.monotonic()
        started = time.monotonic()
        try:
            fields = self._integration.poll()
        except IntegrationError as exc:
            self._failed(str(exc))
        except Exception as exc:  # an integration bug must not take the daemon down
            log.exception("integration raised", extra={"integration": self._integration.name})
            self._failed(f"{type(exc).__name__}: {exc}")
        else:
            latency_ms = (time.monotonic() - started) * 1000.0
            self._store.put(self._integration.name, fields, latency_ms, self._clock())
            self.next_delay = self._interval

    def run(self) -> None:
        try:
            self._integration.open()
            while not self._stop.is_set():
                self.poll_once()
                self._sleeper(self.next_delay)
        finally:
            self._integration.close()

    def _failed(self, reason: str) -> None:
        self._store.fail(self._integration.name, reason, self._clock())
        doubled = max(self._interval, self.next_delay) * 2 if self.next_delay > 0 else self._interval
        self.next_delay = min(self._backoff_cap, doubled if self.next_delay != self._interval else self._interval * 2)
        log.warning(
            "poll failed",
            extra={"integration": self._integration.name, "reason": reason, "retry_in": self.next_delay},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_poller.py -v`
Expected: PASS — 7 passed.

If `test_backoff_doubles_on_repeated_failure_and_is_capped` fails, the `_failed` delay arithmetic is wrong — the sequence must be interval, then doubling from interval, capped. Simplify it until the expected list matches; do not change the test.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): poller thread with backoff and health tracking"
```

---

### Task 7: kubectl port-forward tunnel

**Files:**
- Create: `daemon/src/ors_daemon/tunnel.py`
- Test: `daemon/tests/test_tunnel.py`

**Interfaces:**
- Consumes: `ors_schema.daemon.TunnelConfig`
- Produces:
  - `Tunnel(threading.Thread)` constructed with `(config, stop, probe, launcher=None, discoverer=None, sleeper=None)`
  - `.ready: threading.Event`
  - `.base_url: str` — `http://localhost:<local_port>`
  - `.tick() -> None` — one supervision cycle, the unit the tests drive
  - `Launcher = Callable[[list[str]], Popen-like]`, `Discoverer = Callable[[TunnelConfig], str | None]`
  - `default_launcher(argv)` and `default_discoverer(config)` shell out to `kubectl`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_tunnel.py`:

```python
import threading

from ors_schema.daemon import TunnelConfig

from ors_daemon.tunnel import Tunnel

CONFIG = TunnelConfig(
    kubeconfig="/tmp/kubeconfig", namespace="monitoring", remote_port=9090, local_port=19090
)


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode

    def die(self):
        self._returncode = 1


class Harness:
    def __init__(self, probe_results, service="prometheus"):
        self.processes = []
        self.argvs = []
        self.probe_results = list(probe_results)
        self.service = service
        self.discoveries = 0

    def launcher(self, argv):
        self.argvs.append(argv)
        process = FakeProcess()
        self.processes.append(process)
        return process

    def discoverer(self, config):
        self.discoveries += 1
        return self.service

    def probe(self, url):
        return self.probe_results.pop(0) if self.probe_results else True


def make(harness):
    return Tunnel(
        config=CONFIG,
        stop=threading.Event(),
        probe=harness.probe,
        launcher=harness.launcher,
        discoverer=harness.discoverer,
        sleeper=lambda seconds: None,
    )


def test_base_url_points_at_the_local_port():
    assert make(Harness([True])).base_url == "http://localhost:19090"


def test_the_first_tick_launches_kubectl_with_the_configured_arguments():
    harness = Harness([True])
    tunnel = make(harness)
    tunnel.tick()

    argv = harness.argvs[0]
    assert argv[0] == "kubectl"
    assert "--kubeconfig" in argv and "/tmp/kubeconfig" in argv
    assert "-n" in argv and "monitoring" in argv
    assert "svc/prometheus" in argv
    assert "19090:9090" in argv


def test_ready_is_set_only_once_a_probe_succeeds():
    harness = Harness([False, True])
    tunnel = make(harness)

    tunnel.tick()
    assert tunnel.ready.is_set() is False
    tunnel.tick()
    tunnel.tick()
    assert tunnel.ready.is_set() is True


def test_two_failed_probes_tear_the_tunnel_down_and_relaunch_it():
    harness = Harness([True, False, False, True])
    tunnel = make(harness)
    for _ in range(5):
        tunnel.tick()

    assert harness.processes[0].terminated is True
    assert len(harness.processes) >= 2, "a dead tunnel must be relaunched, not left alive"


def test_a_dead_process_clears_ready_and_relaunches():
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    tunnel.tick()
    assert tunnel.ready.is_set() is True

    harness.processes[0].die()
    tunnel.tick()
    assert tunnel.ready.is_set() is False
    assert len(harness.processes) == 2


def test_a_service_name_is_discovered_once_and_then_reused():
    harness = Harness([True] * 6)
    tunnel = make(harness)
    for _ in range(3):
        tunnel.tick()

    assert harness.discoveries == 1


def test_an_explicit_service_name_skips_discovery():
    harness = Harness([True, True])
    tunnel = Tunnel(
        config=CONFIG.model_copy(update={"service": "prom-server"}),
        stop=threading.Event(),
        probe=harness.probe,
        launcher=harness.launcher,
        discoverer=harness.discoverer,
        sleeper=lambda seconds: None,
    )
    tunnel.tick()

    assert harness.discoveries == 0
    assert "svc/prom-server" in harness.argvs[0]


def test_a_service_that_cannot_be_discovered_does_not_launch_or_crash():
    harness = Harness([True])
    harness.service = None
    tunnel = make(harness)
    tunnel.tick()

    assert harness.argvs == []
    assert tunnel.ready.is_set() is False


def test_stopping_terminates_the_subprocess():
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    tunnel.shutdown()

    assert harness.processes[0].terminated is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_tunnel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.tunnel'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/tunnel.py`:

```python
from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import requests
from ors_schema.daemon import TunnelConfig

log = logging.getLogger(__name__)

Launcher = Callable[[list[str]], Any]
Discoverer = Callable[[TunnelConfig], str | None]
Probe = Callable[[str], bool]

_FAILURES_BEFORE_RELAUNCH = 2


def default_launcher(argv: list[str]) -> Any:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_discoverer(config: TunnelConfig) -> str | None:
    """Find a service whose name looks like the integration it fronts."""
    try:
        output = subprocess.check_output(
            ["kubectl", "--kubeconfig", config.kubeconfig, "get", "svc", "-n", config.namespace, "-o", "name"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode()
    except Exception as exc:
        log.warning("service discovery failed", extra={"namespace": config.namespace, "error": str(exc)})
        return None
    for line in output.splitlines():
        name = line.split("/")[-1].strip()
        if "prom" in name.lower():
            return name
    return None


def default_probe(url: str) -> bool:
    try:
        return requests.get(url, timeout=3.0).status_code < 500
    except Exception:
        return False


class Tunnel(threading.Thread):
    """Keeps one `kubectl port-forward` alive and *actually working*.

    A liveness check on the subprocess is not enough: after a cluster reboot the
    process usually survives while the tunnel underneath it is dead. So every
    cycle probes the local URL, and repeated probe failures tear the process
    down -- freeing the local port -- rather than waiting for it to exit.
    """

    def __init__(
        self,
        config: TunnelConfig,
        stop: threading.Event,
        probe: Probe | None = None,
        launcher: Launcher | None = None,
        discoverer: Discoverer | None = None,
        sleeper: Callable[[float], None] | None = None,
        interval: float = 5.0,
    ) -> None:
        super().__init__(name=f"tunnel-{config.namespace}", daemon=True)
        self._config = config
        self._stop = stop
        self._probe = probe or default_probe
        self._launch = launcher or default_launcher
        self._discover = discoverer or default_discoverer
        self._sleeper = sleeper or (lambda seconds: stop.wait(seconds))
        self._interval = interval
        self._process: Any | None = None
        self._service: str | None = None if config.service == "auto" else config.service
        self._failures = 0
        self.ready = threading.Event()
        self.base_url = f"http://localhost:{config.local_port}"

    def tick(self) -> None:
        if self._process is None or self._process.poll() is not None:
            self.ready.clear()
            self._kill()
            self._start()
            return

        if self._probe(f"{self.base_url}/"):
            self._failures = 0
            self.ready.set()
            return

        self._failures += 1
        self.ready.clear()
        if self._failures >= _FAILURES_BEFORE_RELAUNCH:
            log.warning("tunnel probes failing, relaunching", extra={"namespace": self._config.namespace})
            self._kill()
            self._failures = 0

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self.tick()
                self._sleeper(self._interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.ready.clear()
        self._kill()

    def _start(self) -> None:
        if self._service is None:
            self._service = self._discover(self._config)
        if not self._service:
            log.warning("no service to forward", extra={"namespace": self._config.namespace})
            return
        argv = [
            "kubectl",
            "--kubeconfig",
            self._config.kubeconfig,
            "port-forward",
            "-n",
            self._config.namespace,
            f"svc/{self._service}",
            f"{self._config.local_port}:{self._config.remote_port}",
        ]
        log.info("starting tunnel", extra={"argv": " ".join(argv)})
        self._process = self._launch(argv)

    def _kill(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                log.warning("could not kill kubectl", extra={"namespace": self._config.namespace})
        finally:
            self._process = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_tunnel.py -v`
Expected: PASS — 9 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): kubectl port-forward tunnel with probe-driven restart"
```

---

### Task 8: Display backends

**Files:**
- Create: `daemon/src/ors_daemon/displays/__init__.py`
- Create: `daemon/src/ors_daemon/displays/virtual.py`
- Create: `daemon/src/ors_daemon/displays/gc9a01.py`
- Test: `daemon/tests/test_displays.py`

**Interfaces:**
- Consumes: `ors_schema.daemon.DisplayConfig`
- Produces:
  - `DisplayBackend` protocol: `show(image) -> None`, `sleep() -> None`, `wake() -> None`, `close() -> None`
  - `DisplayError(Exception)`
  - `build_display(config: DisplayConfig, name: str) -> DisplayBackend`
  - `VirtualDisplay(out_dir: Path, name: str)` — writes `<out_dir>/<name>.png`, exposes `.frames: int`, `.asleep: bool`
  - `GC9A01Display(spi_bus, spi_cs, dc, rst, hz, serial_factory=None)` — `.pack565(image) -> bytes` is a static method tested without hardware

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_displays.py`:

```python
import pytest
from ors_schema.daemon import DisplayConfig
from PIL import Image

from ors_daemon.displays import DisplayError, build_display
from ors_daemon.displays.gc9a01 import GC9A01Display
from ors_daemon.displays.virtual import VirtualDisplay


def panel(color=(255, 0, 0)) -> Image.Image:
    return Image.new("RGB", (240, 240), color)


def test_virtual_display_writes_one_png_per_screen(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.show(panel())

    written = tmp_path / "CPU.png"
    assert written.exists()
    assert Image.open(written).size == (240, 240)
    assert display.frames == 1


def test_virtual_display_overwrites_rather_than_accumulating(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.show(panel((255, 0, 0)))
    display.show(panel((0, 255, 0)))

    assert list(tmp_path.glob("*.png")) == [tmp_path / "CPU.png"]
    assert Image.open(tmp_path / "CPU.png").getpixel((120, 120)) == (0, 255, 0)
    assert display.frames == 2


def test_virtual_display_tracks_sleep_and_wake(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    assert display.asleep is False
    display.sleep()
    assert display.asleep is True
    display.wake()
    assert display.asleep is False


def test_virtual_display_creates_its_directory(tmp_path):
    target = tmp_path / "nested" / "panels"
    VirtualDisplay(target, "CPU").show(panel())

    assert (target / "CPU.png").exists()


def test_pack565_is_two_big_endian_bytes_per_pixel():
    packed = GC9A01Display.pack565(Image.new("RGB", (2, 1), (255, 0, 0)))

    assert len(packed) == 4
    assert packed == b"\xf8\x00\xf8\x00"


@pytest.mark.parametrize(
    ("color", "expected"),
    [((255, 255, 255), b"\xff\xff"), ((0, 0, 0), b"\x00\x00"), ((0, 255, 0), b"\x07\xe0"), ((0, 0, 255), b"\x00\x1f")],
)
def test_pack565_channel_layout(color, expected):
    assert GC9A01Display.pack565(Image.new("RGB", (1, 1), color)) == expected


def test_build_display_returns_a_virtual_backend(tmp_path):
    config = DisplayConfig(backend="virtual", out_dir=str(tmp_path))
    assert isinstance(build_display(config, "CPU"), VirtualDisplay)


def test_build_display_reports_a_missing_hardware_dependency_clearly():
    config = DisplayConfig(backend="gc9a01", spi_bus=0, spi_cs=0, dc=6, rst=5)
    try:
        import luma.lcd  # noqa: F401
    except ImportError:
        with pytest.raises(DisplayError, match="luma"):
            build_display(config, "CPU")
    else:
        pytest.skip("luma is installed; the import-error path cannot be exercised here")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_displays.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.displays'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/displays/__init__.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ors_schema.daemon import DisplayConfig
from PIL import Image


class DisplayError(Exception):
    """A backend could not be built or could not write to its panel."""


@runtime_checkable
class DisplayBackend(Protocol):
    """Pure transport: a finished panel image goes in, nothing comes back.

    Rotation and h-flip are applied by the screen worker *before* `show`, so a
    virtual backend cannot show something different from the glass.
    """

    def show(self, image: Image.Image) -> None: ...

    def sleep(self) -> None: ...

    def wake(self) -> None: ...

    def close(self) -> None: ...


def build_display(config: DisplayConfig, name: str) -> DisplayBackend:
    if config.backend == "virtual":
        from ors_daemon.displays.virtual import VirtualDisplay

        return VirtualDisplay(Path(config.out_dir or "."), name)

    try:
        from ors_daemon.displays.gc9a01 import GC9A01Display
    except ImportError as exc:  # luma is an optional extra
        raise DisplayError(
            f"the gc9a01 backend needs the hardware extra (pip install 'ors-daemon[hardware]'): {exc}"
        ) from exc

    return GC9A01Display(
        spi_bus=config.spi_bus, spi_cs=config.spi_cs, dc=config.dc, rst=config.rst, hz=config.hz
    )
```

`daemon/src/ors_daemon/displays/virtual.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image


class VirtualDisplay:
    """Writes what would have gone to glass, so the daemon runs with no hardware."""

    def __init__(self, out_dir: Path, name: str) -> None:
        self._path = Path(out_dir) / f"{name}.png"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.asleep = False

    def show(self, image: Image.Image) -> None:
        temporary = self._path.with_suffix(".png.tmp")
        image.save(temporary, format="PNG")
        os.replace(temporary, self._path)
        self.frames += 1

    def sleep(self) -> None:
        self.asleep = True

    def wake(self) -> None:
        self.asleep = False

    def close(self) -> None:
        pass
```

`daemon/src/ors_daemon/displays/gc9a01.py` — the driver, with the init sequence carried over byte-for-byte from the `k8s_monitor.py` this project replaces. **That script is not in this repository**; the sequence below is the authoritative copy, and it is known-working on this exact hardware. Do not "clean it up" — the magic numbers are the panel's, and there is no datasheet symbol for most of them.

```python
from __future__ import annotations

import math
import time
from typing import Any

from PIL import Image

from ors_daemon.displays import DisplayError

_INIT = [
    (0xEF,), (0xEB, 0x14), (0xFE,), (0xEF,), (0xEB, 0x14),
    (0x84, 0x40), (0x85, 0xFF), (0x86, 0xFF), (0x87, 0xFF),
    (0x88, 0x0A), (0x89, 0x21), (0x8A, 0x00), (0x8B, 0x80),
    (0x8C, 0x01), (0x8D, 0x01), (0x8E, 0xFF), (0x8F, 0xFF),
    (0xB6, 0x00, 0x20), (0x36, 0x08), (0x3A, 0x05),
    (0x90, 0x08, 0x08, 0x08, 0x08), (0xBD, 0x06), (0xBC, 0x00),
    (0xFF, 0x60, 0x01, 0x04), (0xC3, 0x13), (0xC4, 0x13),
    (0xC9, 0x22), (0xBE, 0x11), (0xE1, 0x10, 0x0E),
    (0xDF, 0x21, 0x0C, 0x02),
    (0xF0, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A),
    (0xF1, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F),
    (0xF2, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A),
    (0xF3, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F),
    (0xED, 0x1B, 0x0B), (0xAE, 0x77), (0xCD, 0x63),
    (0x70, 0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03),
    (0xE8, 0x34),
    (0x62, 0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70),
    (0x63, 0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70),
    (0x64, 0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07),
    (0x66, 0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00),
    (0x67, 0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98),
    (0x74, 0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00),
    (0x98, 0x3E, 0x07), (0x35,), (0x21,), (0x11,),
]
_SLEEP_OUT_MS = 0.120   # the datasheet requires >=120ms before the next command
_DISPLAY_ON_MS = 0.020


class GC9A01Display:
    """240x240 round SPI panel. Pure transport: it draws what it is handed."""

    def __init__(
        self,
        spi_bus: int,
        spi_cs: int,
        dc: int,
        rst: int,
        hz: int,
        serial_factory: Any | None = None,
    ) -> None:
        if serial_factory is None:
            from luma.core.interface.serial import spi

            serial_factory = spi
        try:
            self._serial = serial_factory(
                port=spi_bus, device=spi_cs, gpio_DC=dc, gpio_RST=rst, bus_speed_hz=hz
            )
        except Exception as exc:
            raise DisplayError(f"cannot open SPI{spi_bus}.{spi_cs}: {exc}") from exc
        self._init_panel()

    def _command(self, cmd: int, *args: int) -> None:
        try:
            self._serial.command(cmd)
            if args:
                self._serial.data(list(args))
        except Exception as exc:
            raise DisplayError(f"SPI command 0x{cmd:02X} failed: {exc}") from exc

    def _init_panel(self) -> None:
        for entry in _INIT:
            self._command(*entry)
        time.sleep(_SLEEP_OUT_MS)
        self._command(0x29)
        time.sleep(_DISPLAY_ON_MS)

    @staticmethod
    def pack565(image: Image.Image) -> bytes:
        """Big-endian RGB565, two bytes per pixel. Tested without any hardware."""
        packed = bytearray()
        for red, green, blue in image.convert("RGB").getdata():
            value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
            packed += value.to_bytes(2, "big")
        return bytes(packed)

    def show(self, image: Image.Image) -> None:
        width, height = image.size
        self._command(0x2A, 0, 0, (width - 1) >> 8, (width - 1) & 0xFF)
        self._command(0x2B, 0, 0, (height - 1) >> 8, (height - 1) & 0xFF)
        self._command(0x2C)
        try:
            self._serial.data(list(self.pack565(image)))
        except Exception as exc:
            raise DisplayError(f"SPI write failed: {exc}") from exc

    def sleep(self) -> None:
        self._command(0x28)
        time.sleep(_DISPLAY_ON_MS)
        self._command(0x10)
        time.sleep(_SLEEP_OUT_MS)

    def wake(self) -> None:
        self._command(0x11)
        time.sleep(_SLEEP_OUT_MS)
        self._command(0x29)
        time.sleep(_DISPLAY_ON_MS)

    def close(self) -> None:
        try:
            self._serial.cleanup()
        except Exception:
            pass
```

**Research note for this task:** `pack565` above is a plain Python loop, which is correct but slow — the original script used numpy vectorisation for this and ran four panels at 3 fps on a Pi 3B+. Measure it on the Pi during the hardware checklist; if a frame takes more than ~50 ms, restore the numpy path (`numpy` is already in the `hardware` extra) and keep this loop as the fallback when numpy is absent. Do not optimise it before measuring. Also verify luma's current `spi()` keyword names against upstream docs before trusting the constructor call above.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_displays.py -v`
Expected: PASS — the `gc9a01` build test skips if luma is installed, passes otherwise.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): display backend protocol, virtual and GC9A01 backends"
```

---

### Task 9: Config loading and screen resolution

**Files:**
- Create: `daemon/src/ors_daemon/config.py`
- Test: `daemon/tests/test_config.py`

**Interfaces:**
- Consumes: `DaemonConfig`, `ors_render.load_builtin_templates`, `Template.bind_params`
- Produces:
  - `ConfigError(Exception)` — message names the offending field
  - `load_config(path: Path) -> DaemonConfig`
  - `ResolvedScreen(config: ScreenConfig, scenes: list[Scene], params: dict[str, Any], depends_on: frozenset[str])` — frozen dataclass
  - `resolve_screens(config: DaemonConfig) -> list[ResolvedScreen]` — enabled screens only, ordered by `position`
  - `system_scenes() -> dict[str, Scene]` — keyed by scene name

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_config.py`:

```python
import pytest
import yaml

from ors_daemon.config import ConfigError, load_config, resolve_screens, system_scenes

BASE = {
    "version": 1,
    "timezone": "Europe/Amsterdam",
    "integrations": [
        {"name": "prom", "type": "prometheus", "url": "http://p:9090", "fields": {"cpu": {"query": "up"}}}
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "ring-gauge",
            "params": {"title": "CPU", "value": "{{prom.cpu}}", "big": "{{prom.cpu | round:0}}%"},
        }
    ],
}


def write(tmp_path, config):
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_a_valid_file_loads(tmp_path):
    config = load_config(write(tmp_path, BASE))
    assert config.timezone == "Europe/Amsterdam"


def test_a_missing_file_reports_its_path(tmp_path):
    with pytest.raises(ConfigError, match="rack.yaml"):
        load_config(tmp_path / "rack.yaml")


def test_malformed_yaml_reports_a_config_error(tmp_path):
    path = tmp_path / "rack.yaml"
    path.write_text("screens: [unclosed")
    with pytest.raises(ConfigError):
        load_config(path)


def test_an_invalid_field_is_named_in_the_message(tmp_path):
    broken = {**BASE, "night": {"start": "25:00"}}
    with pytest.raises(ConfigError, match="night"):
        load_config(write(tmp_path, broken))


def test_a_screen_naming_an_unknown_template_is_rejected(tmp_path):
    broken = {**BASE, "screens": [{**BASE["screens"][0], "template": "nope"}]}
    with pytest.raises(ConfigError, match="nope"):
        resolve_screens(load_config(write(tmp_path, broken)))


def test_a_resolved_screen_carries_its_template_scenes_and_bound_params(tmp_path):
    screens = resolve_screens(load_config(write(tmp_path, BASE)))

    assert len(screens) == 1
    assert screens[0].scenes, "a resolved screen must carry renderable scenes"
    assert screens[0].params["title"] == "CPU"
    assert "subtitle" in screens[0].params, "template defaults must be merged in"


def test_dependencies_are_derived_from_params_and_scenes(tmp_path):
    screens = resolve_screens(load_config(write(tmp_path, BASE)))
    assert screens[0].depends_on == frozenset({"prom"})


def test_a_screen_referencing_no_integration_depends_on_nothing(tmp_path):
    static = {
        **BASE,
        "screens": [
            {
                **BASE["screens"][0],
                "template": "text-only",
                "params": {"big": "HELLO"},
            }
        ],
    }
    screens = resolve_screens(load_config(write(tmp_path, static)))
    assert screens[0].depends_on == frozenset()


def test_a_binding_naming_an_unconfigured_namespace_is_not_a_dependency(tmp_path):
    stray = {
        **BASE,
        "screens": [{**BASE["screens"][0], "params": {"big": "{{qbit.speed}}"}}],
    }
    screens = resolve_screens(load_config(write(tmp_path, stray)))
    assert screens[0].depends_on == frozenset()


def test_disabled_screens_are_dropped_and_the_rest_ordered_by_position(tmp_path):
    many = {
        **BASE,
        "screens": [
            {**BASE["screens"][0], "name": "C", "position": 3},
            {**BASE["screens"][0], "name": "A", "position": 1},
            {**BASE["screens"][0], "name": "OFF", "position": 2, "enabled": False},
        ],
    }
    names = [screen.config.name for screen in resolve_screens(load_config(write(tmp_path, many)))]
    assert names == ["A", "C"]


def test_an_inline_template_overrides_a_builtin_of_the_same_name(tmp_path):
    inline = {
        **BASE,
        "templates": {
            "ring-gauge": {
                "name": "ring-gauge",
                "scenes": [{"name": "custom", "elements": [{"type": "text", "text": "X"}]}],
            }
        },
    }
    screens = resolve_screens(load_config(write(tmp_path, inline)))
    assert screens[0].scenes[0].name == "custom"


def test_system_scenes_are_available_by_name():
    scenes = system_scenes()
    assert {"connecting", "stale", "error", "identify"} <= set(scenes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.config'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/config.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig, ScreenConfig
from ors_schema.scene import Scene, Template
from pydantic import ValidationError

_NAMESPACE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*[.\[]")


class ConfigError(Exception):
    """The config could not be read, parsed or validated."""


@dataclass(frozen=True)
class ResolvedScreen:
    config: ScreenConfig
    scenes: list[Scene]
    params: dict[str, Any]
    depends_on: frozenset[str]


def load_config(path: Path) -> DaemonConfig:
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return DaemonConfig.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "(root)"
        raise ConfigError(f"{path}: {location}: {first['msg']}") from exc


def _templates(config: DaemonConfig) -> dict[str, Template]:
    return {**load_builtin_templates(), **config.templates}


def system_scenes() -> dict[str, Scene]:
    """The `system` template's scenes, keyed by name.

    They carry no `when`, so the daemon selects them by name rather than by
    condition -- see the screen worker's health stage.
    """
    return {scene.name: scene for scene in load_builtin_templates()["system"].scenes}


def _dependencies(scenes: list[Scene], params: dict[str, Any], known: set[str]) -> frozenset[str]:
    text = " ".join(str(value) for value in params.values())
    text += " ".join(scene.model_dump_json() for scene in scenes)
    return frozenset(name for name in _NAMESPACE.findall(text) if name in known)


def resolve_screens(config: DaemonConfig) -> list[ResolvedScreen]:
    available = _templates(config)
    known = {integration.name for integration in config.integrations}

    resolved: list[ResolvedScreen] = []
    for screen in sorted(config.screens, key=lambda item: item.position):
        if not screen.enabled:
            continue
        template = available.get(screen.template)
        if template is None:
            raise ConfigError(
                f"screen {screen.name!r} names template {screen.template!r}, which is not defined"
            )
        params = template.bind_params(screen.params)
        resolved.append(
            ResolvedScreen(
                config=screen,
                scenes=list(template.scenes),
                params=params,
                depends_on=_dependencies(list(template.scenes), params, known),
            )
        )
    return resolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_config.py -v`
Expected: PASS — 12 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): config loading, template resolution and dependency derivation"
```

---

### Task 10: Screen worker

**Files:**
- Create: `daemon/src/ors_daemon/screen.py`
- Test: `daemon/tests/test_screen.py`

**Interfaces:**
- Consumes: `ResolvedScreen`, `SnapshotStore`, `DisplayBackend`, `Clock`, `NightWindow`, `system_scenes`, `ors_render.render_screen`
- Produces:
  - `ScreenWorker(threading.Thread)` constructed with `(screen, store, display, system, night, stop, clock, floor=5.0)`
  - `.tick() -> None` — one loop iteration without waiting, the unit tests drive
  - `.current_scene: str | None`, `.last_render: datetime | None`, `.renders: int`, `.faulted: bool`, `.asleep: bool`, `.heartbeat: float`
  - `.identify(ordinal: str) -> None` — renders the `identify` system scene once, immediately

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_screen.py`:

```python
import threading
from datetime import datetime, timezone

import pytest
from ors_schema.daemon import DisplayConfig, NightWindow, ScreenConfig

from ors_daemon.clock import FakeClock
from ors_daemon.config import ResolvedScreen, system_scenes
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_render import load_builtin_templates

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class RecordingDisplay:
    def __init__(self, fail_times=0):
        self.images = []
        self.sleeps = 0
        self.wakes = 0
        self.closed = 0
        self.fail_times = fail_times

    def show(self, image):
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("SPI write failed")
        self.images.append(image)

    def sleep(self):
        self.sleeps += 1

    def wake(self):
        self.wakes += 1

    def close(self):
        self.closed += 1


def make(params=None, night=None, clock=None, display=None, template="ring-gauge", depends=("prom",)):
    resolved = ResolvedScreen(
        config=ScreenConfig(
            name="CPU",
            position=1,
            display=DisplayConfig(backend="virtual", out_dir="/tmp"),
            rotation=0,
            hflip=False,
            template=template,
            params={},
        ),
        scenes=list(load_builtin_templates()[template].scenes),
        params=load_builtin_templates()[template].bind_params(
            params if params is not None else {"title": "CPU", "value": "{{prom.cpu}}", "big": "42%"}
        ),
        depends_on=frozenset(depends),
    )
    store = SnapshotStore()
    store.register("prom")
    worker = ScreenWorker(
        screen=resolved,
        store=store,
        display=display or RecordingDisplay(),
        system=system_scenes(),
        night=night or NightWindow(enabled=False),
        stop=threading.Event(),
        clock=clock or FakeClock(NOW),
        floor=5.0,
    )
    return worker, store, worker._display


def test_a_screen_with_no_data_yet_shows_the_connecting_scene():
    worker, _, display = make()
    worker.tick()

    assert worker.current_scene == "connecting"
    assert len(display.images) == 1


def test_a_stale_source_shows_the_stale_scene():
    worker, store, _ = make()
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    for _ in range(3):
        store.fail("prom", "timeout", now=NOW)
    worker.tick()

    assert worker.current_scene == "stale"


def test_healthy_data_selects_a_template_scene():
    worker, store, _ = make()
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()

    assert worker.current_scene not in {"connecting", "stale", "error"}


def test_a_screen_depending_on_nothing_never_waits_for_an_integration():
    worker, _, _ = make(params={"big": "HELLO"}, template="text-only", depends=())
    worker.tick()

    assert worker.current_scene == "default"


def test_nothing_is_redrawn_while_data_and_scene_are_unchanged():
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()
    worker.tick()
    worker.tick()

    assert len(display.images) == 1


def test_new_data_triggers_exactly_one_redraw():
    worker, store, display = make()
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()
    store.put("prom", {"cpu": 43.0}, latency_ms=1.0, now=NOW)
    worker.tick()

    assert len(display.images) == 2


def test_the_heartbeat_floor_redraws_a_frozen_screen():
    clock = FakeClock(NOW)
    worker, store, display = make(clock=clock)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()
    clock.advance(5.1)
    worker.tick()

    assert len(display.images) == 2


def test_entering_the_night_window_sleeps_the_panel_and_stops_rendering():
    clock = FakeClock(datetime(2026, 8, 11, 23, 30, tzinfo=timezone.utc))
    worker, store, display = make(night=NightWindow(start="23:00", end="07:00"), clock=clock)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)

    worker.tick()
    worker.tick()

    assert display.sleeps == 1, "sleep is sent once, not every tick"
    assert display.images == []
    assert worker.asleep is True


def test_leaving_the_night_window_wakes_the_panel_and_renders():
    clock = FakeClock(datetime(2026, 8, 11, 23, 30, tzinfo=timezone.utc))
    worker, store, display = make(night=NightWindow(start="23:00", end="07:00"), clock=clock)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()

    clock.advance(9 * 3600)
    worker.tick()

    assert display.wakes == 1
    assert len(display.images) == 1


def test_a_per_screen_override_replaces_the_global_window():
    clock = FakeClock(datetime(2026, 8, 11, 23, 30, tzinfo=timezone.utc))
    worker, store, display = make(night=NightWindow(start="23:00", end="07:00"), clock=clock)
    worker._night = NightWindow(enabled=False)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()

    assert display.sleeps == 0
    assert len(display.images) == 1


def test_a_display_failure_retries_then_faults_the_screen_without_raising():
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)

    for _ in range(4):
        worker.tick()

    assert worker.faulted is True


def test_a_faulted_screen_stops_touching_its_backend():
    display = RecordingDisplay(fail_times=99)
    worker, store, _ = make(display=display)
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    for _ in range(4):
        worker.tick()
    before = display.fail_times

    worker.tick()
    assert display.fail_times == before


def test_identify_renders_the_ordinal_immediately():
    worker, _, display = make()
    worker.identify("2")

    assert worker.current_scene == "identify"
    assert len(display.images) == 1


@pytest.mark.parametrize(("rotation", "hflip"), [(0, False), (90, False), (270, True), (180, True)])
def test_rotation_and_flip_are_applied_before_the_backend_sees_the_image(rotation, hflip):
    worker, store, display = make()
    worker._screen.config.rotation = rotation
    worker._screen.config.hflip = hflip
    store.put("prom", {"cpu": 42.0}, latency_ms=1.0, now=NOW)
    worker.tick()

    assert display.images[0].size == (240, 240)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.screen'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/screen.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from ors_render import RenderContext, render_screen, select_scene
from ors_schema.daemon import NightWindow
from ors_schema.scene import Scene
from PIL import Image

from ors_daemon.clock import Clock, in_window, seconds_until_boundary
from ors_daemon.config import ResolvedScreen
from ors_daemon.snapshot import Health, SnapshotStore

log = logging.getLogger(__name__)

_MAX_DISPLAY_RETRIES = 3


class ScreenWorker(threading.Thread):
    """Owns one panel: what to draw, when to draw it, and when to stop."""

    def __init__(
        self,
        screen: ResolvedScreen,
        store: SnapshotStore,
        display: object,
        system: dict[str, Scene],
        night: NightWindow,
        stop: threading.Event,
        clock: Clock,
        floor: float = 5.0,
    ) -> None:
        super().__init__(name=f"screen-{screen.config.name}", daemon=True)
        self._screen = screen
        self._store = store
        self._display = display
        self._system = system
        self._night = screen.config.sleep_override or night
        self._stop = stop
        self._clock = clock
        self._floor = floor
        self._seen_version = -1
        self._last_render_monotonic = 0.0
        self._failures = 0
        self._logged_error: str | None = None

        self.screen_name = screen.config.name
        """The panel's name. The thread's own `name` is prefixed, so status
        reporting reads this rather than parsing the thread name back apart."""
        self.current_scene: str | None = None
        self.last_render: datetime | None = None
        self.renders = 0
        self.faulted = False
        self.asleep = False
        self.heartbeat = 0.0

    def tick(self) -> None:
        self.heartbeat = time.monotonic()
        now = self._clock()

        if in_window(now, self._night):
            if not self.asleep:
                self._display.sleep()
                self.asleep = True
                log.info("night mode", extra={"screen": self._screen.config.name})
            return

        if self.asleep:
            self._display.wake()
            self.asleep = False

        if self.faulted:
            return

        snapshot = self._store.read()
        scene, name = self._select(snapshot)
        if not self._should_render(snapshot.version, name):
            return
        self._render_and_show(scene, name, snapshot)

    def identify(self, ordinal: str) -> None:
        scene = self._system["identify"]
        context = RenderContext(data={"params": {"ordinal": ordinal}})
        self._show(render_screen([scene], context), "identify")

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self.tick()
                self._store.wait_for_change(self._seen_version, timeout=self._wait_for())
        finally:
            self._display.close()

    def _wait_for(self) -> float:
        return min(self._floor, seconds_until_boundary(self._clock(), self._night))

    def _select(self, snapshot) -> tuple[Scene, str]:
        for name in self._screen.depends_on:
            health = snapshot.health.get(name)
            if health is None or health.state is Health.CONNECTING:
                return self._system["connecting"], "connecting"
            if health.stale:
                return self._system["stale"], "stale"

        context = self._context(snapshot)
        chosen = select_scene(self._screen.scenes, context)
        if chosen is None:
            return self._system["stale"], "stale"
        return chosen, chosen.name

    def _context(self, snapshot) -> RenderContext:
        return RenderContext(data={**snapshot.data, "params": self._screen.params})

    def _should_render(self, version: int, scene_name: str) -> bool:
        if version != self._seen_version:
            return True
        if scene_name != self.current_scene:
            return True
        return (time.monotonic() - self._last_render_monotonic) >= self._floor

    def _render_and_show(self, scene: Scene, name: str, snapshot) -> None:
        try:
            image = render_screen([scene], self._context(snapshot))
        except Exception as exc:  # the renderer promises not to, but a panel must survive it
            message = f"{type(exc).__name__}: {exc}"
            if message != self._logged_error:
                log.error("render failed", extra={"screen": self._screen.config.name, "error": message})
                self._logged_error = message
            image = render_screen(
                [self._system["error"]], RenderContext(data={"params": {"message": message[:40]}})
            )
            name = "error"
        self._seen_version = snapshot.version
        self._show(image, name)

    def _show(self, image: Image.Image, name: str) -> None:
        rotation = self._screen.config.rotation
        if rotation:
            image = image.rotate(-rotation)
        if self._screen.config.hflip:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)

        try:
            self._display.show(image)
        except Exception as exc:
            self._failures += 1
            log.warning(
                "display write failed",
                extra={"screen": self._screen.config.name, "error": str(exc), "attempt": self._failures},
            )
            if self._failures >= _MAX_DISPLAY_RETRIES:
                self.faulted = True
                log.error("screen faulted", extra={"screen": self._screen.config.name})
            return

        self._failures = 0
        self.current_scene = name
        self.last_render = self._clock()
        self._last_render_monotonic = time.monotonic()
        self.renders += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_screen.py -v`
Expected: PASS — 17 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): screen worker with change-driven pacing and night mode"
```

---

### Task 11: Status file

**Files:**
- Create: `daemon/src/ors_daemon/status.py`
- Test: `daemon/tests/test_status.py`

**Interfaces:**
- Consumes: `SnapshotStore`, `ScreenWorker`, `Clock`
- Produces:
  - `build_status(started_at, now, config_version, screens, snapshot) -> dict[str, Any]`
  - `write_status(path: Path, payload: dict[str, Any]) -> None` — temp file, fsync, rename

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_status.py`:

```python
import json
import threading
from datetime import datetime, timedelta, timezone

from ors_daemon.snapshot import SnapshotStore
from ors_daemon.status import build_status, write_status

START = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class FakeWorker:
    def __init__(self, name="CPU"):
        self.screen_name = name
        self.current_scene = "default"
        self.last_render = START
        self.renders = 12
        self.faulted = False
        self.asleep = False


def test_status_reports_uptime_screens_and_integration_health():
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=12.5, now=START)

    payload = build_status(
        started_at=START,
        now=START + timedelta(seconds=90),
        config_version=1,
        screens=[FakeWorker()],
        snapshot=store.read(),
    )

    assert payload["uptime_s"] == 90
    assert payload["config_version"] == 1
    assert payload["screens"][0] == {
        "name": "CPU",
        "scene": "default",
        "state": "awake",
        "last_render": START.isoformat(),
        "renders": 12,
    }
    assert payload["integrations"][0]["name"] == "prom"
    assert payload["integrations"][0]["state"] == "healthy"
    assert payload["integrations"][0]["latency_ms"] == 12.5
    assert payload["integrations"][0]["last_error"] is None


def test_a_sleeping_and_a_faulted_screen_report_their_state():
    sleeping, faulted = FakeWorker("A"), FakeWorker("B")
    sleeping.asleep = True
    faulted.faulted = True

    payload = build_status(START, START, 1, [sleeping, faulted], SnapshotStore().read())
    assert [screen["state"] for screen in payload["screens"]] == ["asleep", "faulted"]


def test_a_failing_integration_reports_its_reason():
    store = SnapshotStore()
    store.register("prom")
    store.fail("prom", "connection refused", now=START)

    payload = build_status(START, START, 1, [], store.read())
    assert payload["integrations"][0]["state"] == "unhealthy"
    assert payload["integrations"][0]["last_error"] == "connection refused"


def test_write_status_produces_readable_json(tmp_path):
    path = tmp_path / "status.json"
    write_status(path, {"uptime_s": 1})

    assert json.loads(path.read_text()) == {"uptime_s": 1}


def test_write_status_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "status.json"
    write_status(path, {"uptime_s": 1})

    assert [item.name for item in tmp_path.iterdir()] == ["status.json"]


def test_a_reader_never_observes_a_partial_file(tmp_path):
    path = tmp_path / "status.json"
    write_status(path, {"n": 0})
    stop = threading.Event()
    corrupt = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except (json.JSONDecodeError, FileNotFoundError) as exc:
                corrupt.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    for n in range(200):
        write_status(path, {"n": n, "padding": "x" * 5000})
    stop.set()
    thread.join(timeout=5.0)

    assert corrupt == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.status'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/status.py`:

```python
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ors_daemon.snapshot import Snapshot


def build_status(
    started_at: datetime,
    now: datetime,
    config_version: int,
    screens: list[Any],
    snapshot: Snapshot,
) -> dict[str, Any]:
    """Assemble what a person over SSH -- and later the server -- needs to see."""
    return {
        "uptime_s": int((now - started_at).total_seconds()),
        "config_version": config_version,
        "screens": [
            {
                "name": worker.screen_name,
                "scene": worker.current_scene,
                "state": "faulted" if worker.faulted else ("asleep" if worker.asleep else "awake"),
                "last_render": worker.last_render.isoformat() if worker.last_render else None,
                "renders": worker.renders,
            }
            for worker in screens
        ],
        "integrations": [
            {
                "name": name,
                "state": health.state.value,
                "stale": health.stale,
                "latency_ms": health.latency_ms,
                "last_success": health.last_success.isoformat() if health.last_success else None,
                "last_error": health.reason,
            }
            for name, health in snapshot.health.items()
        ],
    }


def write_status(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically, so a reader polling this file never sees half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "w") as handle:
        json.dump(payload, handle, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_status.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): atomic status file"
```

---

### Task 12: Supervisor

**Files:**
- Create: `daemon/src/ors_daemon/supervisor.py`
- Test: `daemon/tests/test_supervisor.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `Supervisor(config, screens, store, clock, status_path, display_factory=None, poller_factory=None, watchdog_timeout=30.0)`
  - `.start() -> None`, `.stop() -> None`, `.tick() -> None` (one watchdog + status cycle), `.run_forever() -> None`
  - `.workers: list[ScreenWorker]`, `.pollers: list[Poller]`, `.tunnels: list[Tunnel]`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_supervisor.py`:

```python
import json
import threading
from datetime import datetime, timezone

import yaml
from ors_schema.daemon import DaemonConfig

from ors_daemon.clock import FakeClock
from ors_daemon.config import resolve_screens
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class RecordingDisplay:
    def __init__(self):
        self.images, self.sleeps, self.closed = [], 0, 0

    def show(self, image):
        self.images.append(image)

    def sleep(self):
        self.sleeps += 1

    def wake(self):
        pass

    def close(self):
        self.closed += 1


def config_dict(tmp_path, screens=2):
    return {
        "version": 1,
        "timezone": "UTC",
        "night": {"enabled": False},
        "integrations": [
            {"name": "prom", "type": "prometheus", "url": "http://p:9090", "fields": {"cpu": {"query": "up"}}}
        ],
        "screens": [
            {
                "name": f"S{n}",
                "position": n,
                "display": {"backend": "virtual", "out_dir": str(tmp_path / "panels")},
                "template": "ring-gauge",
                "params": {"title": f"S{n}", "value": "{{prom.cpu}}", "big": "{{prom.cpu | round:0}}%"},
            }
            for n in range(1, screens + 1)
        ],
    }


def make(tmp_path, screens=2):
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens))
    store = SnapshotStore()
    displays = {}

    def display_factory(screen_config, name):
        displays[name] = RecordingDisplay()
        return displays[name]

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    return supervisor, store, displays


def test_start_creates_one_worker_per_enabled_screen(tmp_path):
    supervisor, _, displays = make(tmp_path, screens=3)
    supervisor.start()
    try:
        assert len(supervisor.workers) == 3
        assert set(displays) == {"S1", "S2", "S3"}
    finally:
        supervisor.stop()


def test_a_tick_writes_the_status_file(tmp_path):
    supervisor, store, _ = make(tmp_path)
    store.register("prom")
    supervisor.start()
    try:
        supervisor.tick()
        payload = json.loads((tmp_path / "status.json").read_text())
        assert len(payload["screens"]) == 2
        assert payload["integrations"][0]["name"] == "prom"
    finally:
        supervisor.stop()


def test_the_watchdog_restarts_a_worker_whose_heartbeat_went_stale(tmp_path):
    supervisor, _, _ = make(tmp_path, screens=1)
    supervisor.start()
    try:
        original = supervisor.workers[0]
        original.heartbeat = 0.0
        supervisor.tick()
        assert supervisor.workers[0] is not original
    finally:
        supervisor.stop()


def test_stop_sleeps_and_closes_every_panel(tmp_path):
    supervisor, _, displays = make(tmp_path)
    supervisor.start()
    supervisor.stop()

    for display in displays.values():
        assert display.sleeps == 1
        assert display.closed == 1


def test_a_screen_whose_backend_cannot_be_built_does_not_stop_the_others(tmp_path):
    config = DaemonConfig.model_validate(config_dict(tmp_path, screens=2))
    store = SnapshotStore()

    def display_factory(screen_config, name):
        if name == "S1":
            raise RuntimeError("no such SPI bus")
        return RecordingDisplay()

    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=display_factory,
        poller_factory=lambda integration_config, url_provider: None,
    )
    supervisor.start()
    try:
        assert [worker.name for worker in supervisor.workers] == ["screen-S2"]
    finally:
        supervisor.stop()


def test_a_tunnelled_integration_gets_a_tunnel(tmp_path):
    raw = config_dict(tmp_path)
    raw["integrations"][0]["tunnel"] = {
        "kubeconfig": "/tmp/kc",
        "namespace": "monitoring",
        "remote_port": 9090,
        "local_port": 19090,
    }
    config = DaemonConfig.model_validate(raw)
    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=SnapshotStore(),
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        display_factory=lambda screen_config, name: RecordingDisplay(),
        poller_factory=lambda integration_config, url_provider: None,
        tunnel_factory=lambda tunnel_config, stop: _FakeTunnel(),
    )
    supervisor.start()
    try:
        assert len(supervisor.tunnels) == 1
    finally:
        supervisor.stop()


class _FakeTunnel:
    def __init__(self):
        self.ready = threading.Event()
        self.base_url = "http://localhost:19090"
        self.started = False

    def start(self):
        self.started = True

    def shutdown(self):
        pass

    def is_alive(self):
        return self.started

    def join(self, timeout=None):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.supervisor'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/supervisor.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ors_schema.daemon import DaemonConfig, IntegrationConfig, ScreenConfig, TunnelConfig

from ors_daemon.clock import Clock
from ors_daemon.config import ResolvedScreen, system_scenes
from ors_daemon.displays import build_display
from ors_daemon.integrations import build_integration
from ors_daemon.poller import Poller
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.status import build_status, write_status
from ors_daemon.tunnel import Tunnel

log = logging.getLogger(__name__)

DisplayFactory = Callable[[ScreenConfig, str], Any]
PollerFactory = Callable[[IntegrationConfig, Callable[[], str] | None], Any]
TunnelFactory = Callable[[TunnelConfig, threading.Event], Any]


class Supervisor:
    """Starts threads, watches their heartbeats, and shuts the rack down cleanly."""

    def __init__(
        self,
        config: DaemonConfig,
        screens: list[ResolvedScreen],
        store: SnapshotStore,
        clock: Clock,
        status_path: Path,
        display_factory: DisplayFactory | None = None,
        poller_factory: PollerFactory | None = None,
        tunnel_factory: TunnelFactory | None = None,
        watchdog_timeout: float = 30.0,
    ) -> None:
        self._config = config
        self._screens = screens
        self._store = store
        self._clock = clock
        self._status_path = Path(status_path)
        self._display_factory = display_factory or (lambda screen, name: build_display(screen.display, name))
        self._poller_factory = poller_factory
        self._tunnel_factory = tunnel_factory or (lambda cfg, stop: Tunnel(config=cfg, stop=stop))
        self._watchdog_timeout = watchdog_timeout
        self._stop = threading.Event()
        self._displays: dict[str, Any] = {}
        self._started_at = clock()

        self.workers: list[ScreenWorker] = []
        self.pollers: list[Any] = []
        self.tunnels: list[Any] = []

    def start(self) -> None:
        for integration_config in self._config.integrations:
            self._store.register(integration_config.name)
            self._start_integration(integration_config)
        for screen in self._screens:
            self._start_screen(screen)

    def tick(self) -> None:
        now = time.monotonic()
        for index, worker in enumerate(list(self.workers)):
            if worker.heartbeat and (now - worker.heartbeat) > self._watchdog_timeout:
                log.error("worker wedged, restarting", extra={"screen": worker.name})
                self.workers[index] = self._restart(worker)
        write_status(
            self._status_path,
            build_status(
                started_at=self._started_at,
                now=self._clock(),
                config_version=self._config.version,
                screens=self.workers,
                snapshot=self._store.read(),
            ),
        )

    def run_forever(self, interval: float = 1.0) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(interval)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for worker in self.workers:
            worker.join(timeout=5.0)
        for poller in self.pollers:
            poller.join(timeout=5.0)
        for tunnel in self.tunnels:
            tunnel.shutdown()
        for display in self._displays.values():
            try:
                display.sleep()
                display.close()
            except Exception:
                log.warning("could not shut a panel down cleanly")

    def _start_integration(self, integration_config: IntegrationConfig) -> None:
        url_provider = None
        if integration_config.tunnel is not None:
            tunnel = self._tunnel_factory(integration_config.tunnel, self._stop)
            tunnel.start()
            self.tunnels.append(tunnel)
            url_provider = lambda: tunnel.base_url  # noqa: E731 - deliberate late binding

        if self._poller_factory is not None:
            poller = self._poller_factory(integration_config, url_provider)
        else:
            poller = Poller(
                integration=build_integration(integration_config, url_provider),
                store=self._store,
                interval=integration_config.poll_interval,
                stop=self._stop,
                clock=self._clock,
            )
        if poller is not None:
            poller.start()
            self.pollers.append(poller)

    def _start_screen(self, screen: ResolvedScreen) -> None:
        name = screen.config.name
        try:
            display = self._display_factory(screen.config, name)
        except Exception as exc:
            log.error("screen unavailable", extra={"screen": name, "error": str(exc)})
            return
        self._displays[name] = display
        worker = self._make_worker(screen, display)
        worker.start()
        self.workers.append(worker)

    def _make_worker(self, screen: ResolvedScreen, display: Any) -> ScreenWorker:
        return ScreenWorker(
            screen=screen,
            store=self._store,
            display=display,
            system=system_scenes(),
            night=self._config.night,
            stop=self._stop,
            clock=self._clock,
        )

    def _restart(self, worker: ScreenWorker) -> ScreenWorker:
        screen = next(item for item in self._screens if f"screen-{item.config.name}" == worker.name)
        replacement = self._make_worker(screen, self._displays[screen.config.name])
        replacement.start()
        return replacement
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests/test_supervisor.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): supervisor with watchdog and clean shutdown"
```

---

### Task 13: CLI, example config, systemd unit, end-to-end test

**Files:**
- Create: `daemon/src/ors_daemon/__main__.py`
- Create: `daemon/examples/rack.yaml`
- Create: `daemon/examples/openrackscreen.service`
- Create: `daemon/README.md`
- Test: `daemon/tests/test_cli.py`, `daemon/tests/test_end_to_end.py`

**Interfaces:**
- Consumes: everything above
- Produces: `main(argv: list[str] | None = None) -> int` with subcommands `run`, `validate`, `render`, `identify`

- [ ] **Step 1: Write the failing test**

`daemon/tests/test_cli.py`:

```python
import json
from pathlib import Path

import yaml

from ors_daemon.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rack.yaml"


def write_virtual_config(tmp_path):
    config = yaml.safe_load(EXAMPLE.read_text())
    for screen in config["screens"]:
        screen["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    for integration in config["integrations"]:
        integration.pop("tunnel", None)
        integration["url"] = "http://127.0.0.1:1"
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_the_shipped_example_config_validates():
    assert main(["validate", "--config", str(EXAMPLE)]) == 0


def test_validate_reports_a_broken_config_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "rack.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "night": {"start": "99:99"}}))

    assert main(["validate", "--config", str(path)]) == 1
    assert "night" in capsys.readouterr().err


def test_render_writes_one_png_per_screen_without_touching_hardware(tmp_path):
    path = write_virtual_config(tmp_path)

    assert main(["render", "--config", str(path), "--out", str(tmp_path / "out")]) == 0
    written = sorted(item.name for item in (tmp_path / "out").glob("*.png"))
    assert len(written) == 4


def test_render_accepts_a_data_file_so_a_screen_can_be_checked_offline(tmp_path):
    path = write_virtual_config(tmp_path)
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"prom": {"cpu": 42.4, "nodes_ready": 3, "nodes_total": 3, "alerts": 0}}))

    assert main(["render", "--config", str(path), "--out", str(tmp_path / "out"), "--data", str(data)]) == 0
    assert (tmp_path / "out" / "CPU.png").exists()


def test_an_unknown_subcommand_exits_nonzero(tmp_path):
    assert main(["frobnicate"]) != 0
```

`daemon/tests/test_end_to_end.py`:

```python
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from ors_schema.daemon import DaemonConfig
from PIL import Image

from ors_daemon.clock import FakeClock
from ors_daemon.config import resolve_screens
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rack.yaml"

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


def virtual_config(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["night"] = {"enabled": False}
    for screen in raw["screens"]:
        screen["display"] = {"backend": "virtual", "out_dir": str(tmp_path / "panels")}
    for integration in raw["integrations"]:
        integration.pop("tunnel", None)
    return DaemonConfig.model_validate(raw)


def test_the_whole_rack_renders_from_the_example_config(tmp_path):
    config = virtual_config(tmp_path)
    store = SnapshotStore()
    supervisor = Supervisor(
        config=config,
        screens=resolve_screens(config),
        store=store,
        clock=FakeClock(NOW),
        status_path=tmp_path / "status.json",
        poller_factory=lambda integration_config, url_provider: None,
    )
    rendered = threading.Event()
    supervisor.start()
    try:
        store.register("prom")
        store.put("prom", HEALTHY, latency_ms=5.0, now=NOW)
        for _ in range(200):
            if len(list((tmp_path / "panels").glob("*.png"))) == 4:
                rendered.set()
                break
            supervisor._stop.wait(0.02)
        supervisor.tick()
    finally:
        supervisor.stop()

    assert rendered.is_set(), "all four panels should render once data arrives"
    for panel in (tmp_path / "panels").glob("*.png"):
        assert Image.open(panel).size == (240, 240)

    status = json.loads((tmp_path / "status.json").read_text())
    assert {screen["name"] for screen in status["screens"]} == {"CPU", "MEM", "PODS", "HEALTH"}
    assert all(screen["scene"] not in {None, "connecting"} for screen in status["screens"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest daemon/tests/test_cli.py daemon/tests/test_end_to_end.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_daemon.__main__'`

- [ ] **Step 3: Write minimal implementation**

`daemon/src/ors_daemon/__main__.py`:

```python
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from ors_render import RenderContext, render_screen

from ors_daemon.clock import ClockError, system_clock
from ors_daemon.config import ConfigError, load_config, resolve_screens, system_scenes
from ors_daemon.logging import setup_logging
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.supervisor import Supervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ors-daemon")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "validate"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--config", required=True, type=Path)
    subparsers.choices["run"].add_argument("--status", type=Path, default=Path("/tmp/ors-status.json"))

    render = subparsers.add_parser("render")
    render.add_argument("--config", required=True, type=Path)
    render.add_argument("--out", required=True, type=Path)
    render.add_argument("--data", type=Path)

    identify = subparsers.add_parser("identify")
    identify.add_argument("--config", required=True, type=Path)

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    try:
        config = load_config(args.config)
        screens = resolve_screens(config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"ok: {len(screens)} screen(s), {len(config.integrations)} integration(s)")
        return 0

    if args.command == "render":
        return _render(screens, args.out, args.data)

    try:
        clock = system_clock(config.timezone)
    except ClockError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    supervisor = Supervisor(
        config=config,
        screens=screens,
        store=SnapshotStore(),
        clock=clock,
        status_path=getattr(args, "status", Path("/tmp/ors-status.json")),
    )

    if args.command == "identify":
        supervisor.start()
        try:
            for index, worker in enumerate(supervisor.workers, start=1):
                worker.identify(str(index))
        finally:
            supervisor.stop()
        return 0

    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
    signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
    supervisor.run_forever()
    return 0


def _render(screens: list, out: Path, data_path: Path | None) -> int:
    data = json.loads(data_path.read_text()) if data_path else {}
    out.mkdir(parents=True, exist_ok=True)
    scenes = system_scenes()
    for screen in screens:
        context = RenderContext(data={**data, "params": screen.params})
        chosen = screen.scenes if data else [scenes["connecting"]]
        render_screen(chosen, context).save(out / f"{screen.config.name}.png")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

`daemon/examples/rack.yaml` — the author's real rack. The wiring, PromQL and params below are the ones the script being replaced uses; keep them exact.

```yaml
version: 1
timezone: Europe/Amsterdam
night: { enabled: true, start: "23:00", end: "07:00" }

integrations:
  - name: prom
    type: prometheus
    poll_interval: 5
    url: http://localhost:19090
    timeout: 4
    tunnel:
      kubeconfig: ~/k8s-monitor.yaml
      namespace: monitoring
      service: auto
      remote_port: 9090
      local_port: 19090
    fields:
      cpu:
        query: '100-avg(rate(node_cpu_seconds_total{mode="idle"}[2m]))*100'
      cpu_hot:
        query: '100-avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[2m]))*100'
        reduce: top
        label: instance
        strip: last_octet
      mem:
        query: '(1-sum(node_memory_MemAvailable_bytes)/sum(node_memory_MemTotal_bytes))*100'
      mem_used_gb:
        query: '(sum(node_memory_MemTotal_bytes)-sum(node_memory_MemAvailable_bytes))/1073741824'
      mem_total_gb:
        query: 'sum(node_memory_MemTotal_bytes)/1073741824'
      mem_hot:
        query: '(1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)*100'
        reduce: top
        label: instance
        strip: last_octet
      pods_run:
        query: 'sum(kube_pod_status_phase{phase="Running"}) or vector(0)'
      pods_tot:
        query: 'count(kube_pod_info) or vector(0)'
      nodes_ready:
        query: 'sum(kube_node_status_condition{condition="Ready",status="true"}) or vector(0)'
      nodes_total:
        query: 'count(kube_node_info) or vector(0)'
      alerts:
        query: 'count(ALERTS{alertstate="firing"}) or vector(0)'

screens:
  - name: CPU
    position: 1
    display: { backend: gc9a01, spi_bus: 0, spi_cs: 0, dc: 6, rst: 5, hz: 40000000 }
    rotation: 270
    hflip: false
    template: ring-gauge
    params:
      title: CPU
      value: "{{prom.cpu}}"
      big: "{{prom.cpu | round:0}}%"
      subtitle: cluster avg
      hint: "peak: {{prom.cpu_hot.node}} {{prom.cpu_hot.value | round:0}}%"
      palette: cyan

  - name: MEM
    position: 2
    display: { backend: gc9a01, spi_bus: 0, spi_cs: 1, dc: 13, rst: 26, hz: 40000000 }
    rotation: 270
    hflip: true
    template: ring-gauge
    params:
      title: MEM
      value: "{{prom.mem}}"
      big: "{{prom.mem | round:0}}%"
      subtitle: "{{prom.mem_used_gb | round:1}} / {{prom.mem_total_gb | round:0}} G"
      hint: "peak: {{prom.mem_hot.node}} {{prom.mem_hot.value | round:0}}%"
      palette: green

  - name: PODS
    position: 3
    display: { backend: gc9a01, spi_bus: 1, spi_cs: 0, dc: 23, rst: 22, hz: 16000000 }
    rotation: 270
    hflip: true
    template: big-number
    params:
      title: PODS
      value: "{{prom.pods_run / prom.pods_tot * 100}}"
      big: "{{prom.pods_run}}"
      subtitle: "/ {{prom.pods_tot}} total"
      palette: lime

  - name: HEALTH
    position: 4
    display: { backend: gc9a01, spi_bus: 1, spi_cs: 1, dc: 4, rst: 27, hz: 16000000 }
    rotation: 270
    hflip: true
    template: node-health
    params:
      title: NODES
```

Note the torrent screen is absent: qBittorrent is M5, and `node-health` renders the readout the HEALTH panel falls back to today.

`daemon/examples/openrackscreen.service` is a systemd unit running `ors-daemon run --config /etc/openrackscreen/rack.yaml --status /run/openrackscreen/status.json` as a non-root user in the `spi` and `gpio` groups, with `Restart=always`, `RestartSec=5` and `RuntimeDirectory=openrackscreen`.

`daemon/README.md` documents installing the hardware extra, enabling SPI1 via `dtoverlay`, the four CLI subcommands, and the hardware checklist from the spec's §10 as a printable list.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest daemon/tests -v && uv run ruff check . && uv run ruff format --check .`
Expected: PASS — the whole daemon suite green, lint clean.

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): CLI, example config, systemd unit and end-to-end test"
```

---

## Definition of done for M2

- `uv run pytest` passes from a clean checkout with no hardware; `ruff check` and `ruff format --check` pass; CI green.
- `daemon/examples/rack.yaml` validates in CI via `test_the_shipped_example_config_validates`.
- The end-to-end test renders all four panels through the virtual backend and the status file names all four screens with a non-`connecting` scene.
- `ors-daemon run` drives all four panels on the Pi from that config, with no server involved.
- Night mode sleeps and wakes the panels at the configured boundary.
- The hardware checklist in `daemon/README.md` has been walked, including the 24-hour soak and a mid-run cluster reboot.
- **`k8s_monitor.py` is stopped on the Pi.**

## What M3 picks up

M3 (server and link) consumes: `ors_schema.daemon.DaemonConfig` — the server produces the same structure it pushes down; `ors_daemon.status.build_status`'s payload, reported upstream verbatim; and `ors_daemon.config`, which gains an "apply this snapshot atomically" path alongside `load_config`. No M2 code should need changing for M3 to exist — if it does, raise it rather than patching around it.
