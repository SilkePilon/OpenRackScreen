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
