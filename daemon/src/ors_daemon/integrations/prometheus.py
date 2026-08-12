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
        """Shut down what `open` built. Raises nothing, per the contract."""
        if not (self._owns_session and self._session is not None):
            return
        try:
            self._session.close()
        except Exception as exc:  # a second failure during shutdown has nowhere to go
            log.warning(
                "closing the session failed",
                extra={"integration": self.name, "error": str(exc)},
            )
        finally:
            # Dropped either way: a session we could not close is not one we can
            # keep using, and holding it would make the next `poll` reuse it.
            self._session = None
            self._owns_session = False

    def poll(self) -> dict[str, Any]:
        # Resolving the base URL happens first and inside the guard: the whole
        # reason `UrlProvider` is a callable is that the URL may be unavailable
        # at this instant -- a `kubectl port-forward` that died between polls --
        # so failing to produce one is an ordinary poll failure, not a crash out
        # of `poll()`. Doing it before `open()` also means a dead tunnel does not
        # cost a session.
        base = self._base_url()
        try:
            self.open()
        except IntegrationError:
            raise
        except Exception as exc:  # descriptor exhaustion, a patched factory, ...
            raise IntegrationError(f"could not open a session: {exc}") from exc
        url = f"{base}/api/v1/query"

        fields: dict[str, Any] = {}
        failures = 0
        for name, spec in self._config.fields.items():
            try:
                fields[name] = self._one(url, name, spec)
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

    def _base_url(self) -> str:
        """The base URL for this poll, or `IntegrationError`.

        Every way of not having one lands here: the provider raising, the
        provider returning `None` or something that is not a string, and a value
        that could never be requested. The alternative is a `RuntimeError` or an
        `AttributeError` escaping `poll()`, which the poller does not catch.
        """
        source = "url_provider" if self._url_provider else "config.url"
        try:
            raw: Any = self._url_provider() if self._url_provider else self._config.url
        except Exception as exc:
            raise IntegrationError(f"{source} could not supply a base URL: {exc}") from exc

        if not isinstance(raw, str):
            raise IntegrationError(f"{source} supplied {raw!r}, which is not a base URL string")
        base = raw.strip().rstrip("/")
        # A scheme is checked here rather than left to requests: without one,
        # every field fails identically with a `MissingSchema` buried in a log
        # line, and the poll ends as "every field failed" -- true, but it names
        # the queries instead of the typo that caused it.
        if not base or "://" not in base:
            raise IntegrationError(f"{source} supplied {raw!r}, which is not a usable base URL")
        return base

    def _one(self, url: str, name: str, spec: FieldSpec) -> Any:
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

        # "The query matched nothing" and "the envelope has no data at all" are
        # different events, and only the first is healthy. A defaulted `.get`
        # chain reads them the same, so an endpoint answering every field with a
        # bare `{"status": "success"}` -- a stub, a rewriting proxy -- would
        # publish all-`None`, count zero failures and look healthy forever while
        # every panel reads `--`. `data` must therefore be an object carrying a
        # `result` list; anything else is a malformed body and takes the counted
        # failure path below, like any other wrong shape.
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise ValueError("success envelope without a data.result list")

        results = data["result"]
        if not results:
            return None
        if spec.reduce == "top":
            return self._top(results, spec)
        # Kept, not changed: a query that normally yields one series may briefly
        # yield two (a node restarting under a `... or vector(0)`), and failing
        # the field there would be worse than reading it. But Prometheus does not
        # order instant vectors, so under `scalar` the panel shows an arbitrary
        # node's number and which one can change between polls -- the only place
        # in this file where a plausible-looking *wrong* number reaches the
        # glass. It gets a journal line naming the field so a missing `avg()`
        # is findable.
        if len(results) > 1:
            log.warning(
                "scalar reduce over a multi-series vector; publishing the first series",
                extra={"integration": self.name, "field": name, "series": len(results)},
            )
        return _sample(results[0]["value"][1])

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


def _sample(raw: Any) -> float | None:
    """A sample as a float, or `None` when Prometheus sent a non-finite one.

    Raises when `raw` is not a number at all. The two are not the same event:
    `"NaN"` / `"+Inf"` are values Prometheus really sends for a division by zero
    or an empty rate window, so they are a missing *reading* and the field is
    simply blank; `"not-a-number"` is a body Prometheus cannot produce, so it is
    a malformed response and has to count toward the all-fields-failed rule.
    """
    value = float(raw)  # TypeError / ValueError => malformed, and counted
    return value if math.isfinite(value) else None


def _number(raw: Any) -> float | None:
    """`_sample`, but lenient -- for `_top`, which skips a bad series rather than
    letting one poison the ranking of the ones that are fine."""
    try:
        return _sample(raw)
    except (TypeError, ValueError):
        return None


def _strip(label: str, mode: str) -> str:
    if mode != "last_octet":
        return label
    host = _host(label)
    # The mode is opt-in and named for IPv4, so it shortens only what really is
    # one. `host.example.com` used to become `.com`, which renders every
    # DNS-named node identically and makes the peak-node hint useless; `[fe80::1]`
    # became `[fe80`. A full hostname fits a 240px panel worse than a wrong one
    # does, but it is at least distinguishable from its neighbour.
    if not _is_ipv4(host):
        return host
    return f".{host.rsplit('.', 1)[1]}"


def _host(label: str) -> str:
    """`label` without a trailing `:port`, and without mangling anything else."""
    if label.startswith("[") and "]:" in label:  # [fe80::1]:9100
        return label[: label.index("]:") + 1]
    head, sep, tail = label.rpartition(":")
    # One colon and digits after it is a port. More than one and it is a bare
    # IPv6 address, whose last group is also digits -- `2001:db8::5` must not
    # lose its `5`.
    if sep and head and ":" not in head and tail.isdigit():
        return head
    return label


def _is_ipv4(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(
        part.isascii() and part.isdigit() and int(part) <= 255 for part in parts
    )
