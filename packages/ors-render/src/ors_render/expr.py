from __future__ import annotations

import ast
import operator
from collections.abc import Mapping, Sequence
from typing import Any


class ExpressionError(Exception):
    """Raised for any expression that is malformed or not on the allow-list."""


# Real `when` expressions (attribute chains, comparisons, small boolean
# combinations) are well under a hundred characters. This cap is generous for
# that use case while sitting far below the size needed to stress the ast
# parser's recursion/stack limits (see test_deeply_chained_arithmetic_source_
# becomes_expression_error and test_long_unary_chain_source_becomes_
# expression_error).
MAX_EXPRESSION_LENGTH = 512

# `round(int, ndigits)` computes `10 ** abs(ndigits)` internally when rounding
# an int, so a tiny expression like `round(1, -99999999999999999999)` can burn
# unbounded CPU/memory (or never terminate) even though the source is a few
# bytes. A display-rendering language never needs to round outside a small
# range around the decimal point, so any `ndigits` magnitude beyond this is
# rejected rather than computed.
MAX_ROUND_NDIGITS = 100


def _round(*args: Any) -> Any:
    if len(args) >= 2:
        # The built-in `round` accepts any object with `__index__` as ndigits,
        # not just `int`, so gating this bound on `isinstance(ndigits, int)`
        # would let such an object walk straight into the unbounded
        # `10 ** abs(ndigits)` path. Convert the same way `round` does, and
        # treat "not an integer at all" (TypeError) as out of scope for the
        # bound - the real `round` then rejects it as it always has.
        try:
            ndigits = operator.index(args[1])
        except TypeError:
            ndigits = None
        if ndigits is not None and abs(ndigits) > MAX_ROUND_NDIGITS:
            raise ExpressionError(
                f"round() ndigits out of range (abs must be <= {MAX_ROUND_NDIGITS})"
            )
    return round(*args)


def _mul(left: Any, right: Any) -> Any:
    if isinstance(left, str | list | tuple) or isinstance(right, str | list | tuple):
        raise ExpressionError("multiplication of strings, lists, or tuples is not allowed")
    return operator.mul(left, right)


def _mod(left: Any, right: Any) -> Any:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        raise ExpressionError("modulo is only allowed on numbers")
    return operator.mod(left, right)


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: _mul,
    ast.Div: operator.truediv,
    ast.Mod: _mod,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_FUNCS = {
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "round": _round,
    "int": int,
    "float": float,
    "str": str,
}

_LITERALS = {"null": None, "true": True, "false": False}


def evaluate(expr: str, data: Mapping[str, Any]) -> Any:
    """Evaluate a sandboxed `when` expression against `data`.

    Raises `ExpressionError` and only `ExpressionError` for any malformed,
    disallowed, or otherwise unevaluable expression - including a non-`str`
    `expr`. A missing field (an absent key or an attribute/subscript on a
    shape that doesn't support it) evaluates to `None` rather than raising.

    Boolean operators (`and`/`or`) always return `bool`, not the deciding
    operand as in ordinary Python - e.g. `0 or 5` is `True`, not `5`. Callers
    that want the deciding-operand idiom (`qbit.speed or 0`) must not rely on
    this function for that; it is intentionally normalized for consistency
    with `Compare`, which likewise collapses chained comparisons to `bool`.
    """
    if not isinstance(expr, str):
        raise ExpressionError(f"expression must be a string, got {type(expr).__name__}")
    if len(expr) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"expression too long ({len(expr)} > {MAX_EXPRESSION_LENGTH} characters)"
        )
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error in {expr!r}: {exc}") from exc
    except (ValueError, RecursionError, MemoryError) as exc:
        # ValueError: e.g. a NUL byte in the source (raised instead of
        # SyntaxError on Python 3.11+). RecursionError/MemoryError: defense
        # in depth beyond the length cap above, in case some other input
        # shape stresses the parser's C stack.
        raise ExpressionError(f"failed to parse expression {expr!r}: {exc}") from exc
    try:
        return _eval(tree.body, data)
    except ExpressionError:
        raise
    except Exception as exc:  # any runtime failure is an expression failure
        raise ExpressionError(f"failed to evaluate {expr!r}: {exc}") from exc


def truthy(expr: str | None, data: Mapping[str, Any]) -> bool:
    if expr is None:
        return True
    return bool(evaluate(expr, data))


def _eval(node: ast.AST, data: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str | int | float | bool | None):
            return node.value
        raise ExpressionError(f"unsupported constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in _LITERALS:
            return _LITERALS[node.id]
        if node.id.startswith("_"):
            raise ExpressionError(f"name not allowed: {node.id}")
        return data.get(node.id)

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ExpressionError(f"attribute not allowed: {node.attr}")
        value = _eval(node.value, data)
        # A base that isn't a mapping (None, or a shape mismatch such as an
        # upstream field that changed from an object to a scalar) degrades to
        # None like any other missing field, rather than raising. No getattr
        # is ever performed here, so this cannot expose Python attributes.
        if not isinstance(value, Mapping):
            return None
        return value.get(node.attr)

    if isinstance(node, ast.Subscript):
        value = _eval(node.value, data)
        key = _eval(node.slice, data)
        if isinstance(value, Mapping):
            return value.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str) and isinstance(key, int):
            return value[key] if -len(value) <= key < len(value) else None
        # Same reasoning as Attribute above: a base that is neither a mapping
        # nor a list is a data shape mismatch, not a disallowed construct.
        return None

    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, data)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ExpressionError("unsupported unary operator")

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError("unsupported binary operator")
        return op(_eval(node.left, data), _eval(node.right, data))

    if isinstance(node, ast.BoolOp):
        # Short-circuit like Python: stop evaluating operands as soon as the
        # result is decided, so `qbit.speed != 0 and 100 / qbit.speed > 1`
        # doesn't evaluate the division when the guard is False. Unlike
        # Python, this returns a bool rather than the deciding operand -
        # consistent with Compare below, which already normalizes chained
        # comparisons to True/False instead of returning an operand.
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not _eval(value, data):
                    return False
            return True
        for value in node.values:
            if _eval(value, data):
                return True
        return False

    if isinstance(node, ast.Compare):
        left = _eval(node.left, data)
        for op_node, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval(right_node, data)
            if isinstance(op_node, ast.In):
                result = right is not None and left in right
            elif isinstance(op_node, ast.NotIn):
                result = right is None or left not in right
            else:
                op = _CMP_OPS.get(type(op_node))
                if op is None:
                    raise ExpressionError("unsupported comparison operator")
                if left is None or right is None:
                    result = op(left, right) if type(op_node) in (ast.Eq, ast.NotEq) else False
                else:
                    result = op(left, right)
            if not result:
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only direct calls to allow-listed functions are permitted")
        func = _FUNCS.get(node.func.id)
        if func is None:
            raise ExpressionError(f"function not allowed: {node.func.id}")
        if node.keywords:
            raise ExpressionError("keyword arguments are not supported")
        return func(*[_eval(a, data) for a in node.args])

    if isinstance(node, ast.List | ast.Tuple):
        return [_eval(e, data) for e in node.elts]

    raise ExpressionError(f"expression node not allowed: {type(node).__name__}")
