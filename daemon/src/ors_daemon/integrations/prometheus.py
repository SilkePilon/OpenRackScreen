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
        self.name: str = config.name
        self._config = config
        self._url_provider = url_provider
        self._session: Any | None = session
        # We close only what we opened. A session handed in belongs to the
        # caller, and both closing it and dropping the reference -- which would
        # make the next `poll()` silently build a real one -- are the integration
        # disposing of something it was only lent.
        self._owns_session = False

    def open(self) -> None:
        """Build the session if there is not one. Idempotent, and `poll` calls it."""
        if self._session is None:
            self._session = requests.Session()
            self._owns_session = True

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None
            self._owns_session = False

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
                log.warning(
                    "field failed",
                    extra={"integration": self.name, "field": name, "error": str(exc)},
                )
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

        # 5xx is the server, not the query: Prometheus answers 503 when a query
        # times out or is aborted, and anything in front of it answers 502/504
        # when Prometheus is gone. Either way the next field would fail the same
        # way, so this ends the whole poll.
        if response.status_code >= 500:
            raise IntegrationError(f"HTTP {response.status_code} from {url}")

        payload = response.json()
        # `status` is the envelope's authoritative marker, and upstream is
        # explicit that on an error "the data field may still hold additional
        # data" -- so a non-success body is never a source of values, whatever
        # it carries. 400 (bad parameters) and 422 (unexecutable expression)
        # arrive this way, and both are statements about this one query, so they
        # degrade the field rather than ending the poll. A body that is not a
        # JSON object at all raises here too, and lands in the same place.
        if payload.get("status") != "success":
            raise ValueError(payload.get("error") or f"status {payload.get('status')!r}")

        results = payload.get("data", {}).get("result", [])
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
