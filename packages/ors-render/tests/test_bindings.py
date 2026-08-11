import json

import pytest
from ors_render.bindings import FILTERS, resolve, resolve_list, resolve_number, resolve_text

DATA = {
    "prom": {
        "cpu": 42.4,
        "mem_used": 19.4,
        "mem_total": 32.0,
        "hot": {"node": ".5", "value": 71.2},
    },
    "qbit": {
        "active": [{"name": "a-very-long-torrent-name", "progress": 91.2}],
        "speed": 4613734,
        "eta": 1112,
    },
    "params": {"title": "CPU"},
}


def test_whole_string_binding_returns_the_raw_value():
    assert resolve("{{prom.cpu}}", DATA) == 42.4
    assert resolve("{{qbit.active}}", DATA) == DATA["qbit"]["active"]


def test_mixed_text_interpolates_to_a_string():
    assert resolve("{{prom.cpu | round:0}}%", DATA) == "42%"
    peak = resolve_text("peak: {{prom.hot.node}} {{prom.hot.value | round:0}}%", DATA)
    assert peak == "peak: .5 71%"


def test_literal_values_pass_through():
    assert resolve("cluster avg", DATA) == "cluster avg"
    assert resolve(0.875, DATA) == 0.875


def test_missing_field_renders_empty_unless_default_given():
    assert resolve_text("{{prom.nope}}", DATA) == ""
    assert resolve_text("{{prom.nope | default:--}}", DATA) == "--"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("{{qbit.speed | bytes}}", "4.4 MB"),
        ("{{qbit.eta | duration}}", "18m"),
        ("{{prom.cpu | round:1}}", "42.4"),
        ("{{qbit.active[0].name | trunc:10}}", "a-very-lo."),
        ("{{params.title | lower}}", "cpu"),
    ],
)
def test_filters(spec, expected):
    assert resolve_text(spec, DATA) == expected


def test_resolve_number_coerces_and_falls_back():
    assert resolve_number("{{prom.cpu}}", DATA) == 42.4
    assert resolve_number(12, DATA) == 12.0
    assert resolve_number("{{prom.nope}}", DATA, default=-1.0) == -1.0
    assert resolve_number("not a number", DATA, default=7.0) == 7.0


def test_resolve_number_rejects_non_finite_values():
    # Prometheus emits NaN, and float() happily parses "nan"/"inf"/"-inf". A
    # non-finite number would propagate silently into sweep angles and
    # positions as garbage geometry, so it must degrade to the default.
    assert resolve_number("nan", DATA, default=-1.0) == -1.0
    assert resolve_number("NaN", DATA, default=-1.0) == -1.0
    assert resolve_number("inf", DATA, default=-1.0) == -1.0
    assert resolve_number("-inf", DATA, default=-1.0) == -1.0
    assert resolve_number(float("nan"), DATA, default=-1.0) == -1.0
    assert resolve_number(float("inf"), DATA, default=-1.0) == -1.0
    # Ordinary numbers are untouched.
    assert resolve_number("42.4", DATA, default=-1.0) == 42.4
    assert resolve_number("{{prom.cpu}}", DATA, default=-1.0) == 42.4
    assert resolve_number("{{prom.mem_total}}", DATA, default=-1.0) == 32.0
    assert resolve_number(0, DATA, default=-1.0) == 0.0


def test_resolve_number_coerces_a_bool_like_a_number():
    assert resolve_number("{{prom.cpu > 40}}", DATA, default=-1.0) == 1.0
    assert resolve_number("{{prom.cpu < 40}}", DATA, default=-1.0) == 0.0


def test_resolve_list_always_returns_a_list():
    assert resolve_list("{{qbit.active}}", DATA) == DATA["qbit"]["active"]
    assert resolve_list("{{prom.nope}}", DATA) == []
    assert resolve_list("{{prom.cpu}}", DATA) == []
    assert resolve_list("{{prom.hot}}", DATA) == []  # a mapping is not a list


def test_bad_expression_inside_binding_renders_empty_not_raises():
    assert resolve_text("{{__import__('os')}}", DATA) == ""


def test_whole_string_binding_keeps_none_rather_than_stringifying_it():
    # The raw-value mode must not collapse a missing field to "": callers such
    # as resolve_list/resolve_number distinguish None from an empty string.
    assert resolve("{{prom.nope}}", DATA) is None
    assert resolve("  {{qbit.active}}  ", DATA) == DATA["qbit"]["active"]


def test_two_bindings_in_one_string_interpolate_independently():
    # A string that merely starts with "{{" and ends with "}}" is still mixed
    # text, not one whole-string binding.
    assert resolve("{{prom.cpu | round:0}}/{{prom.mem_total}}", DATA) == "42/32"
    assert resolve_text("{{prom.hot.node}} {{params.title}}", DATA) == ".5 CPU"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("{{prom.mem_total}} GB", "32 GB"),  # integral float loses the ".0"
        ("{{prom.cpu}} pct", "42.4 pct"),
        ("{{prom.cpu > 40}}", "true"),  # bools render lowercase, JSON-style
        ("{{prom.cpu < 40}}", "false"),
        ("[{{prom.nope}}]", "[]"),  # None renders as nothing at all
    ],
)
def test_interpolation_number_and_bool_formatting(spec, expected):
    assert resolve_text(spec, DATA) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "{{qbit.active | round:0}}",  # value is the wrong type for the filter
        "{{prom.cpu | round:abc}}",  # filter argument is not an int
        "{{prom.cpu | round:1,2,3}}",  # too many filter arguments
    ],
)
def test_filter_that_cannot_be_applied_renders_empty_not_raises(spec):
    assert resolve_text(spec, DATA) == ""


def test_unknown_filter_is_ignored_and_leaves_the_value_untouched():
    assert resolve_text("{{prom.cpu | nosuchfilter}}", DATA) == "42.4"


@pytest.mark.parametrize(
    ("name", "args", "value", "expected"),
    [
        ("bytes", (), 2147483648, "2.0 GB"),
        ("bytes", (), 2048, "2.0 KB"),
        ("bytes", (), 512, "512 B"),
        ("duration", (), 45, "45s"),
        ("duration", (), 7860, "2h 11m"),
        ("duration", (), -1, "inf"),
        ("duration", (), 864001, "inf"),
        ("pct", (), 91.2, "91%"),
        ("pct", ("1",), 91.24, "91.2%"),
        ("upper", (), "cpu", "CPU"),
        ("trunc", (), "short", "short"),
        ("round", ("-1",), 42.4, 40),
        ("default", (), "kept", "kept"),
    ],
)
def test_each_filter_formats_its_own_units(name, args, value, expected):
    assert FILTERS[name](value, *args) == expected


@pytest.mark.parametrize("limit", ["0", "-5", "1", "3", "12"])
def test_trunc_never_exceeds_its_limit(limit):
    # The scene layout budgets a width from this limit, so overshooting it is
    # silent overflow rather than a visible error.
    text = resolve_text(f"{{{{qbit.active[0].name | trunc:{limit}}}}}", DATA)
    assert len(text) <= max(int(limit), 0)


def test_trunc_at_or_below_zero_yields_nothing():
    assert FILTERS["trunc"]("42.4", "0") == ""
    assert FILTERS["trunc"]("42.4", "-5") == ""
    assert resolve_text("{{prom.cpu | trunc:0}}", DATA) == ""
    assert resolve_text("{{prom.cpu | trunc:-5}}", DATA) == ""


@pytest.mark.parametrize("name", [n for n in sorted(FILTERS) if n != "default"])
def test_every_filter_but_default_passes_a_missing_value_through(name):
    # Only `default` is allowed to turn None into something; the rest must stay
    # None so a later `| default:--` in the same chain can still fire.
    assert FILTERS[name](None) is None


# `+Inf` is what Prometheus emits for a counter that has not been scraped long
# enough to have a rate, and `json.loads` parses `Infinity` without complaint, so
# a non-finite value in `data` is ordinary upstream input rather than something a
# caller had to construct by hand. `OverflowError` is neither a `TypeError` nor a
# `ValueError` -- it derives from `ArithmeticError` -- so it used to walk straight
# out of the filter guard and take the whole panel down.
INFINITE = json.loads('{"x": Infinity, "neg": -Infinity, "nan": NaN}')

# 400 digits: a JSON integer larger than any float, which `float()` refuses with
# `OverflowError: int too large to convert to float` rather than by returning
# `inf`. Also reachable straight from a feed.
HUGE_INT = json.loads('{"n": ' + "9" * 400 + "}")


@pytest.mark.parametrize(
    "spec",
    [
        # No data reference at all: the arithmetic is in the scene's own source.
        "{{1e308 + 1e308 | round:0}}",
        "{{-1e308 - 1e308 | round:0}}",
    ],
)
def test_arithmetic_that_overflows_in_the_scene_source_renders_empty(spec):
    assert resolve_text(spec, DATA) == ""


@pytest.mark.parametrize("field", ["x", "neg", "nan"])
@pytest.mark.parametrize("filter_spec", ["round:0", "round:-3"])
def test_a_non_finite_reading_cannot_be_rounded_and_renders_empty(field, filter_spec):
    # `round` returns an `int` at zero or fewer digits, and no infinity or NaN
    # has one -- so there is no number to show and the field goes blank, exactly
    # as a missing one does.
    assert resolve_text(f"{{{{{field} | {filter_spec}}}}}", INFINITE) == ""


@pytest.mark.parametrize("field", ["x", "neg", "nan"])
@pytest.mark.parametrize("filter_spec", ["pct", "pct:2", "bytes", "round:1"])
def test_a_non_finite_reading_never_raises_out_of_a_filter(field, filter_spec):
    # The formatting filters have a float representation for these ("inf%",
    # "nan GB"), which is not pretty but is a reading the panel can show and is
    # not this fix's business to change. What matters is that none of them
    # raises.
    assert isinstance(resolve_text(f"{{{{{field} | {filter_spec}}}}}", INFINITE), str)


@pytest.mark.parametrize("filter_spec", ["round:0", "round:-3", "pct", "bytes", "duration"])
def test_an_integer_too_large_for_a_float_degrades_through_every_filter(filter_spec):
    assert resolve_text(f"{{{{n | {filter_spec}}}}}", HUGE_INT) == ""


def test_resolve_number_falls_back_for_a_number_no_float_can_hold():
    # `ring.value` and every other `NumberSpec` field goes through here, so this
    # is the path a 400-digit integer takes to the geometry.
    assert resolve_number("{{n}}", HUGE_INT, default=-1.0) == -1.0


def test_duration_reports_an_infinite_eta_as_inf():
    # `torrent.json` draws `{{qbit.min_eta | duration}}`, and a torrent with no
    # progress has an infinite ETA. The out-of-range branch was written for
    # exactly this reading but sat *after* an `int()` that raised on it first.
    assert FILTERS["duration"](float("inf")) == "inf"
    assert FILTERS["duration"](float("-inf")) == "inf"
    assert resolve_text("{{x | duration}}", INFINITE) == "inf"
    assert resolve_text("{{neg | duration}}", INFINITE) == "inf"


def test_duration_reports_a_reading_that_is_not_a_number_as_nothing():
    # NaN is not "beyond the horizon", it is no reading at all, so it degrades to
    # empty like any other unusable value rather than claiming an infinite ETA.
    assert FILTERS["duration"](float("nan")) is None
    assert resolve_text("{{nan | duration}}", INFINITE) == ""


class _Hostile:
    """A data value whose every dunder raises, as a stand-in for a broken feed."""

    def __str__(self) -> str:
        raise RuntimeError("boom")

    def __float__(self) -> float:
        raise RuntimeError("boom")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("boom")

    def __len__(self) -> int:
        raise RuntimeError("boom")

    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "spec",
    [
        "{{bad | upper}}",  # _stringify -> str()
        "{{bad | lower}}",
        "{{bad | round:0}}",  # float()
        "{{bad | pct}}",
        "{{bad | bytes}}",
        "{{bad | duration}}",
        "{{bad | trunc:4}}",  # str() then len()
        "{{bad | default:--}}",  # == ""
    ],
)
def test_a_value_whose_dunders_raise_degrades_rather_than_taking_the_panel_down(spec):
    # `_apply`'s guard is a *trust boundary*, not a list of the exceptions seen so
    # far: both the value and the filter arguments are untrusted, which is the
    # same argument `ors_render.expr.evaluate` makes when it catches `Exception`.
    # Enumerating exception types is what let `OverflowError` through.
    assert resolve_text(spec, {"bad": _Hostile()}) == ""
