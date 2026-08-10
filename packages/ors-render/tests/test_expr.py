import time

import pytest
from ors_render.expr import MAX_EXPRESSION_LENGTH, ExpressionError, evaluate, truthy

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


# --- Finding 1: non-ExpressionError exceptions must never escape evaluate() ---


def test_deeply_chained_arithmetic_source_becomes_expression_error():
    # ~20 KB source that blows the ast parser's recursion depth.
    hostile = "1" + "+1" * 10000
    with pytest.raises(ExpressionError):
        evaluate(hostile, DATA)


def test_long_unary_chain_source_becomes_expression_error():
    # ~10 KB of unary minus that overflows the ast parser's C stack.
    hostile = "-" * 10000 + "1"
    with pytest.raises(ExpressionError):
        evaluate(hostile, DATA)


def test_nul_byte_in_source_becomes_expression_error():
    # On Python 3.11, ast.parse raises ValueError (not SyntaxError) for NUL bytes.
    with pytest.raises(ExpressionError):
        evaluate("1\x00+1", DATA)


def test_source_length_cap_is_enforced():
    hostile = "1" + " + 1" * (MAX_EXPRESSION_LENGTH // 2)
    assert len(hostile) > MAX_EXPRESSION_LENGTH
    with pytest.raises(ExpressionError):
        evaluate(hostile, DATA)


# --- Finding 2: expression source alone must not cause unbounded allocation ---


def test_string_multiplication_is_rejected():
    with pytest.raises(ExpressionError):
        evaluate("'a' * 800000000", DATA)


def test_list_multiplication_is_rejected():
    with pytest.raises(ExpressionError):
        evaluate("[0, 0] * 10000000", DATA)


def test_string_modulo_formatting_is_rejected():
    with pytest.raises(ExpressionError):
        evaluate("'%.200000000f' % 1.5", DATA)


def test_numeric_multiplication_and_modulo_still_work():
    assert evaluate("6 * 7", DATA) == 42
    assert evaluate("7 % 3", DATA) == 1


# --- Finding 3: BoolOp must short-circuit ---


def test_and_short_circuits_without_evaluating_right_operand():
    assert evaluate("prom.alerts > 0 and 1 / 0", DATA) is False


def test_or_short_circuits_without_evaluating_right_operand():
    assert evaluate("prom.alerts == 0 or 1 / 0", DATA) is True


def test_bool_op_returns_bool_not_operand():
    # This expression language deliberately normalizes BoolOp results to bool,
    # consistent with Compare (which already returns True/False rather than
    # chaining operands per Python semantics). See task-4-report.md for details.
    assert evaluate("1 and 2", DATA) is True
    assert evaluate("0 or 5", DATA) is True
    assert evaluate("0 and 5", DATA) is False
    assert evaluate("0 or 0", DATA) is False


# --- Finding 5: shape mismatches on data degrade to None, not ExpressionError ---


def test_attribute_access_on_non_mapping_returns_none():
    assert evaluate("prom.cpu.foo", DATA) is None


def test_subscript_on_non_mapping_non_list_returns_none():
    assert evaluate("prom.cpu[0]", DATA) is None


# --- Coverage gaps named in review ---


def test_none_comparison_gt_is_false():
    assert evaluate("prom.nope > 5", DATA) is False


def test_none_comparison_ne_is_true():
    assert evaluate("prom.nope != 5", DATA) is True


def test_in_with_none_right_operand():
    assert evaluate("5 in prom.nope", DATA) is False


def test_not_in_with_none_right_operand():
    assert evaluate("5 not in prom.nope", DATA) is True


def test_truthy_on_missing_field_is_false():
    assert truthy("prom.nope", DATA) is False


def test_truthy_on_disallowed_construct_raises():
    with pytest.raises(ExpressionError):
        truthy("__import__('os')", DATA)


# --- Fix round 2, Finding 1: round() with an out-of-range ndigits is an
# unbounded allocator (round(1, -N) computes 10**N internally) and must be
# rejected promptly instead of burning CPU/memory or hanging forever.


@pytest.mark.parametrize(
    "expr",
    [
        "round(1, -1000000)",
        "round(1, -10000000)",
        "round(1, -99999999999999999999)",
        "round(1, 1000000)",
    ],
)
def test_round_with_out_of_range_ndigits_is_rejected(expr):
    start = time.monotonic()
    with pytest.raises(ExpressionError):
        evaluate(expr, DATA)
    # A regression back to the unbounded path would take seconds (or hang
    # forever) for these inputs; the guard must reject before ever calling
    # the real `round`.
    assert time.monotonic() - start < 1.0


def test_round_still_works_for_ordinary_arguments():
    assert evaluate("round(3.14159, 2)", DATA) == 3.14
    assert evaluate("round(3.7)", DATA) == 4
    assert evaluate("round(prom.cpu)", DATA) == 42


# --- Fix round 2, Finding 2: evaluate() must raise ExpressionError, not
# TypeError, for a non-str expression (e.g. a scene JSON's `when` field that
# is a number, null, or a list because of malformed upstream data).


@pytest.mark.parametrize("bad_expr", [123, None, ["1+1"], b"1+1"])
def test_evaluate_rejects_non_string_expression(bad_expr):
    with pytest.raises(ExpressionError):
        evaluate(bad_expr, DATA)
