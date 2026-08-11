import json

import pytest
import requests
from ors_daemon.integrations import IntegrationError, build_integration
from ors_daemon.integrations.prometheus import PrometheusIntegration
from ors_schema.daemon import PrometheusConfig


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
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [0, value]}],
            },
        }
    )


def vector(*pairs):
    return FakeResponse(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"instance": instance}, "value": [0, value]}
                    for instance, value in pairs
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
    session = FakeSession(
        {"good": scalar("1.0"), "bad": FakeResponse(None, text="<html>502</html>")}
    )
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


# --- What Prometheus really puts on the wire -------------------------------
#
# Verified against the upstream HTTP API reference:
# "JSON does not support special float values such as NaN, Inf, and -Inf, so
# sample values are transferred as quoted JSON strings rather than raw numbers."
# The envelope is `{status, data, errorType, error, warnings, infos}`, and on an
# error "the data field may still hold additional data".


@pytest.mark.parametrize(
    "raw",
    ["NaN", "+Inf", "-Inf", float("nan"), float("inf")],
    ids=["quoted_nan", "quoted_pos_inf", "quoted_neg_inf", "bare_nan", "bare_inf"],
)
def test_a_non_finite_sample_is_dropped_rather_than_drawn(raw):
    # Quoted is what Prometheus sends; bare is what a lenient json decoder would
    # hand back if anything upstream re-encoded the body, and both must not reach
    # a panel as "nan" or "inf".
    session = FakeSession({"up": scalar(raw)})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    assert integration.poll() == {"cpu": None}


def test_a_top_reduction_skips_non_finite_series_instead_of_ranking_them():
    session = FakeSession({"q": vector(("192.168.1.5:9100", "NaN"), ("192.168.1.7:9100", "3.0"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "192.168.1.7:9100", "value": 3.0}}


def test_strip_last_octet_leaves_a_label_with_no_octets_alone():
    session = FakeSession({"q": vector(("worker-01:9100", "5.0"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top", "strip": "last_octet"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "worker-01", "value": 5.0}}


def test_a_status_error_body_publishes_nothing_even_when_it_carries_data():
    # A 422 with `errorType: execution` is Prometheus saying *this query* failed,
    # not that the server is down -- so the field degrades and its siblings live.
    # `status` is authoritative: the stale `data` below must not reach a panel.
    session = FakeSession(
        {
            "good": scalar("1.0"),
            "bad": FakeResponse(
                {
                    "status": "error",
                    "errorType": "execution",
                    "error": "query processing would load too many samples",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {}, "value": [0, "9.0"]}],
                    },
                },
                status_code=422,
            ),
        }
    )
    integration = PrometheusIntegration(
        config(good={"query": "good"}, bad={"query": "bad"}), session=session
    )

    assert integration.poll() == {"good": 1.0, "bad": None}


def test_every_query_erroring_raises_rather_than_publishing_all_nones():
    error = FakeResponse(
        {"status": "error", "errorType": "bad_data", "error": "invalid parameter"},
        status_code=400,
    )
    session = FakeSession({"a": error, "b": error})
    integration = PrometheusIntegration(config(a={"query": "a"}, b={"query": "b"}), session=session)

    with pytest.raises(IntegrationError, match="every field failed"):
        integration.poll()


@pytest.mark.parametrize(
    "bad",
    [
        # HTML has its own two tests above and below; these are the rest.
        pytest.param(FakeResponse(None, text=""), id="empty_body"),
        pytest.param(FakeResponse([]), id="json_array"),
        pytest.param(FakeResponse({"status": "success"}), id="no_data_key"),
        pytest.param(
            FakeResponse({"status": "success", "data": {"result": [{"metric": {}}]}}),
            id="series_without_a_value",
        ),
        pytest.param(
            FakeResponse(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[0, "1.0"]]}],
                    },
                }
            ),
            id="range_result",
        ),
        pytest.param(
            FakeResponse(
                {
                    "status": "success",
                    "data": {"result": [{"metric": {}, "value": [0, "not-a-number"]}]},
                }
            ),
            id="unparseable_value",
        ),
    ],
)
def test_a_malformed_body_degrades_only_its_own_field(bad):
    session = FakeSession({"good": scalar("1.0"), "bad": bad})
    integration = PrometheusIntegration(
        config(good={"query": "good"}, bad={"query": "bad"}), session=session
    )

    assert integration.poll() == {"good": 1.0, "bad": None}


def test_an_ingress_serving_html_to_every_field_raises():
    html = FakeResponse(None, text="<html>502 Bad Gateway</html>")
    session = FakeSession({"a": html, "b": html})
    integration = PrometheusIntegration(config(a={"query": "a"}, b={"query": "b"}), session=session)

    with pytest.raises(IntegrationError, match="every field failed"):
        integration.poll()


def test_a_lone_field_has_no_partial_mode_and_raises_on_its_own_failure():
    # Pinned deliberately: with one field "every field failed" degenerates to
    # "any field failed", so a single-field integration is all-or-nothing. See
    # the report -- surfacing this as unhealthy beats a panel reading "--"
    # forever, but the same fault in a two-field integration only nulls a field.
    session = FakeSession({"a": FakeResponse(None, text="<html>502</html>")})
    integration = PrometheusIntegration(config(a={"query": "a"}), session=session)

    with pytest.raises(IntegrationError, match="every field failed"):
        integration.poll()


# --- Session ownership and the tunnel's moving URL --------------------------


class OwnedSession(requests.Session):
    """A real `requests.Session` subclass, so the ownership check recognises it.

    Constructing one touches no socket; `get` is overridden so none is opened.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, float | None]] = []
        self.closed = False

    def get(self, url, params=None, timeout=None):  # type: ignore[override]
        self.calls.append((url, params["query"], timeout))
        return scalar("1.0")

    def close(self) -> None:
        self.closed = True


def test_open_is_idempotent_and_close_shuts_down_only_what_open_built(monkeypatch):
    sessions: list[OwnedSession] = []

    def factory() -> OwnedSession:
        sessions.append(OwnedSession())
        return sessions[-1]

    monkeypatch.setattr(requests, "Session", factory)
    integration = PrometheusIntegration(config(cpu={"query": "up"}))

    integration.open()
    integration.open()
    assert len(sessions) == 1

    integration.close()
    assert sessions[0].closed

    assert integration.poll() == {"cpu": 1.0}
    assert len(sessions) == 2


def test_close_does_not_discard_a_session_the_caller_supplied(monkeypatch):
    def forbidden() -> None:
        raise AssertionError("must not build a real session over an injected one")

    monkeypatch.setattr(requests, "Session", forbidden)
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(config(cpu={"query": "up"}), session=session)

    integration.close()

    assert integration.poll() == {"cpu": 1.0}


def test_the_url_provider_is_read_on_every_poll_so_a_tunnel_can_move():
    urls = iter(["http://localhost:19090", "http://localhost:19091"])
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(
        config(cpu={"query": "up"}), url_provider=lambda: next(urls), session=session
    )

    integration.poll()
    integration.poll()

    assert [call[0] for call in session.calls] == [
        "http://localhost:19090/api/v1/query",
        "http://localhost:19091/api/v1/query",
    ]


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(
        config(cpu={"query": "up"}), url_provider=lambda: "http://prom:9090/", session=session
    )
    integration.poll()

    assert session.calls[0][0] == "http://prom:9090/api/v1/query"


def test_build_integration_wires_the_url_provider_into_the_client(monkeypatch):
    session = FakeSession({"up": scalar("1.0")})
    monkeypatch.setattr(requests, "Session", lambda: session)
    integration = build_integration(
        config(cpu={"query": "up"}), url_provider=lambda: "http://tunnel:19090"
    )

    assert integration.poll() == {"cpu": 1.0}
    assert session.calls[0][0] == "http://tunnel:19090/api/v1/query"
