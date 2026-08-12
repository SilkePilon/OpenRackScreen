import contextlib
import json
import logging

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


@contextlib.contextmanager
def recorded_warnings():
    """Capture this module's warnings without relying on propagation.

    `setup_logging` sets `propagate = False` on the `ors_daemon` logger, so
    `caplog` -- which listens on the root -- sees nothing once any other test has
    installed the daemon's handler. Attaching straight to the module's logger is
    order-independent.
    """
    logger = logging.getLogger("ors_daemon.integrations.prometheus")
    records: list[logging.LogRecord] = []

    class Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Recorder(level=logging.WARNING)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


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
        pytest.param(FakeResponse({"status": "success", "data": None}), id="null_data"),
        pytest.param(FakeResponse({"status": "success", "data": []}), id="data_is_a_list"),
        pytest.param(
            FakeResponse({"status": "success", "data": {"resultType": "vector"}}),
            id="no_result_key",
        ),
        pytest.param(
            FakeResponse({"status": "success", "data": {"result": {}}}),
            id="result_is_not_a_list",
        ),
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

    # Nulled is only half the contract: the field must also be *counted*, or an
    # endpoint answering every field this way reports healthy forever while every
    # panel reads `--`. Paired with a healthy sibling the two are indistinguishable,
    # so the same body is polled again as the only field, where the all-fields-failed
    # rule is the counter's one observable consequence.
    alone = PrometheusIntegration(config(bad={"query": "bad"}), session=FakeSession({"bad": bad}))

    with pytest.raises(IntegrationError, match="every field failed"):
        alone.poll()


def test_a_success_envelope_with_no_data_is_a_failure_not_an_empty_query():
    # The whole endpoint answering `{"status": "success"}` -- an ingress stub, a
    # Prometheus behind a rewriting proxy -- is a dead source, not eleven queries
    # that each matched nothing. `data.result` absent and `data.result == []` are
    # different events and only the second one is healthy.
    body = FakeResponse({"status": "success"})
    session = FakeSession({"a": body, "b": body})
    integration = PrometheusIntegration(config(a={"query": "a"}, b={"query": "b"}), session=session)

    with pytest.raises(IntegrationError, match="every field failed"):
        integration.poll()


def test_an_empty_result_list_is_still_not_a_failure():
    # The other side of the line above: `result: []` is a query that legitimately
    # matched nothing, and it must not push the integration toward unhealthy.
    empty = FakeResponse({"status": "success", "data": {"resultType": "vector", "result": []}})
    session = FakeSession({"a": empty, "b": empty})
    integration = PrometheusIntegration(config(a={"query": "a"}, b={"query": "b"}), session=session)

    assert integration.poll() == {"a": None, "b": None}


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


def test_close_swallows_a_failing_shutdown_and_still_frees_the_session(monkeypatch):
    # The contract says `close()` raises nothing: it runs on shutdown and on the
    # way out of a failed `open()`, where a second exception would mask the first.
    class Stubborn(OwnedSession):
        def close(self) -> None:
            raise OSError("connection reset during shutdown")

    sessions: list[OwnedSession] = []

    def factory() -> OwnedSession:
        sessions.append(Stubborn())
        return sessions[-1]

    monkeypatch.setattr(requests, "Session", factory)
    integration = PrometheusIntegration(config(cpu={"query": "up"}))
    integration.open()

    integration.close()  # must not raise

    assert integration.poll() == {"cpu": 1.0}
    assert len(sessions) == 2  # the unclosable one was dropped, not reused


# --- Resolving the base URL is itself a failure path ------------------------
#
# `UrlProvider` is public API with no "must not raise" clause, and the only
# reason it is a callable at all is that the URL may be unavailable at that
# instant -- a `kubectl port-forward` that died between polls. So every way of
# failing to produce a base URL has to arrive as `IntegrationError`, which is
# the one exception `poll()` is allowed to raise.


def test_a_url_provider_that_raises_becomes_an_integration_error(monkeypatch):
    def dead_tunnel() -> str:
        raise RuntimeError("tunnel is down")

    def forbidden() -> None:
        raise AssertionError("must not build a session for a URL we could not resolve")

    monkeypatch.setattr(requests, "Session", forbidden)
    integration = PrometheusIntegration(config(cpu={"query": "up"}), url_provider=dead_tunnel)

    with pytest.raises(IntegrationError, match="tunnel is down"):
        integration.poll()


@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("/", id="bare_slash"),
        pytest.param("http://", id="scheme_only"),
        pytest.param(19090, id="not_a_string"),
        pytest.param(b"http://prom:9090", id="bytes"),
        pytest.param("prom:9090", id="no_scheme"),
    ],
)
def test_a_url_provider_returning_an_unusable_base_url_becomes_an_integration_error(supplied):
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(
        config(cpu={"query": "up"}), url_provider=lambda: supplied, session=session
    )

    with pytest.raises(IntegrationError, match="base URL"):
        integration.poll()

    assert session.calls == []


def test_an_unusable_configured_url_becomes_an_integration_error_too():
    # Same gate, other source: with no provider the config's `url` goes through
    # the identical check, so a blank `url:` in the YAML is a loud poll failure
    # rather than eleven requests to `/api/v1/query`.
    session = FakeSession({"up": scalar("1.0")})
    integration = PrometheusIntegration(
        PrometheusConfig(name="prom", url="", fields={"cpu": {"query": "up"}}), session=session
    )

    with pytest.raises(IntegrationError, match="base URL"):
        integration.poll()

    assert session.calls == []


def test_a_session_that_cannot_be_built_still_leaves_poll_raising_integration_error(monkeypatch):
    # `open()` may raise `IntegrationError` by contract, and `poll()` calls it --
    # so whatever it throws (here: descriptor exhaustion) must be converted, or
    # poll's one-exception guarantee has a hole in its very first statement.
    def exhausted() -> None:
        raise OSError("too many open files")

    monkeypatch.setattr(requests, "Session", exhausted)
    integration = PrometheusIntegration(config(cpu={"query": "up"}))

    with pytest.raises(IntegrationError, match="too many open files"):
        integration.poll()


# --- `reduce: scalar` over a vector that is not one series ------------------


def test_a_scalar_reduce_over_a_multi_series_vector_warns_which_field_and_how_many():
    # Prometheus does not guarantee instant-vector ordering, so a query written
    # without an aggregation puts an arbitrary node's number on the panel and
    # which node it is can change between polls. The behaviour stays -- a
    # legitimately-single-series query that occasionally returns two must not
    # start failing -- but it stops being silent.
    session = FakeSession({"q": vector(("192.168.1.5:9100", "71.2"), ("192.168.1.7:9100", "12.0"))})
    integration = PrometheusIntegration(config(cpu={"query": "q"}), session=session)

    with recorded_warnings() as records:
        assert integration.poll() == {"cpu": 71.2}

    assert len(records) == 1
    assert records[0].field == "cpu"
    assert records[0].series == 2
    assert records[0].integration == "prom"


def test_a_single_series_vector_and_a_top_reduction_warn_about_nothing():
    session = FakeSession(
        {
            "one": scalar("1.0"),
            "many": vector(("a:9100", "1.0"), ("b:9100", "2.0")),
            "none": FakeResponse({"status": "success", "data": {"result": []}}),
        }
    )
    integration = PrometheusIntegration(
        config(
            one={"query": "one"},
            hot={"query": "many", "reduce": "top"},
            empty={"query": "none"},
        ),
        session=session,
    )

    with recorded_warnings() as records:
        integration.poll()

    assert records == []


# --- `strip: last_octet` is named for IPv4 and now only fires on IPv4 -------


@pytest.mark.parametrize(
    "label,expected",
    [
        pytest.param("192.168.1.5:9100", ".5", id="ipv4_with_port"),
        pytest.param("10.0.0.100", ".100", id="ipv4_bare"),
        pytest.param("worker-01:9100", "worker-01", id="short_hostname"),
        pytest.param("host.example.com:9100", "host.example.com", id="dns_name"),
        pytest.param("[fe80::1]:9100", "[fe80::1]", id="ipv6_bracketed_with_port"),
        pytest.param("2001:db8::5", "2001:db8::5", id="ipv6_bare"),
        pytest.param("192.168.1.256:9100", "192.168.1.256", id="not_really_an_octet"),
    ],
)
def test_strip_last_octet_shortens_only_what_is_really_an_ipv4_address(label, expected):
    # `host.example.com` used to render as `.com`, which makes every DNS-named
    # node identical on the glass -- the peak-node hint then says nothing at all.
    # A full hostname overflows a 240px panel, but an overflowing label is still
    # distinguishable from its neighbour and a wrong one is not.
    session = FakeSession({"q": vector((label, "71.2"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top", "label": "instance", "strip": "last_octet"}),
        session=session,
    )

    assert integration.poll() == {"hot": {"node": expected, "value": 71.2}}


# --- `_top`, at its edges ---------------------------------------------------


def test_a_top_reduction_breaks_a_tie_in_favour_of_the_first_series():
    # Strictly `>`, so equal series do not displace the incumbent. Which node an
    # exact tie names is arbitrary either way; pinned so a later refactor to `>=`
    # is a test failure rather than a silently different panel.
    session = FakeSession({"q": vector(("192.168.1.5:9100", "50.0"), ("192.168.1.7:9100", "50.0"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "192.168.1.5:9100", "value": 50.0}}


def test_a_top_reduction_over_all_negative_values_still_picks_the_highest():
    # `best_value is None` rather than a falsy check, so -1.0 is a real reading
    # and 0.0 does not read as "nothing seen yet".
    session = FakeSession({"q": vector(("a:9100", "-9.0"), ("b:9100", "-1.5"), ("c:9100", "-4.0"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "b:9100", "value": -1.5}}


def test_a_top_series_missing_the_configured_label_publishes_an_empty_node():
    # Decision, pinned rather than left emergent: the label is a *hint* and the
    # value is the reading, so a series whose `metric` has no `instance` still
    # publishes its number and names nobody. `peak: {{node}} {{value}}%` then
    # renders `peak:  71%` -- a cosmetic double space -- which is honest about
    # not knowing the node. Substituting `?` or `unknown` would put a fabricated
    # name exactly where a real label goes, and could collide with one.
    session = FakeSession({"q": vector(("192.168.1.5:9100", "71.2"))})
    integration = PrometheusIntegration(
        config(hot={"query": "q", "reduce": "top", "label": "node"}), session=session
    )

    assert integration.poll() == {"hot": {"node": "", "value": 71.2}}
