import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ors_daemon.clock import FakeClock
from ors_daemon.config import ResolvedScreen
from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import SnapshotStore
from ors_daemon.status import build_status, write_status
from ors_schema.daemon import DisplayConfig, NightWindow, ScreenConfig

START = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def make_worker(name: str = "CPU") -> ScreenWorker:
    """A real worker, never started.

    Real rather than a fake on purpose: every field the status file reads is
    read off `ScreenWorker` itself, so renaming one of them fails here rather
    than shipping a status file that quietly reports the wrong thing.
    """
    resolved = ResolvedScreen(
        config=ScreenConfig(
            name=name,
            position=1,
            display=DisplayConfig(backend="virtual", out_dir="/tmp"),
            template="ring-gauge",
            params={},
        ),
        scenes=[],
        params={},
        depends_on=frozenset(),
    )
    return ScreenWorker(
        screen=resolved,
        store=SnapshotStore(),
        # Never touched: nothing here ticks, so the panel is never drawn on.
        display=object(),  # type: ignore[arg-type]
        system={},
        night=NightWindow(enabled=False),
        stop=threading.Event(),
        clock=FakeClock(START),
    )


def rendered(name: str = "CPU", scene: str = "default", renders: int = 12) -> ScreenWorker:
    worker = make_worker(name)
    worker.current_scene = scene
    worker.last_render = START
    worker.renders = renders
    return worker


def test_status_reports_uptime_screens_and_integration_health() -> None:
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=12.5, now=START)

    payload = build_status(
        started_at=START,
        now=START + timedelta(seconds=90),
        config_version=1,
        screens=[rendered()],
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
    assert payload["integrations"][0] == {
        "name": "prom",
        "state": "healthy",
        "stale": False,
        "latency_ms": 12.5,
        "last_success": START.isoformat(),
        "last_error": None,
    }


def test_a_sleeping_and_a_faulted_screen_report_their_state() -> None:
    sleeping, faulted = rendered("A"), rendered("B")
    sleeping.asleep = True
    faulted.faulted = True

    payload = build_status(START, START, 1, [sleeping, faulted], SnapshotStore().read())
    assert [screen["state"] for screen in payload["screens"]] == ["asleep", "faulted"]


def test_a_screen_that_has_never_rendered_says_so_rather_than_guessing() -> None:
    payload = build_status(START, START, 1, [make_worker()], SnapshotStore().read())
    assert payload["screens"][0] == {
        "name": "CPU",
        "scene": None,
        "state": "awake",
        "last_render": None,
        "renders": 0,
    }


def test_a_daemon_with_no_screens_and_no_integrations_reports_empty_lists() -> None:
    payload = build_status(START, START, 1, [], SnapshotStore().read())
    assert payload["screens"] == []
    assert payload["integrations"] == []


def test_a_failing_integration_reports_its_reason() -> None:
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=12.5, now=START)
    store.fail("prom", "connection refused", now=START)

    payload = build_status(START, START, 1, [], store.read())
    assert payload["integrations"][0]["state"] == "unhealthy"
    assert payload["integrations"][0]["last_error"] == "connection refused"
    # The last *success* is what the field says, and a failure does not move it.
    assert payload["integrations"][0]["last_success"] == START.isoformat()


def test_an_integration_that_has_never_answered_reports_connecting() -> None:
    store = SnapshotStore()
    store.register("prom")
    store.fail("prom", "connection refused", now=START)

    payload = build_status(START, START, 1, [], store.read())
    # Not unhealthy: the store keeps a cold start `connecting` however many
    # times it has failed, and the status file must not relabel that.
    assert payload["integrations"][0]["state"] == "connecting"
    assert payload["integrations"][0]["last_error"] == "connection refused"
    assert payload["integrations"][0]["last_success"] is None
    assert payload["integrations"][0]["latency_ms"] is None


def test_a_stale_integration_says_so() -> None:
    store = SnapshotStore(stale_after=2)
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=12.5, now=START)
    store.fail("prom", "timeout", now=START)
    store.fail("prom", "timeout", now=START)

    payload = build_status(START, START, 1, [], store.read())
    assert payload["integrations"][0]["stale"] is True


def test_the_payload_needs_no_serialisation_fallback() -> None:
    """Every value is JSON-native already; `default=str` is a net, not the floor.

    Without this, a `datetime` or a `Health` left unconverted would still be
    written -- as whatever `str()` makes of it -- and no test would notice.
    """
    store = SnapshotStore()
    store.register("prom")
    store.put("prom", {"cpu": 1.0}, latency_ms=12.5, now=START)

    payload = build_status(START, START, 1, [rendered(), make_worker("B")], store.read())
    assert json.loads(json.dumps(payload)) == payload


def test_write_status_produces_readable_json(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    write_status(path, {"uptime_s": 1})

    assert json.loads(path.read_text()) == {"uptime_s": 1}


def test_write_status_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    write_status(path, {"uptime_s": 1})

    assert [item.name for item in tmp_path.iterdir()] == ["status.json"]


def test_write_status_creates_a_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "run" / "ors" / "status.json"
    write_status(path, {"uptime_s": 1})

    assert json.loads(path.read_text()) == {"uptime_s": 1}


def test_the_temporary_file_is_written_beside_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename is only atomic within one filesystem, so the two must share a directory.

    Asserted at the rename rather than by listing the directory, because the
    temporary file is gone by the time `write_status` returns -- which is the
    whole point of it.
    """
    renames: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: Any, target: Any) -> None:
        renames.append((Path(source), Path(target)))
        real_replace(source, target)

    # `os` itself: `ors_daemon.status.os` *is* the one module object, so there is
    # no module-local name to patch instead. Restored when the test ends.
    monkeypatch.setattr(os, "replace", recording_replace)
    path = tmp_path / "status.json"
    write_status(path, {"uptime_s": 1})

    assert renames[0][0].parent == path.parent
    assert renames[0][1] == path


def test_write_status_raises_rather_than_reporting_a_write_it_did_not_do(
    tmp_path: Path,
) -> None:
    """The caller decides whether a status file is worth stopping for. It is not."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")

    with pytest.raises(OSError):
        write_status(blocked / "status.json", {"uptime_s": 1})


def test_a_reader_never_observes_a_partial_file(tmp_path: Path) -> None:
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
