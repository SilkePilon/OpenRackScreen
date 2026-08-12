from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from ors_schema.daemon import IntegrationConfig

UrlProvider = Callable[[], str]
"""Supplies the base URL at poll time, so a tunnel can move underneath.

It is allowed to fail. The reason this is a callable and not a string is that
the URL may not exist at the instant it is asked for -- a `kubectl port-forward`
that died between polls has no local port -- so a provider may raise, and an
implementation must treat that (and a return value that is not a usable URL) as
a failed poll rather than letting it escape. The caller of `build_integration`
therefore does not have to make its provider total.
"""


class IntegrationError(Exception):
    """A poll failed. The poller owns what happens next."""


@runtime_checkable
class Integration(Protocol):
    """A pure fetcher.

    It owns no interval, no retry policy, no health state and no threading --
    the poller owns all of that. Raising `IntegrationError` is how a failure is
    reported, and is the only failure channel. Which method may use it is part
    of the contract, because the answer is not the same for all three:

    - `open()` **may** raise `IntegrationError`, and a caller must be ready for
      it. Prometheus only builds a `requests.Session` here and cannot fail, but
      M5's qBittorrent logs in, and a login fails when the daemon starts before
      the service, or when the password is wrong. A caller treats it exactly
      like a failed `poll()`: unhealthy, backed off, retried. It does not mean
      the integration is spent -- `open()` is idempotent, so a later call may
      succeed against a service that has since come up.
    - `poll()` raises `IntegrationError` and nothing else, whatever the source
      sends back, and it calls `open()` itself. So a caller that only ever polls
      still needs exactly one `except` clause, and one that opens explicitly
      needs the same one twice.
    - `close()` raises nothing. It runs on shutdown and on the way out of a
      failed `open()`, where a second exception has nowhere useful to go; an
      implementation swallows what it cannot finish.
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
