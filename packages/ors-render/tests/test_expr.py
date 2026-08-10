import pytest
from ors_render.expr import ExpressionError, evaluate, truthy

DATA = {
    "prom": {"cpu": 42.5, "nodes_ready": 3, "nodes_total": 3, "alerts": 0, "hot": None},
    "qbit": {"active": [{"progress": 91.0}, {"progress": 12.0}], "speed": 4400},
}


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1 + 2", 3),
        ("prom.cpu", 42.5),
        ("prom.cpu > 40", True),
        ("prom.nodes_ready == prom.nodes_total and prom.alerts == 0", True),
        ("not (prom.alerts > 0)", True),
        ("len(qbit.active)", 2),
        ("len(qbit.active) > 0", True),
        ("qbit.active[0].progress", 91.0),
        ("round(prom.cpu)", 42),
        ("max(1, 5, 3)", 5),
        ("prom.hot == null", True),
        ("'a' in 'abc'", True),
        ("prom.cpu * 2 - 5", 80.0),
    ],
)
def test_evaluates_supported_expressions(expr, expected):
    assert evaluate(expr, DATA) == expected


def test_missing_field_returns_none_rather_than_raising():
    assert evaluate("prom.nope", DATA) is None
    assert evaluate("nope.nope", DATA) is None


def test_truthy_treats_none_expression_as_true():
    assert truthy(None, DATA) is True
    assert truthy("prom.alerts == 0", DATA) is True
    assert truthy("prom.alerts > 0", DATA) is False


@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__",
        "prom.__class__",
        "open('/etc/passwd')",
        "[x for x in range(10)]",
        "lambda: 1",
        "prom.cpu if True else 0",
        "exec('x=1')",
        "globals()",
        "prom._secret",
        "{'a': 1}",
        "f'{prom.cpu}'",
    ],
)
def test_hostile_expressions_are_rejected_at_parse_time(hostile):
    with pytest.raises(ExpressionError):
        evaluate(hostile, DATA)


def test_syntax_error_becomes_expression_error():
    with pytest.raises(ExpressionError):
        evaluate("1 +", DATA)


def test_division_by_zero_becomes_expression_error():
    with pytest.raises(ExpressionError):
        evaluate("1 / 0", DATA)
