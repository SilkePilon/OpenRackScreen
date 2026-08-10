"""Resolve `{{ ... }}` bindings in scene fields against live data.

Two modes, kept deliberately distinct: a spec that is *entirely* one binding
resolves to the raw value (so `{{qbit.active}}` yields a list and `{{prom.cpu}}`
yields a float), while a spec with surrounding text interpolates every binding
into a string. Rendering must degrade rather than crash, so a binding that is
malformed, disallowed, or points at a missing field resolves to `None` - which
renders as the `default` filter's value, or empty.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ors_render.expr import ExpressionError, evaluate

# Lazy body, so the match stops at the first `}}` rather than swallowing the
# text between two adjacent bindings. Whole-string detection is done by
# checking this match's boundaries (see `_whole_binding`) rather than with a
# second, anchored pattern: anchoring `\s*$` after a lazy body lets the regex
# engine backtrack straight past the first `}}`, which would misread
# `{{a}}/{{b}}` as a single binding on the expression `a}}/{{b`.
_BINDING = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _f_round(value: Any, digits: str = "0") -> Any:
    if value is None:
        return None
    n = int(digits)
    result = round(float(value), n)
    return int(result) if n <= 0 else result


def _f_bytes(value: Any) -> Any:
    if value is None:
        return None
    size = float(value)
    for unit, threshold in (("GB", 1073741824), ("MB", 1048576), ("KB", 1024)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{int(size)} B"


def _f_duration(value: Any) -> Any:
    if value is None:
        return None
    seconds = int(value)
    if seconds < 0 or seconds > 864000:
        return "inf"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _f_pct(value: Any, digits: str = "0") -> Any:
    if value is None:
        return None
    return f"{float(value):.{int(digits)}f}%"


def _f_trunc(value: Any, length: str = "12") -> Any:
    if value is None:
        return None
    text = str(value)
    limit = int(length)
    if len(text) <= limit:
        return text
    # The limit is a width the scene layout budgets for, so the result must
    # never exceed it: at a limit of 1 only the ellipsis fits, and at zero or
    # below nothing does. `limit - 1` alone would overflow for those.
    if limit <= 0:
        return ""
    return text[: limit - 1] + "."


def _f_default(value: Any, fallback: str = "") -> Any:
    return fallback if value is None or value == "" else value


FILTERS: dict[str, Callable[..., Any]] = {
    "round": _f_round,
    "bytes": _f_bytes,
    "duration": _f_duration,
    "pct": _f_pct,
    "trunc": _f_trunc,
    "default": _f_default,
    "upper": lambda v: None if v is None else str(v).upper(),
    "lower": lambda v: None if v is None else str(v).lower(),
}


def _apply(inner: str, data: Mapping[str, Any]) -> Any:
    """Evaluate one binding body: an expression followed by `| filter:args`."""
    parts = [p.strip() for p in inner.split("|")]
    expression, filters = parts[0], parts[1:]
    try:
        value: Any = evaluate(expression, data)
    except ExpressionError:
        # The expression comes from user-authored scene JSON, so "malformed" or
        # "not on the allow-list" is bad input, not a bug here. `evaluate`
        # raises nothing else, and a missing field is already `None`.
        value = None
    for spec in filters:
        name, _, raw_args = spec.partition(":")
        func = FILTERS.get(name.strip())
        if func is None:
            continue
        args = [a.strip() for a in raw_args.split(",")] if raw_args else []
        try:
            value = func(value, *args)
        except (TypeError, ValueError):
            # Both inputs to a filter are untrusted: the value comes from live
            # upstream data whose shape can change, and the arguments are raw
            # text from the scene. So a wrong-type value (`{{list | round:0}}`),
            # an unparseable argument (`round:abc`) and a wrong arity
            # (`round:1,2,3`) are all bad input and degrade to empty. This does
            # mean a genuine TypeError/ValueError bug inside a filter would
            # render blank instead of crashing; that is accepted here because
            # rendering must never take the screen down, and each filter's own
            # behaviour is pinned by tests.
            value = None
    return value


def _whole_binding(spec: str) -> str | None:
    """Return the body if `spec` is one binding plus optional surrounding space."""
    match = _BINDING.search(spec)
    if match is None:
        return None
    if spec[: match.start()].strip() or spec[match.end() :].strip():
        return None
    return match.group(1)


def resolve(spec: Any, data: Mapping[str, Any]) -> Any:
    """Resolve a scene field: raw value for a whole-string binding, else text.

    A container returned by the raw-value mode is the live object held in
    `data`, not a copy - callers must not mutate it in place.
    """
    if not isinstance(spec, str):
        return spec
    whole = _whole_binding(spec)
    if whole is not None:
        return _apply(whole, data)
    if "{{" not in spec:
        return spec
    return _BINDING.sub(lambda m: _stringify(_apply(m.group(1), data)), spec)


def resolve_text(spec: Any, data: Mapping[str, Any]) -> str:
    return _stringify(resolve(spec, data))


def resolve_number(spec: Any, data: Mapping[str, Any], default: float = 0.0) -> float:
    value = resolve(spec, data)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    # Prometheus emits NaN, and `float()` also parses the literals "nan",
    # "inf" and "-inf" out of scene text. A non-finite number is not a usable
    # coordinate or angle, so it degrades to the fallback like any other
    # uncoercible value rather than propagating into the geometry.
    return number if math.isfinite(number) else default


def resolve_list(spec: Any, data: Mapping[str, Any]) -> list[Any]:
    value = resolve(spec, data)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
