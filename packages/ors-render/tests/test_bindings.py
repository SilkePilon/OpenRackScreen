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


def test_resolve_list_always_returns_a_list():
    assert resolve_list("{{qbit.active}}", DATA) == DATA["qbit"]["active"]
    assert resolve_list("{{prom.nope}}", DATA) == []
    assert resolve_list("{{prom.cpu}}", DATA) == []


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


@pytest.mark.parametrize("name", [n for n in sorted(FILTERS) if n != "default"])
def test_every_filter_but_default_passes_a_missing_value_through(name):
    # Only `default` is allowed to turn None into something; the rest must stay
    # None so a later `| default:--` in the same chain can still fire.
    assert FILTERS[name](None) is None
