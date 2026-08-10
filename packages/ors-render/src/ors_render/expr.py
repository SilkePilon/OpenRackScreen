from __future__ import annotations

import ast
import operator
from collections.abc import Mapping, Sequence
from typing import Any


class ExpressionError(Exception):
    """Raised for any expression that is malformed or not on the allow-list."""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
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
    "round": round,
    "int": int,
    "float": float,
    "str": str,
}

_LITERALS = {"null": None, "true": True, "false": False}


def evaluate(expr: str, data: Mapping[str, Any]) -> Any:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error in {expr!r}: {exc}") from exc
    try:
        return _eval(tree.body, data)
    except ExpressionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any runtime failure is an expression failure
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
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ExpressionError("attribute access is only allowed on mappings")
        return value.get(node.attr)

    if isinstance(node, ast.Subscript):
        value = _eval(node.value, data)
        key = _eval(node.slice, data)
        if value is None:
            return None
        if isinstance(value, Mapping):
            return value.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str) and isinstance(key, int):
            return value[key] if -len(value) <= key < len(value) else None
        raise ExpressionError("subscript is only allowed on mappings and lists")

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
        values = [_eval(v, data) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

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
