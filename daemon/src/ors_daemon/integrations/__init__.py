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
