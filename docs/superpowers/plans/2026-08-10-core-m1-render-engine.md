# Core M1 — Schema + Render Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `ors-schema` (the shared data contract) and `ors-render` (scene JSON → 240×240 PIL image), proven by golden-image tests that reproduce all four screens of the existing `k8s_monitor.py` with no hardware attached.

**Architecture:** Two pure-Python packages in a `uv` workspace monorepo. `ors-schema` holds pydantic models for scenes, elements, palettes and templates. `ors-render` is a pure function — scene + data context in, image out — with no I/O, no network, no hardware and no clock reads. Everything downstream (daemon, server previews, phase-2 designer) imports these two.

**Tech Stack:** Python 3.11+, `uv` workspace, pydantic v2, Pillow, pytest, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-10-openrackscreen-core-design.md` (§4.1, §5, §10, M1)

## Global Constraints

- **Research before implementing.** Spec §0 applies to every task. Verify Pillow's current `ImageDraw.arc`/`textbbox` behaviour, pydantic v2 discriminated-union syntax, and `uv` workspace layout against current upstream docs before writing code. Where this plan and current docs disagree, docs win — raise it, then implement.
- **TDD, always.** Failing test first, watch it fail, minimal implementation, watch it pass, commit. No exceptions.
- `ors-render` must import nothing from `server/` or `daemon/`, must perform no network or filesystem I/O except loading its own bundled font/asset files, and must never read the wall clock. Any time-dependent value arrives through `RenderContext`.
- Everything in M1 runs in CI on x86 Linux with no Pi, no SPI, no GPIO.
- Python `>=3.11`. Pin the exact minimum only after verifying what current Raspberry Pi OS ships.
- Normalization rules, used everywhere without exception:
  - `cx`, `cy`, and any width/height/coordinate are fractions of the **full panel size** (1.0 = 240 px at default size).
  - `r` and `thickness` are fractions of the **panel radius** (1.0 = 120 px at default size).
  - `size` on text is px at a **240 px baseline** and is scaled by `panel_px / 240`.
- Supersample factor is 2 by default, matching the existing script.
- Colors are `#rrggbb` strings. The literal token `@palette` is legal anywhere a color is, and resolves to the enclosing element's palette accent.
- Every public function gets a type annotation. `ruff check` and `ruff format --check` must pass before every commit. Run `uv run ruff check --fix . && uv run ruff format .` first — import ordering in this plan's code snippets is illustrative, not authoritative, and ruff will correct it.

---

### Task 1: Workspace scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `packages/ors-schema/pyproject.toml`
- Create: `packages/ors-schema/src/ors_schema/__init__.py`
- Create: `packages/ors-render/pyproject.toml`
- Create: `packages/ors-render/src/ors_render/__init__.py`
- Create: `packages/ors-schema/tests/test_smoke.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing
- Produces: importable packages `ors_schema` and `ors_render`, both exposing `__version__: str`. Command `uv run pytest` runs all tests from the repo root. Command `uv run ruff check .` lints.

- [ ] **Step 1: Write the failing test**

`packages/ors-schema/tests/test_smoke.py`:

```python
def test_packages_are_importable():
    import ors_render
    import ors_schema

    assert isinstance(ors_schema.__version__, str)
    assert isinstance(ors_render.__version__, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-schema/tests/test_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_schema'` (or `uv` erroring because no `pyproject.toml` exists yet).

- [ ] **Step 3: Write minimal implementation**

`pyproject.toml` (repo root):

```toml
[project]
name = "openrackscreen"
version = "0.1.0"
description = "Configurable monitoring displays for server racks"
requires-python = ">=3.11"
dependencies = ["ors-schema", "ors-render"]

[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
ors-schema = { workspace = true }
ors-render = { workspace = true }

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["packages/ors-schema/tests", "packages/ors-render/tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

`packages/ors-schema/pyproject.toml`:

```toml
[project]
name = "ors-schema"
version = "0.1.0"
description = "Shared data contract for OpenRackScreen"
requires-python = ">=3.11"
dependencies = ["pydantic>=2.7"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ors_schema"]
```

`packages/ors-schema/src/ors_schema/__init__.py`:

```python
__version__ = "0.1.0"
```

`packages/ors-render/pyproject.toml`:

```toml
[project]
name = "ors-render"
version = "0.1.0"
description = "Scene JSON to image renderer for OpenRackScreen"
requires-python = ">=3.11"
dependencies = ["ors-schema", "pillow>=10.3"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ors_render"]

[tool.uv.sources]
ors-schema = { workspace = true }
```

`packages/ors-render/src/ors_render/__init__.py`:

```python
__version__ = "0.1.0"
```

Create the empty test directory for the render package so pytest's `testpaths` resolves:

```bash
mkdir -p packages/ors-render/tests
touch packages/ors-render/tests/__init__.py
```

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv sync --all-packages && uv run pytest -v`
Expected: PASS — 1 passed.
Run: `uv run ruff check . && uv run ruff format --check .`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml packages .github
git commit -m "chore: uv workspace with ors-schema and ors-render packages"
```

---

### Task 2: Palette and color models

**Files:**
- Create: `packages/ors-schema/src/ors_schema/palette.py`
- Modify: `packages/ors-schema/src/ors_schema/__init__.py`
- Test: `packages/ors-schema/tests/test_palette.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Stop(at: float, color: str)`
  - `GradientPalette(kind: Literal["gradient"], stops: list[Stop])`
  - `ThresholdEntry(at: float, palette: PaletteRef)`
  - `ThresholdPalette(kind: Literal["threshold"], thresholds: list[ThresholdEntry])`
  - `PaletteRef = str | GradientPalette | ThresholdPalette`
  - `Color = str` (validated `#rrggbb`, or the literal `@palette`)

- [ ] **Step 1: Write the failing test**

`packages/ors-schema/tests/test_palette.py`:

```python
import pytest
from pydantic import BaseModel, ValidationError

from ors_schema.palette import Color, GradientPalette, PaletteRef, ThresholdPalette


class Holder(BaseModel):
    color: Color
    palette: PaletteRef


def test_named_palette_is_a_plain_string():
    h = Holder(color="#ff0000", palette="cyan")
    assert h.palette == "cyan"


def test_palette_token_is_a_legal_color():
    assert Holder(color="@palette", palette="cyan").color == "@palette"


def test_gradient_palette_parses_from_dict():
    h = Holder(
        color="#ffffff",
        palette={"kind": "gradient", "stops": [{"at": 0.0, "color": "#00e5ff"}, {"at": 1.0, "color": "#2979ff"}]},
    )
    assert isinstance(h.palette, GradientPalette)
    assert h.palette.stops[1].color == "#2979ff"


def test_threshold_palette_parses_from_dict():
    h = Holder(
        color="#ffffff",
        palette={
            "kind": "threshold",
            "thresholds": [
                {"at": 0, "palette": "green"},
                {"at": 70, "palette": "amber"},
                {"at": 90, "palette": "red"},
            ],
        },
    )
    assert isinstance(h.palette, ThresholdPalette)
    assert h.palette.thresholds[2].palette == "red"


@pytest.mark.parametrize("bad", ["ff0000", "#f00", "#gggggg", "red", ""])
def test_invalid_colors_are_rejected(bad):
    with pytest.raises(ValidationError):
        Holder(color=bad, palette="cyan")


def test_gradient_stops_must_be_ordered_and_nonempty():
    with pytest.raises(ValidationError):
        GradientPalette(stops=[])
    with pytest.raises(ValidationError):
        GradientPalette(stops=[{"at": 1.0, "color": "#000000"}, {"at": 0.0, "color": "#ffffff"}])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-schema/tests/test_palette.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_schema.palette'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-schema/src/ors_schema/palette.py`:

```python
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

PALETTE_TOKEN = "@palette"

Color = Annotated[str, Field(pattern=r"^(#[0-9a-fA-F]{6}|@palette)$")]
"""A `#rrggbb` color, or the literal `@palette` meaning the element's palette accent."""


class Stop(BaseModel):
    at: float = Field(ge=0.0, le=1.0)
    color: Color


class GradientPalette(BaseModel):
    kind: Literal["gradient"] = "gradient"
    stops: list[Stop] = Field(min_length=1)

    @field_validator("stops")
    @classmethod
    def _ordered(cls, stops: list[Stop]) -> list[Stop]:
        positions = [s.at for s in stops]
        if positions != sorted(positions):
            raise ValueError("gradient stops must be ordered by 'at' ascending")
        return stops


class ThresholdEntry(BaseModel):
    at: float
    palette: PaletteRef


class ThresholdPalette(BaseModel):
    kind: Literal["threshold"] = "threshold"
    thresholds: list[ThresholdEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> ThresholdPalette:
        positions = [t.at for t in self.thresholds]
        if positions != sorted(positions):
            raise ValueError("thresholds must be ordered by 'at' ascending")
        return self


PaletteRef = Union[str, Annotated[Union[GradientPalette, ThresholdPalette], Field(discriminator="kind")]]

ThresholdEntry.model_rebuild()
```

Append to `packages/ors-schema/src/ors_schema/__init__.py`:

```python
from ors_schema.palette import (
    Color,
    GradientPalette,
    PaletteRef,
    Stop,
    ThresholdEntry,
    ThresholdPalette,
)

__all__ = [
    "Color",
    "GradientPalette",
    "PaletteRef",
    "Stop",
    "ThresholdEntry",
    "ThresholdPalette",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-schema/tests/test_palette.py -v`
Expected: PASS — 9 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-schema
git commit -m "feat(schema): palette and color models"
```

---

### Task 3: Element, scene and template models

**Files:**
- Create: `packages/ors-schema/src/ors_schema/scene.py`
- Modify: `packages/ors-schema/src/ors_schema/__init__.py`
- Test: `packages/ors-schema/tests/test_scene.py`

**Interfaces:**
- Consumes: `ors_schema.palette` (`Color`, `PaletteRef`)
- Produces:
  - `Binding = str` — a string that may contain `{{ ... }}`
  - `NumberSpec = float | int | str` — a literal number or a binding string
  - Element models: `RingElement`, `ArcElement`, `TextElement`, `RectElement`, `LineElement`, `ImageElement`, `SparklineElement`, `GroupElement`
  - `Repeat(over: str, as_: str, limit: int)` — JSON field name is `as`, python attribute is `as_`
  - `Element` — discriminated union on `type`
  - `Scene(name: str, when: str | None, background: Color, elements: list[Element])`
  - `ParamSpec(type, label, default)` and `Template(name, category, builtin, params_schema, scenes)`

- [ ] **Step 1: Write the failing test**

`packages/ors-schema/tests/test_scene.py`:

```python
import pytest
from pydantic import ValidationError

from ors_schema.scene import (
    GroupElement,
    RingElement,
    Scene,
    Template,
    TextElement,
)


def test_ring_element_defaults_match_spec():
    ring = RingElement(type="ring")
    assert (ring.cx, ring.cy) == (0.5, 0.5)
    assert ring.r == 0.875
    assert ring.min == 0 and ring.max == 100
    assert ring.cap == "none"
    assert ring.start_angle == -90
    assert ring.direction == "cw"


def test_scene_parses_a_mixed_element_list_into_typed_models():
    scene = Scene.model_validate(
        {
            "name": "cpu",
            "elements": [
                {"type": "ring", "value": "{{prom.cpu}}", "palette": "cyan"},
                {"type": "text", "cy": 0.517, "size": 52, "text": "{{prom.cpu | round:0}}%"},
            ],
        }
    )
    assert isinstance(scene.elements[0], RingElement)
    assert isinstance(scene.elements[1], TextElement)
    assert scene.elements[1].size == 52
    assert scene.background == "#000000"


def test_group_nests_elements_and_carries_repeat():
    group = GroupElement.model_validate(
        {
            "type": "group",
            "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
            "step": {"r": -0.125, "thickness": -0.017},
            "palettes": ["blue", "amber", "violet"],
            "elements": [{"type": "ring", "r": 0.858, "value": "{{t.progress}}"}],
        }
    )
    assert group.repeat is not None
    assert group.repeat.as_ == "t"
    assert group.repeat.limit == 3
    assert group.step["r"] == -0.125
    assert isinstance(group.elements[0], RingElement)


def test_unknown_element_type_is_rejected():
    with pytest.raises(ValidationError):
        Scene.model_validate({"elements": [{"type": "hologram"}]})


def test_scene_round_trips_through_json_with_repeat_alias():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t"},
                    "elements": [{"type": "ring", "value": "{{t.progress}}"}],
                }
            ]
        }
    )
    dumped = scene.model_dump(by_alias=True, exclude_none=True)
    assert dumped["elements"][0]["repeat"]["as"] == "t"
    assert Scene.model_validate(dumped) == scene


def test_template_declares_params_and_scenes():
    tpl = Template.model_validate(
        {
            "name": "ring-gauge",
            "builtin": True,
            "params_schema": {"title": {"type": "string", "default": "CPU"}},
            "scenes": [{"elements": [{"type": "text", "text": "{{params.title}}"}]}],
        }
    )
    assert tpl.params_schema["title"].default == "CPU"
    assert tpl.scenes[0].elements[0].text == "{{params.title}}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-schema/tests/test_scene.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_schema.scene'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-schema/src/ors_schema/scene.py`:

```python
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from ors_schema.palette import Color, PaletteRef

Binding = str
"""A string that may contain `{{ ... }}` binding expressions."""

NumberSpec = Union[float, int, str]
"""A literal number, or a binding string resolving to one."""


class BaseElement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cx: float = 0.5
    cy: float = 0.5
    when: str | None = None
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


class RingElement(BaseElement):
    type: Literal["ring"] = "ring"
    r: float = 0.875
    thickness: float = 0.092
    value: NumberSpec = 0
    min: float = 0
    max: float = 100
    palette: PaletteRef = "cyan"
    track: Color | None = "#16181e"
    cap: Literal["none", "dot"] = "none"
    start_angle: float = -90
    direction: Literal["cw", "ccw"] = "cw"


class ArcElement(BaseElement):
    type: Literal["arc"] = "arc"
    r: float = 0.875
    thickness: float = 0.092
    from_angle: float = -90
    to_angle: float = 90
    palette: PaletteRef = "cyan"


class TextElement(BaseElement):
    type: Literal["text"] = "text"
    text: Binding = ""
    size: float = 13
    font: Literal["regular", "bold"] = "bold"
    color: Color = "#ffffff"
    align: Literal["center", "left", "right"] = "center"
    max_width: float | None = None
    ellipsis: bool = True


class RectElement(BaseElement):
    type: Literal["rect"] = "rect"
    w: float = 0.2
    h: float = 0.05
    radius: float = 0.0
    fill: Color | None = "#ffffff"
    stroke: Color | None = None
    stroke_width: float = 0.004


class LineElement(BaseElement):
    type: Literal["line"] = "line"
    x1: float = 0.2
    y1: float = 0.5
    x2: float = 0.8
    y2: float = 0.5
    color: Color = "#ffffff"
    width: float = 0.004


class ImageElement(BaseElement):
    type: Literal["image"] = "image"
    src: Binding = ""
    w: float = 0.25
    h: float = 0.25
    fit: Literal["contain", "cover", "stretch"] = "contain"


class SparklineElement(BaseElement):
    type: Literal["sparkline"] = "sparkline"
    values: Binding = ""
    w: float = 0.5
    h: float = 0.15
    palette: PaletteRef = "cyan"
    fill: bool = False


class Repeat(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    over: Binding
    as_: str = Field(default="item", alias="as")
    limit: int = Field(default=8, ge=1, le=32)


class GroupElement(BaseElement):
    type: Literal["group"] = "group"
    elements: list[Element] = Field(default_factory=list)
    repeat: Repeat | None = None
    step: dict[str, float] = Field(default_factory=dict)
    palettes: list[PaletteRef] = Field(default_factory=list)


Element = Annotated[
    Union[
        RingElement,
        ArcElement,
        TextElement,
        RectElement,
        LineElement,
        ImageElement,
        SparklineElement,
        GroupElement,
    ],
    Field(discriminator="type"),
]

GroupElement.model_rebuild()


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    when: str | None = None
    background: Color = "#000000"
    elements: list[Element] = Field(default_factory=list)


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "color", "palette", "binding", "boolean"] = "string"
    label: str = ""
    default: Any = None


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    category: str = "general"
    builtin: bool = False
    params_schema: dict[str, ParamSpec] = Field(default_factory=dict)
    scenes: list[Scene] = Field(default_factory=list)
```

Extend `packages/ors-schema/src/ors_schema/__init__.py` with the new names (add to the imports and to `__all__`): `ArcElement`, `Binding`, `Element`, `GroupElement`, `ImageElement`, `LineElement`, `NumberSpec`, `ParamSpec`, `RectElement`, `Repeat`, `RingElement`, `Scene`, `SparklineElement`, `Template`, `TextElement`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-schema/tests -v`
Expected: PASS — all tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-schema
git commit -m "feat(schema): element, scene and template models"
```

---

### Task 4: Sandboxed expression evaluator

This is a security boundary. It is a tree-walking interpreter over `ast` nodes — **never** `eval()` or `compile()` of user source.

**Files:**
- Create: `packages/ors-render/src/ors_render/expr.py`
- Test: `packages/ors-render/tests/test_expr.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `evaluate(expr: str, data: Mapping[str, Any]) -> Any`
  - `truthy(expr: str | None, data: Mapping[str, Any]) -> bool` — `None` expression is always `True`
  - `ExpressionError(Exception)`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_expr.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_expr.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_render.expr'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/expr.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-render/tests/test_expr.py -v`
Expected: PASS — all parametrized cases pass, including every hostile input raising `ExpressionError`.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): sandboxed expression evaluator with hostile-input tests"
```

---

### Task 5: Bindings and filters

**Files:**
- Create: `packages/ors-render/src/ors_render/bindings.py`
- Test: `packages/ors-render/tests/test_bindings.py`

**Interfaces:**
- Consumes: `ors_render.expr.evaluate`
- Produces:
  - `resolve(spec: Any, data: Mapping[str, Any]) -> Any` — a whole-string binding returns the raw value; mixed text interpolates
  - `resolve_text(spec: Any, data: Mapping[str, Any]) -> str`
  - `resolve_number(spec: Any, data: Mapping[str, Any], default: float = 0.0) -> float`
  - `resolve_list(spec: Any, data: Mapping[str, Any]) -> list[Any]`
  - `FILTERS: dict[str, Callable[..., Any]]`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_bindings.py`:

```python
import pytest

from ors_render.bindings import resolve, resolve_list, resolve_number, resolve_text

DATA = {
    "prom": {"cpu": 42.4, "mem_used": 19.4, "mem_total": 32.0, "hot": {"node": ".5", "value": 71.2}},
    "qbit": {"active": [{"name": "a-very-long-torrent-name", "progress": 91.2}], "speed": 4613734, "eta": 1112},
    "params": {"title": "CPU"},
}


def test_whole_string_binding_returns_the_raw_value():
    assert resolve("{{prom.cpu}}", DATA) == 42.4
    assert resolve("{{qbit.active}}", DATA) == DATA["qbit"]["active"]


def test_mixed_text_interpolates_to_a_string():
    assert resolve("{{prom.cpu | round:0}}%", DATA) == "42%"
    assert resolve_text("peak: {{prom.hot.node}} {{prom.hot.value | round:0}}%", DATA) == "peak: .5 71%"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_bindings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_render.bindings'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/bindings.py`:

```python
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ors_render.expr import ExpressionError, evaluate

_BINDING = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_WHOLE = re.compile(r"^\s*\{\{(.*?)\}\}\s*$", re.DOTALL)


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
    return text if len(text) <= limit else text[: limit - 1] + "."


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
    parts = [p.strip() for p in inner.split("|")]
    expression, filters = parts[0], parts[1:]
    try:
        value: Any = evaluate(expression, data)
    except ExpressionError:
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
            value = None
    return value


def resolve(spec: Any, data: Mapping[str, Any]) -> Any:
    if not isinstance(spec, str):
        return spec
    whole = _WHOLE.match(spec)
    if whole:
        return _apply(whole.group(1), data)
    if "{{" not in spec:
        return spec
    return _BINDING.sub(lambda m: _stringify(_apply(m.group(1), data)), spec)


def resolve_text(spec: Any, data: Mapping[str, Any]) -> str:
    return _stringify(resolve(spec, data))


def resolve_number(spec: Any, data: Mapping[str, Any], default: float = 0.0) -> float:
    value = resolve(spec, data)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-render/tests/test_bindings.py -v`
Expected: PASS — all cases pass.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): binding resolution and filter pipeline"
```

---

### Task 6: Geometry, palettes, fonts and the golden-image harness

**Files:**
- Create: `packages/ors-render/src/ors_render/geometry.py`
- Create: `packages/ors-render/src/ors_render/palettes.py`
- Create: `packages/ors-render/src/ors_render/fonts.py`
- Create: `packages/ors-render/src/ors_render/assets/fonts/` (bundled TTFs)
- Create: `packages/ors-render/tests/conftest.py`
- Test: `packages/ors-render/tests/test_geometry.py`, `packages/ors-render/tests/test_palettes.py`

**Interfaces:**
- Consumes: `ors_schema.palette`
- Produces:
  - `Geometry(size: int = 240, supersample: int = 2)` with `.px: int`, `.x(n)`, `.y(n)`, `.span(n)`, `.radial(n)`, `.font_px(size_at_240)`
  - `NAMED_PALETTES: dict[str, GradientPalette]`
  - `resolve_palette(ref: PaletteRef, value_pct: float) -> GradientPalette`
  - `gradient_color(palette: GradientPalette, t: float) -> tuple[int, int, int]`
  - `hex_to_rgb(color: str) -> tuple[int, int, int]`
  - `load_font(weight: Literal["regular", "bold"], px: int) -> ImageFont.FreeTypeFont` (cached)
  - pytest fixture `assert_golden(image: Image.Image, name: str) -> None`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_geometry.py`:

```python
import pytest

from ors_render.geometry import Geometry


def test_default_geometry_supersamples_by_two():
    g = Geometry()
    assert g.size == 240
    assert g.px == 480


def test_positions_are_fractions_of_full_size():
    g = Geometry()
    assert g.x(0.5) == 240.0
    assert g.y(1.0) == 480.0
    assert g.span(0.25) == 120.0


def test_radii_are_fractions_of_the_panel_radius():
    g = Geometry()
    # 0.875 of a 240px panel radius == 105px, which is 210px in supersampled space
    assert g.radial(0.875) == 210.0
    assert g.radial(0.092) == pytest.approx(22.08)


def test_font_sizes_scale_from_a_240_baseline():
    assert Geometry().font_px(52) == 104
    assert Geometry(size=480, supersample=1).font_px(52) == 104
```

`packages/ors-render/tests/test_palettes.py`:

```python
import pytest

from ors_render.palettes import NAMED_PALETTES, gradient_color, hex_to_rgb, resolve_palette


def test_hex_to_rgb():
    assert hex_to_rgb("#00e5ff") == (0, 229, 255)


def test_named_palettes_cover_the_builtin_set():
    for name in ("cyan", "green", "lime", "amber", "red", "violet", "blue", "mono"):
        assert name in NAMED_PALETTES


def test_gradient_interpolates_between_stops():
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, 0.0) == hex_to_rgb(palette.stops[0].color)
    assert gradient_color(palette, 1.0) == hex_to_rgb(palette.stops[-1].color)
    mid = gradient_color(palette, 0.5)
    assert mid != gradient_color(palette, 0.0)


def test_gradient_clamps_out_of_range():
    palette = NAMED_PALETTES["cyan"]
    assert gradient_color(palette, -5) == gradient_color(palette, 0.0)
    assert gradient_color(palette, 99) == gradient_color(palette, 1.0)


def test_unknown_palette_name_falls_back_to_mono():
    assert resolve_palette("does-not-exist", 50) == NAMED_PALETTES["mono"]


@pytest.mark.parametrize(("value", "expected"), [(10, "green"), (75, "amber"), (99, "red")])
def test_threshold_palette_selects_by_value(value, expected):
    ref = {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
    from ors_schema.palette import ThresholdPalette

    assert resolve_palette(ThresholdPalette.model_validate(ref), value) == NAMED_PALETTES[expected]
```

`packages/ors-render/tests/conftest.py`:

```python
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture
def assert_golden() -> Callable[[Image.Image, str], None]:
    def _assert(image: Image.Image, name: str, tolerance: float = 2.0) -> None:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path = GOLDEN_DIR / f"{name}.png"
        if os.environ.get("UPDATE_GOLDEN"):
            image.save(path)
            return
        assert path.exists(), f"missing golden {path}; regenerate with UPDATE_GOLDEN=1 uv run pytest"
        reference = Image.open(path).convert("RGB")
        assert image.size == reference.size, f"size {image.size} != golden {reference.size}"
        diff = ImageChops.difference(image.convert("RGB"), reference)
        mean = max(ImageStat.Stat(diff).mean)
        assert mean <= tolerance, f"golden {name} differs by mean {mean:.2f} (tolerance {tolerance})"

    return _assert
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_geometry.py packages/ors-render/tests/test_palettes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_render.geometry'`

- [ ] **Step 3: Write minimal implementation**

Bundle the fonts. DejaVu ships with most Linux distributions; **verify its license permits redistribution before committing it** (it is a permissive Bitstream Vera–derived license, but confirm the current text), and record the source in `packages/ors-render/src/ors_render/assets/fonts/LICENSE`:

```bash
mkdir -p packages/ors-render/src/ors_render/assets/fonts
cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
   /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
   packages/ors-render/src/ors_render/assets/fonts/
```

If those paths do not exist on the build machine, download the release from the upstream DejaVu project rather than substituting a different font — golden images depend on exact glyph metrics.

`packages/ors-render/src/ors_render/geometry.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    """Maps normalized scene coordinates onto the supersampled render canvas.

    Positions and spans are fractions of the full panel size.
    Radii and thicknesses are fractions of the panel *radius*.
    Font sizes are px at a 240px baseline.
    """

    size: int = 240
    supersample: int = 2

    @property
    def px(self) -> int:
        return self.size * self.supersample

    def x(self, n: float) -> float:
        return n * self.px

    def y(self, n: float) -> float:
        return n * self.px

    def span(self, n: float) -> float:
        return n * self.px

    def radial(self, n: float) -> float:
        return n * self.px / 2

    def font_px(self, size_at_240: float) -> int:
        return max(1, round(size_at_240 * self.px / 240))
```

`packages/ors-render/src/ors_render/palettes.py`:

```python
from __future__ import annotations

from ors_schema.palette import GradientPalette, PaletteRef, ThresholdPalette


def _grad(*colors: str) -> GradientPalette:
    step = 1.0 / (len(colors) - 1)
    return GradientPalette(stops=[{"at": i * step, "color": c} for i, c in enumerate(colors)])


NAMED_PALETTES: dict[str, GradientPalette] = {
    "cyan": _grad("#00e5ff", "#2979ff"),
    "green": _grad("#69f0ae", "#00c853"),
    "lime": _grad("#76ff03", "#00c853"),
    "amber": _grad("#ffeb3b", "#ff9100"),
    "red": _grad("#ff5252", "#b71c1c"),
    "blue": _grad("#00b0ff", "#0091ea"),
    "orange": _grad("#ff9100", "#e65100"),
    "violet": _grad("#d500f9", "#7b1fa2"),
    "mono": _grad("#ffffff", "#9e9e9e"),
    "idle": _grad("#0d0d1a", "#1a1a2e"),
}


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def gradient_color(palette: GradientPalette, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    stops = palette.stops
    if len(stops) == 1:
        return hex_to_rgb(stops[0].color)
    for first, second in zip(stops, stops[1:], strict=False):
        if first.at <= t <= second.at:
            span = (second.at - first.at) or 1.0
            f = (t - first.at) / span
            a, b = hex_to_rgb(first.color), hex_to_rgb(second.color)
            return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))  # type: ignore[return-value]
    return hex_to_rgb(stops[-1].color)


def resolve_palette(ref: PaletteRef, value_pct: float = 0.0) -> GradientPalette:
    if isinstance(ref, str):
        return NAMED_PALETTES.get(ref, NAMED_PALETTES["mono"])
    if isinstance(ref, ThresholdPalette):
        chosen = ref.thresholds[0].palette
        for entry in ref.thresholds:
            if value_pct >= entry.at:
                chosen = entry.palette
        return resolve_palette(chosen, value_pct)
    return ref
```

`packages/ors-render/src/ors_render/fonts.py`:

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FILES = {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}


@lru_cache(maxsize=128)
def load_font(weight: Literal["regular", "bold"], px: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / _FILES[weight]
    if not path.exists():
        raise FileNotFoundError(f"bundled font missing: {path}")
    return ImageFont.truetype(str(path), px)
```

Add the font files to the wheel — append to `packages/ors-render/pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/ors_render/assets" = "ors_render/assets"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-render/tests -v`
Expected: PASS — geometry and palette tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): geometry, palettes, bundled fonts, golden harness"
```

---

### Task 7: Canvas and the text element

**Files:**
- Create: `packages/ors-render/src/ors_render/context.py`
- Create: `packages/ors-render/src/ors_render/canvas.py`
- Create: `packages/ors-render/src/ors_render/elements/__init__.py`
- Create: `packages/ors-render/src/ors_render/elements/text.py`
- Create: `packages/ors-render/src/ors_render/render.py`
- Test: `packages/ors-render/tests/test_text.py`

**Interfaces:**
- Consumes: `Geometry`, `load_font`, `resolve_text`, `truthy`, `resolve_palette`, `gradient_color`
- Produces:
  - `RenderContext(data: dict[str, Any], assets: Mapping[str, bytes])`
  - `Canvas(geometry: Geometry, background: str)` with `.draw: ImageDraw.ImageDraw`, `.geometry`, `.finish() -> Image.Image` (downsamples with LANCZOS)
  - `ElementRenderer = Callable[[Canvas, Element, RenderContext, PaletteContext], None]`
  - `RENDERERS: dict[str, ElementRenderer]` — registry keyed by element `type`
  - `render_scene(scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2) -> Image.Image`
  - `resolve_color(color: str, palette: GradientPalette) -> tuple[int, int, int]` — handles `@palette`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_text.py`:

```python
from ors_schema.scene import Scene

from ors_render.context import RenderContext
from ors_render.render import render_scene


def _ctx() -> RenderContext:
    return RenderContext(data={"prom": {"cpu": 42.4}, "params": {"title": "CPU"}})


def test_render_scene_returns_a_240_rgb_image():
    image = render_scene(Scene(), _ctx())
    assert image.size == (240, 240)
    assert image.mode == "RGB"


def test_background_is_honoured():
    image = render_scene(Scene(background="#101010"), _ctx())
    assert image.getpixel((0, 0)) == (16, 16, 16)


def test_text_element_renders_centered(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "text", "cy": 0.5, "size": 52, "text": "{{prom.cpu | round:0}}%"},
                {"type": "text", "cy": 0.28, "size": 15, "text": "{{params.title}}", "color": "#00e5ff"},
            ]
        }
    )
    assert_golden(render_scene(scene, _ctx()), "text_basic")


def test_element_when_false_is_skipped():
    shown = render_scene(
        Scene.model_validate({"elements": [{"type": "text", "size": 52, "text": "X"}]}), _ctx()
    )
    hidden = render_scene(
        Scene.model_validate(
            {"elements": [{"type": "text", "size": 52, "text": "X", "when": "prom.cpu > 100"}]}
        ),
        _ctx(),
    )
    assert shown.tobytes() != hidden.tobytes()
    assert hidden.getpixel((120, 120)) == (0, 0, 0)


def test_text_truncates_to_max_width(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "text", "size": 20, "text": "an extremely long torrent name", "max_width": 0.8}
            ]
        }
    )
    assert_golden(render_scene(scene, _ctx()), "text_truncated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_text.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_render.context'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/context.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RenderContext:
    """Everything a scene may read. The renderer has no other source of truth."""

    data: dict[str, Any] = field(default_factory=dict)
    assets: Mapping[str, bytes] = field(default_factory=dict)

    def child(self, extra: Mapping[str, Any]) -> RenderContext:
        """A context with additional namespaces layered on top (used by repeat)."""
        return RenderContext(data={**self.data, **extra}, assets=self.assets)
```

`packages/ors-render/src/ors_render/canvas.py`:

```python
from __future__ import annotations

from PIL import Image, ImageDraw

from ors_render.geometry import Geometry
from ors_render.palettes import hex_to_rgb


class Canvas:
    def __init__(self, geometry: Geometry, background: str = "#000000") -> None:
        self.geometry = geometry
        self.image = Image.new("RGB", (geometry.px, geometry.px), hex_to_rgb(background))
        self.draw = ImageDraw.Draw(self.image)

    def finish(self) -> Image.Image:
        if self.geometry.supersample == 1:
            return self.image
        return self.image.resize((self.geometry.size, self.geometry.size), Image.LANCZOS)
```

`packages/ors-render/src/ors_render/elements/__init__.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ors_schema.palette import GradientPalette

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.palettes import gradient_color, hex_to_rgb

ElementRenderer = Callable[[Canvas, Any, RenderContext, GradientPalette], None]

RENDERERS: dict[str, ElementRenderer] = {}


def register(type_name: str) -> Callable[[ElementRenderer], ElementRenderer]:
    def decorator(func: ElementRenderer) -> ElementRenderer:
        RENDERERS[type_name] = func
        return func

    return decorator


def resolve_color(
    color: str, palette: GradientPalette, data: Mapping[str, Any] | None = None
) -> tuple[int, int, int]:
    """Resolve a color field, which may be `#rrggbb`, `@palette`, or a binding."""
    if "{{" in color:
        from ors_render.bindings import resolve_text

        color = resolve_text(color, data or {}) or "#ffffff"
    if color == "@palette":
        return gradient_color(palette, 1.0)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return (255, 255, 255)
    return hex_to_rgb(color)
```

This needs `import re` and `from collections.abc import Mapping` at the top of the
module. Every call site passes `ctx.data` as the third argument.

`packages/ors-render/src/ors_render/elements/text.py`:

```python
from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import TextElement

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color
from ors_render.fonts import load_font


@register("text")
def render_text(canvas: Canvas, element: TextElement, ctx: RenderContext, palette: GradientPalette) -> None:
    text = resolve_text(element.text, ctx.data)
    if not text:
        return

    geometry = canvas.geometry
    font = load_font(element.font, geometry.font_px(element.size))
    fill = resolve_color(element.color, palette, ctx.data)

    if element.max_width is not None:
        limit = geometry.span(element.max_width)
        while text and canvas.draw.textlength(text, font=font) > limit:
            text = text[:-1]
            if element.ellipsis and text:
                candidate = text[:-1] + "."
                if canvas.draw.textlength(candidate, font=font) <= limit:
                    text = candidate
                    break

    box = canvas.draw.textbbox((0, 0), text, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    if element.align == "left":
        x = cx - box[0]
    elif element.align == "right":
        x = cx - width - box[0]
    else:
        x = cx - width / 2 - box[0]
    canvas.draw.text((x, cy - height / 2 - box[1]), text, font=font, fill=fill)
```

`packages/ors-render/src/ors_render/render.py`:

```python
from __future__ import annotations

from ors_schema.scene import Scene

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import RENDERERS
from ors_render.elements import text as _text  # noqa: F401 - registers the renderer
from ors_render.expr import ExpressionError, truthy
from ors_render.geometry import Geometry
from ors_render.palettes import NAMED_PALETTES

from PIL import Image


def render_scene(
    scene: Scene, ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    canvas = Canvas(Geometry(size=size, supersample=supersample), scene.background)
    for element in scene.elements:
        draw_element(canvas, element, ctx)
    return canvas.finish()


def draw_element(canvas: Canvas, element: object, ctx: RenderContext) -> None:
    when = getattr(element, "when", None)
    try:
        if not truthy(when, ctx.data):
            return
    except ExpressionError:
        return
    renderer = RENDERERS.get(getattr(element, "type", ""))
    if renderer is None:
        return
    palette_ref = getattr(element, "palette", "mono")
    from ors_render.palettes import resolve_palette

    palette = resolve_palette(palette_ref, 0.0) if palette_ref else NAMED_PALETTES["mono"]
    renderer(canvas, element, ctx, palette)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_text.py -v` then inspect the produced PNGs in `packages/ors-render/tests/golden/` and confirm they look correct, then `uv run pytest packages/ors-render/tests/test_text.py -v`
Expected: PASS — 5 passed.

**Never commit a golden image you have not looked at.** A wrong golden locks in a bug permanently.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): canvas, render_scene entry point and text element"
```

---

### Task 8: Rect and line elements

**Files:**
- Create: `packages/ors-render/src/ors_render/elements/shapes.py`
- Modify: `packages/ors-render/src/ors_render/render.py` (import to register)
- Test: `packages/ors-render/tests/test_shapes.py`

**Interfaces:**
- Consumes: `Canvas`, `register`, `resolve_color`, `resolve_number`
- Produces: renderers registered for `"rect"` and `"line"`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_shapes.py`:

```python
from ors_schema.scene import Scene

from ors_render.context import RenderContext
from ors_render.render import render_scene


def test_rect_fills_expected_pixels():
    scene = Scene.model_validate(
        {"elements": [{"type": "rect", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.5, "fill": "#ff0000"}]}
    )
    image = render_scene(scene, RenderContext())
    assert image.getpixel((120, 120)) == (255, 0, 0)
    assert image.getpixel((10, 10)) == (0, 0, 0)


def test_rounded_rect_and_line_render(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "rect", "cy": 0.35, "w": 0.6, "h": 0.12, "radius": 0.06, "fill": "#2979ff"},
                {"type": "rect", "cy": 0.55, "w": 0.6, "h": 0.12, "radius": 0.06, "fill": None,
                 "stroke": "#69f0ae", "stroke_width": 0.008},
                {"type": "line", "x1": 0.2, "y1": 0.75, "x2": 0.8, "y2": 0.75, "color": "#ffffff",
                 "width": 0.01},
            ]
        }
    )
    assert_golden(render_scene(scene, RenderContext()), "shapes_basic")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_shapes.py -v`
Expected: FAIL — first test fails on `(0, 0, 0) != (255, 0, 0)` because no `rect` renderer is registered.

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/elements/shapes.py`:

```python
from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import LineElement, RectElement

from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color


@register("rect")
def render_rect(canvas: Canvas, element: RectElement, ctx: RenderContext, palette: GradientPalette) -> None:
    g = canvas.geometry
    cx, cy = g.x(element.cx), g.y(element.cy)
    half_w, half_h = g.span(element.w) / 2, g.span(element.h) / 2
    box = [cx - half_w, cy - half_h, cx + half_w, cy + half_h]
    fill = resolve_color(element.fill, palette, ctx.data) if element.fill else None
    stroke = resolve_color(element.stroke, palette, ctx.data) if element.stroke else None
    width = max(1, int(g.span(element.stroke_width)))
    radius = g.span(element.radius)
    if radius > 0:
        canvas.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=stroke, width=width)
    else:
        canvas.draw.rectangle(box, fill=fill, outline=stroke, width=width)


@register("line")
def render_line(canvas: Canvas, element: LineElement, ctx: RenderContext, palette: GradientPalette) -> None:
    g = canvas.geometry
    canvas.draw.line(
        [g.x(element.x1), g.y(element.y1), g.x(element.x2), g.y(element.y2)],
        fill=resolve_color(element.color, palette, ctx.data),
        width=max(1, int(g.span(element.width))),
    )
```

In `packages/ors-render/src/ors_render/render.py`, add next to the existing text import:

```python
from ors_render.elements import shapes as _shapes  # noqa: F401 - registers the renderers
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_shapes.py -v`, inspect `golden/shapes_basic.png`, then `uv run pytest packages/ors-render/tests/test_shapes.py -v`
Expected: PASS — 2 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): rect and line elements"
```

---

### Task 9: Ring and arc elements

The ring is the signature element — every screen in the current script uses it. It must match the existing look: gradient sweep from the top, optional dot cap centred on the stroke, dark track underneath.

**Files:**
- Create: `packages/ors-render/src/ors_render/elements/ring.py`
- Modify: `packages/ors-render/src/ors_render/render.py` (import to register; palette must be resolved from the element's own value)
- Test: `packages/ors-render/tests/test_ring.py`

**Interfaces:**
- Consumes: `Canvas`, `register`, `resolve_number`, `resolve_palette`, `gradient_color`
- Produces: renderers registered for `"ring"` and `"arc"`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_ring.py`:

```python
import pytest
from ors_schema.scene import Scene

from ors_render.context import RenderContext
from ors_render.render import render_scene

CTX = RenderContext(data={"prom": {"cpu": 42.4}})


def _ring(**overrides):
    element = {"type": "ring", "value": "{{prom.cpu}}", "palette": "cyan"}
    element.update(overrides)
    return Scene.model_validate({"elements": [element]})


def test_ring_draws_the_track_all_the_way_round():
    image = render_scene(_ring(value=0), CTX)
    # top of the ring: track colour, not background
    assert image.getpixel((120, 6)) != (0, 0, 0)


def test_ring_starts_at_twelve_oclock_and_sweeps_clockwise():
    image = render_scene(_ring(value=25), CTX)
    right = image.getpixel((234, 120))
    left = image.getpixel((6, 120))
    assert right != left, "a 25% clockwise sweep should reach 3 o'clock but not 9 o'clock"


@pytest.mark.parametrize("value", [0, 42, 100])
def test_ring_golden(assert_golden, value):
    assert_golden(render_scene(_ring(value=value, cap="dot"), CTX), f"ring_{value}")


def test_threshold_palette_changes_colour_with_value():
    palette = {
        "kind": "threshold",
        "thresholds": [
            {"at": 0, "palette": "green"},
            {"at": 70, "palette": "amber"},
            {"at": 90, "palette": "red"},
        ],
    }
    low = render_scene(_ring(value=10, palette=palette), CTX)
    high = render_scene(_ring(value=95, palette=palette), CTX)
    assert low.getpixel((120, 6)) != high.getpixel((120, 6))


def test_palette_token_in_text_uses_the_elements_palette(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "ring", "value": 80, "palette": "amber", "cap": "dot"},
                {"type": "text", "cy": 0.28, "size": 15, "text": "MEM", "color": "@palette"},
            ]
        }
    )
    assert_golden(render_scene(scene, CTX), "ring_palette_token")


def test_arc_renders_explicit_angles(assert_golden):
    scene = Scene.model_validate(
        {"elements": [{"type": "arc", "from_angle": -90, "to_angle": 90, "palette": "violet"}]}
    )
    assert_golden(render_scene(scene, CTX), "arc_half")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_ring.py -v`
Expected: FAIL — the first test fails because nothing is drawn.

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/elements/ring.py`:

```python
from __future__ import annotations

import math

from ors_schema.palette import GradientPalette
from ors_schema.scene import ArcElement, RingElement

from ors_render.bindings import resolve_number
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register
from ors_render.palettes import gradient_color, hex_to_rgb


def _sweep(canvas: Canvas, box: list[float], start: float, sweep: float, thickness: float,
           palette: GradientPalette) -> None:
    steps = max(1, int(abs(sweep) / 2))
    for i in range(steps):
        t = i / steps
        a0 = start + sweep * t
        a1 = start + sweep * (i + 1) / steps
        lo, hi = (a0, a1 + 1.2) if sweep >= 0 else (a1 - 1.2, a0)
        canvas.draw.arc(box, lo, hi, fill=gradient_color(palette, t), width=int(thickness))


@register("ring")
def render_ring(canvas: Canvas, element: RingElement, ctx: RenderContext, palette: GradientPalette) -> None:
    g = canvas.geometry
    cx, cy = g.x(element.cx), g.y(element.cy)
    radius, thickness = g.radial(element.r), g.radial(element.thickness)
    box = [cx - radius, cy - radius, cx + radius, cy + radius]

    if element.track:
        canvas.draw.arc(box, 0, 360, fill=hex_to_rgb(element.track), width=int(thickness))

    value = resolve_number(element.value, ctx.data)
    span = (element.max - element.min) or 1.0
    fraction = max(0.0, min(1.0, (value - element.min) / span))
    if fraction <= 0:
        return

    sweep = 360.0 * fraction * (1 if element.direction == "cw" else -1)
    _sweep(canvas, box, element.start_angle, sweep, thickness, palette)

    if element.cap == "dot":
        end = math.radians(element.start_angle + sweep)
        track_radius = radius - thickness / 2
        tx = cx + track_radius * math.cos(end)
        ty = cy + track_radius * math.sin(end)
        dot = thickness * 0.55
        canvas.draw.ellipse(
            [tx - dot, ty - dot, tx + dot, ty + dot], fill=gradient_color(palette, 1.0)
        )


@register("arc")
def render_arc(canvas: Canvas, element: ArcElement, ctx: RenderContext, palette: GradientPalette) -> None:
    g = canvas.geometry
    cx, cy = g.x(element.cx), g.y(element.cy)
    radius, thickness = g.radial(element.r), g.radial(element.thickness)
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    _sweep(canvas, box, element.from_angle, element.to_angle - element.from_angle, thickness, palette)
```

Update `draw_element` in `packages/ors-render/src/ors_render/render.py` so a ring's threshold palette is chosen from its own resolved value, and register the new module:

```python
from ors_render.bindings import resolve_number
from ors_render.elements import ring as _ring  # noqa: F401 - registers the renderers
from ors_render.palettes import resolve_palette


def draw_element(canvas: Canvas, element: object, ctx: RenderContext) -> None:
    when = getattr(element, "when", None)
    try:
        if not truthy(when, ctx.data):
            return
    except ExpressionError:
        return
    renderer = RENDERERS.get(getattr(element, "type", ""))
    if renderer is None:
        return

    palette_ref = getattr(element, "palette", None) or "mono"
    value_spec = getattr(element, "value", None)
    value_pct = resolve_number(value_spec, ctx.data) if value_spec is not None else 0.0
    palette = resolve_palette(palette_ref, value_pct)
    renderer(canvas, element, ctx, palette)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_ring.py -v`, **look at every generated PNG** (`ring_0`, `ring_42`, `ring_100`, `ring_palette_token`, `arc_half`) and confirm the sweep starts at 12 o'clock, runs clockwise, and the dot sits centred on the stroke — then `uv run pytest packages/ors-render/tests/test_ring.py -v`
Expected: PASS — all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): ring and arc elements with gradient sweep and dot cap"
```

---

### Task 10: Group element — nesting, `when`, `repeat`, `step`, `palettes`

**Files:**
- Create: `packages/ors-render/src/ors_render/elements/group.py`
- Modify: `packages/ors-render/src/ors_render/render.py` (import to register)
- Test: `packages/ors-render/tests/test_group.py`

**Interfaces:**
- Consumes: `draw_element` (from `render.py`), `resolve_list`, `RenderContext.child`
- Produces: renderer registered for `"group"`. Inside a repeat, the context gains the alias namespace (e.g. `t`) and an `index` key. `step` applies `field + delta * index` to every direct child that has that numeric field. `palettes[index % len]` overrides each child's palette.

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_group.py`:

```python
from ors_schema.scene import Scene

from ors_render.context import RenderContext
from ors_render.render import render_scene

CTX = RenderContext(
    data={
        "qbit": {
            "active": [
                {"name": "alpha", "progress": 91.0},
                {"name": "beta", "progress": 55.0},
                {"name": "gamma", "progress": 20.0},
                {"name": "delta", "progress": 5.0},
            ]
        }
    }
)


def test_group_renders_nested_elements():
    scene = Scene.model_validate(
        {
            "elements": [
                {"type": "group", "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}]}
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (255, 0, 0)


def test_group_when_false_skips_all_children():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "when": "len(qbit.active) > 99",
                    "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (0, 0, 0)


def test_repeat_honours_limit_and_exposes_alias_and_index(assert_golden):
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 3},
                    "step": {"r": -0.125, "thickness": -0.017},
                    "palettes": ["blue", "orange", "violet"],
                    "elements": [
                        {"type": "ring", "r": 0.858, "thickness": 0.083, "value": "{{t.progress}}",
                         "track": "#16181e"}
                    ],
                }
            ]
        }
    )
    assert_golden(render_scene(scene, CTX), "group_repeat_rings")


def test_repeat_over_empty_list_draws_nothing():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.nothing}}", "as": "t"},
                    "elements": [{"type": "rect", "w": 0.5, "h": 0.5, "fill": "#ff0000"}],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (0, 0, 0)


def test_index_is_available_inside_the_repeat():
    scene = Scene.model_validate(
        {
            "elements": [
                {
                    "type": "group",
                    "repeat": {"over": "{{qbit.active}}", "as": "t", "limit": 2},
                    "elements": [
                        {"type": "rect", "cy": 0.5, "w": 0.5, "h": 0.5, "fill": "#ff0000",
                         "when": "index == 1"}
                    ],
                }
            ]
        }
    )
    assert render_scene(scene, CTX).getpixel((120, 120)) == (255, 0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_group.py -v`
Expected: FAIL — nothing is drawn; first test fails.

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/elements/group.py`:

```python
from __future__ import annotations

from ors_schema.palette import GradientPalette
from ors_schema.scene import GroupElement

from ors_render.bindings import resolve_list
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register


def _stepped(child: object, step: dict[str, float], index: int, palette_override: object | None) -> object:
    if not step and palette_override is None:
        return child
    updates: dict[str, object] = {}
    for field, delta in step.items():
        current = getattr(child, field, None)
        if isinstance(current, int | float) and not isinstance(current, bool):
            updates[field] = current + delta * index
    if palette_override is not None and hasattr(child, "palette"):
        updates["palette"] = palette_override
    return child.model_copy(update=updates) if updates else child


@register("group")
def render_group(
    canvas: Canvas, element: GroupElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    from ors_render.render import draw_element

    if element.repeat is None:
        for child in element.elements:
            draw_element(canvas, child, ctx)
        return

    items = resolve_list(element.repeat.over, ctx.data)[: element.repeat.limit]
    for index, item in enumerate(items):
        child_ctx = ctx.child({element.repeat.as_: item, "index": index})
        override = element.palettes[index % len(element.palettes)] if element.palettes else None
        for child in element.elements:
            draw_element(canvas, _stepped(child, element.step, index, override), child_ctx)
```

In `packages/ors-render/src/ors_render/render.py`, add:

```python
from ors_render.elements import group as _group  # noqa: F401 - registers the renderer
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_group.py -v`, inspect `group_repeat_rings.png` (three concentric rings, decreasing radius, blue/orange/violet), then `uv run pytest packages/ors-render/tests/test_group.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): group element with when, repeat, step and palette cycling"
```

---

### Task 11: Sparkline and image elements

**Files:**
- Create: `packages/ors-render/src/ors_render/elements/media.py`
- Modify: `packages/ors-render/src/ors_render/render.py` (import to register)
- Test: `packages/ors-render/tests/test_media.py`

**Interfaces:**
- Consumes: `resolve_list`, `resolve_text`, `RenderContext.assets`
- Produces: renderers registered for `"sparkline"` and `"image"`. `image.src` resolves to a key in `ctx.assets`; a missing key renders nothing.

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_media.py`:

```python
import io

from ors_schema.scene import Scene
from PIL import Image

from ors_render.context import RenderContext
from ors_render.render import render_scene


def _asset() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_sparkline_renders(assert_golden):
    ctx = RenderContext(data={"h": {"cpu": [10, 40, 20, 80, 35, 90, 60, 75, 30]}})
    scene = Scene.model_validate(
        {"elements": [{"type": "sparkline", "values": "{{h.cpu}}", "w": 0.6, "h": 0.2,
                       "palette": "cyan", "fill": True}]}
    )
    assert_golden(render_scene(scene, ctx), "sparkline_basic")


def test_sparkline_with_fewer_than_two_points_draws_nothing():
    ctx = RenderContext(data={"h": {"cpu": [10]}})
    scene = Scene.model_validate({"elements": [{"type": "sparkline", "values": "{{h.cpu}}"}]})
    assert render_scene(scene, ctx).getpixel((120, 120)) == (0, 0, 0)


def test_image_draws_from_assets():
    ctx = RenderContext(data={}, assets={"logo": _asset()})
    scene = Scene.model_validate(
        {"elements": [{"type": "image", "src": "logo", "w": 0.5, "h": 0.5, "fit": "stretch"}]}
    )
    assert render_scene(scene, ctx).getpixel((120, 120)) == (255, 0, 0)


def test_missing_asset_renders_nothing():
    ctx = RenderContext(data={}, assets={})
    scene = Scene.model_validate({"elements": [{"type": "image", "src": "nope", "w": 0.5, "h": 0.5}]})
    assert render_scene(scene, ctx).getpixel((120, 120)) == (0, 0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_media.py -v`
Expected: FAIL — `test_image_draws_from_assets` fails on `(0, 0, 0) != (255, 0, 0)`.

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/elements/media.py`:

```python
from __future__ import annotations

import io

from ors_schema.palette import GradientPalette
from ors_schema.scene import ImageElement, SparklineElement
from PIL import Image

from ors_render.bindings import resolve_list, resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register
from ors_render.palettes import gradient_color


@register("sparkline")
def render_sparkline(
    canvas: Canvas, element: SparklineElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    raw = resolve_list(element.values, ctx.data)
    points = [float(v) for v in raw if isinstance(v, int | float) and not isinstance(v, bool)]
    if len(points) < 2:
        return

    g = canvas.geometry
    width, height = g.span(element.w), g.span(element.h)
    left, top = g.x(element.cx) - width / 2, g.y(element.cy) - height / 2
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    step = width / (len(points) - 1)
    coords = [(left + i * step, top + height - (v - low) / span * height) for i, v in enumerate(points)]

    if element.fill:
        polygon = [*coords, (coords[-1][0], top + height), (coords[0][0], top + height)]
        canvas.draw.polygon(polygon, fill=gradient_color(palette, 0.25))
    canvas.draw.line(coords, fill=gradient_color(palette, 1.0), width=max(1, int(g.span(0.008))), joint="curve")


@register("image")
def render_image(
    canvas: Canvas, element: ImageElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    key = resolve_text(element.src, ctx.data)
    blob = ctx.assets.get(key)
    if not blob:
        return

    g = canvas.geometry
    width, height = int(g.span(element.w)), int(g.span(element.h))
    if width <= 0 or height <= 0:
        return

    source = Image.open(io.BytesIO(blob)).convert("RGBA")
    if element.fit == "stretch":
        source = source.resize((width, height), Image.LANCZOS)
    elif element.fit == "contain":
        source.thumbnail((width, height), Image.LANCZOS)
    else:  # cover
        scale = max(width / source.width, height / source.height)
        source = source.resize((int(source.width * scale), int(source.height * scale)), Image.LANCZOS)
        left = (source.width - width) // 2
        top = (source.height - height) // 2
        source = source.crop((left, top, left + width, top + height))

    x = int(g.x(element.cx) - source.width / 2)
    y = int(g.y(element.cy) - source.height / 2)
    canvas.image.paste(source, (x, y), source)
```

In `packages/ors-render/src/ors_render/render.py`, add:

```python
from ors_render.elements import media as _media  # noqa: F401 - registers the renderers
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_media.py -v`, inspect `sparkline_basic.png`, then `uv run pytest packages/ors-render/tests/test_media.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): sparkline and image elements"
```

---

### Task 12: Scene selection and the public render API

**Files:**
- Modify: `packages/ors-render/src/ors_render/render.py`
- Modify: `packages/ors-render/src/ors_render/__init__.py`
- Test: `packages/ors-render/tests/test_render_screen.py`

**Interfaces:**
- Consumes: `render_scene`, `truthy`
- Produces:
  - `select_scene(scenes: Sequence[Scene], ctx: RenderContext) -> Scene | None` — first scene whose `when` passes; a scene with no `when` always passes
  - `render_screen(scenes: Sequence[Scene], ctx: RenderContext, size: int = 240, supersample: int = 2) -> Image.Image` — renders the selected scene, or a blank panel when nothing matches
  - Both re-exported from `ors_render`, alongside `render_scene`, `RenderContext`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_render_screen.py`:

```python
from ors_schema.scene import Scene

from ors_render import RenderContext, render_screen, select_scene

HEALTHY = RenderContext(
    data={"prom": {"nodes_ready": 3, "nodes_total": 3, "alerts": 0}, "qbit": {"active": [{"progress": 50}]}}
)
DEGRADED = RenderContext(
    data={"prom": {"nodes_ready": 2, "nodes_total": 3, "alerts": 1}, "qbit": {"active": []}}
)

SCENES = [
    Scene.model_validate(
        {
            "name": "downloads",
            "when": "prom.nodes_ready == prom.nodes_total and prom.alerts == 0 and len(qbit.active) > 0",
            "elements": [{"type": "text", "size": 30, "text": "DL"}],
        }
    ),
    Scene.model_validate({"name": "nodes", "elements": [{"type": "text", "size": 30, "text": "NODES"}]}),
]


def test_first_matching_scene_wins():
    assert select_scene(SCENES, HEALTHY).name == "downloads"


def test_falls_through_to_the_unconditional_scene():
    assert select_scene(SCENES, DEGRADED).name == "nodes"


def test_no_match_returns_none():
    only_conditional = [Scene.model_validate({"name": "x", "when": "1 == 2", "elements": []})]
    assert select_scene(only_conditional, HEALTHY) is None


def test_broken_condition_is_treated_as_not_matching():
    scenes = [
        Scene.model_validate({"name": "broken", "when": "__import__('os')", "elements": []}),
        Scene.model_validate({"name": "ok", "elements": []}),
    ]
    assert select_scene(scenes, HEALTHY).name == "ok"


def test_render_screen_picks_the_right_scene():
    healthy = render_screen(SCENES, HEALTHY)
    degraded = render_screen(SCENES, DEGRADED)
    assert healthy.size == (240, 240)
    assert healthy.tobytes() != degraded.tobytes()


def test_render_screen_with_no_match_returns_a_blank_panel():
    only_conditional = [Scene.model_validate({"name": "x", "when": "1 == 2", "elements": []})]
    image = render_screen(only_conditional, HEALTHY)
    assert image.size == (240, 240)
    assert image.getpixel((120, 120)) == (0, 0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_render_screen.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_screen' from 'ors_render'`

- [ ] **Step 3: Write minimal implementation**

Append to `packages/ors-render/src/ors_render/render.py`:

```python
from collections.abc import Sequence


def select_scene(scenes: Sequence[Scene], ctx: RenderContext) -> Scene | None:
    for scene in scenes:
        try:
            if truthy(scene.when, ctx.data):
                return scene
        except ExpressionError:
            continue
    return None


def render_screen(
    scenes: Sequence[Scene], ctx: RenderContext, size: int = 240, supersample: int = 2
) -> Image.Image:
    scene = select_scene(scenes, ctx)
    if scene is None:
        return Canvas(Geometry(size=size, supersample=supersample), "#000000").finish()
    return render_scene(scene, ctx, size=size, supersample=supersample)
```

Replace `packages/ors-render/src/ors_render/__init__.py` with:

```python
from ors_render.context import RenderContext
from ors_render.geometry import Geometry
from ors_render.render import render_scene, render_screen, select_scene

__version__ = "0.1.0"

__all__ = ["Geometry", "RenderContext", "render_scene", "render_screen", "select_scene"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/ors-render/tests -v`
Expected: PASS — the whole render suite passes.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-render
git commit -m "feat(render): scene selection and public render_screen API"
```

---

### Task 13: Built-in templates and parity goldens

The acceptance test for M1: every screen the current `k8s_monitor.py` draws is reproduced from JSON.

**Files:**
- Create: `packages/ors-render/src/ors_render/templates/__init__.py`
- Create: `packages/ors-render/src/ors_render/templates/builtin/ring-gauge.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/big-number.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/multi-ring.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/node-health.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/torrent.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/text-only.json`
- Create: `packages/ors-render/src/ors_render/templates/builtin/system.json`
- Test: `packages/ors-render/tests/test_builtin_templates.py`

**Interfaces:**
- Consumes: `Template`, `render_screen`
- Produces:
  - `load_builtin_templates() -> dict[str, Template]` — keyed by template name, cached
  - `BUILTIN_DIR: Path`
  - System scenes live in `system.json` as a template named `system` whose scenes are named `stale`, `connecting`, `error`, `identify`

- [ ] **Step 1: Write the failing test**

`packages/ors-render/tests/test_builtin_templates.py`:

```python
import pytest

from ors_render import RenderContext, render_screen
from ors_render.templates import load_builtin_templates

EXPECTED = {"ring-gauge", "big-number", "multi-ring", "node-health", "torrent", "text-only", "system"}

PROM = {
    "cpu": 42.4,
    "mem": 61.2,
    "mem_used_gb": 19.4,
    "mem_total_gb": 32.0,
    "cpu_hot": {"node": ".5", "value": 71.2},
    "mem_hot": {"node": ".7", "value": 78.0},
    "pods_run": 38,
    "pods_tot": 41,
    "pods_pend": 1,
    "pods_fail": 0,
    "nodes_ready": 3,
    "nodes_total": 3,
    "alerts": 0,
}
QBIT = {
    "active": [
        {"name": "alpha-release-2160p", "progress": 91.2, "eta": 1112, "speed": 4613734},
        {"name": "beta", "progress": 55.0, "eta": 3300, "speed": 1200000},
        {"name": "gamma", "progress": 20.0, "eta": 9000, "speed": 400000},
    ],
    "min_eta": 1112,
    "total_speed": 6213734,
    "count": 3,
}


def test_all_builtin_templates_load_and_validate():
    templates = load_builtin_templates()
    assert set(templates) == EXPECTED


def test_every_declared_param_has_a_label():
    for template in load_builtin_templates().values():
        for name, spec in template.params_schema.items():
            assert spec.label, f"{template.name}.{name} has no label"


@pytest.mark.parametrize(
    ("golden", "template_name", "params", "data"),
    [
        (
            "screen_cpu",
            "ring-gauge",
            {"title": "CPU", "value": "{{prom.cpu}}", "big": "{{prom.cpu | round:0}}%",
             "subtitle": "cluster avg", "palette": "cyan",
             "hint": "peak: {{prom.cpu_hot.node}} {{prom.cpu_hot.value | round:0}}%"},
            {"prom": PROM},
        ),
        (
            "screen_mem",
            "ring-gauge",
            {"title": "MEM", "value": "{{prom.mem}}", "big": "{{prom.mem | round:0}}%",
             "subtitle": "{{prom.mem_used_gb | round:1}} / {{prom.mem_total_gb | round:0}} G",
             "palette": "green",
             "hint": "peak: {{prom.mem_hot.node}} {{prom.mem_hot.value | round:0}}%"},
            {"prom": PROM},
        ),
        (
            "screen_pods",
            "big-number",
            {"title": "PODS", "value": "{{prom.pods_run / prom.pods_tot * 100}}",
             "big": "{{prom.pods_run}}", "subtitle": "/ {{prom.pods_tot}} total", "palette": "lime",
             "hint": ""},
            {"prom": PROM},
        ),
        (
            "screen_nodes",
            "node-health",
            {"title": "NODES"},
            {"prom": PROM},
        ),
        (
            "screen_torrent",
            "torrent",
            {"title": "TORRENT"},
            {"qbit": QBIT},
        ),
    ],
)
def test_builtin_templates_reproduce_the_original_screens(assert_golden, golden, template_name, params, data):
    template = load_builtin_templates()[template_name]
    ctx = RenderContext(data={**data, "params": params})
    assert_golden(render_screen(template.scenes, ctx), golden)


def test_health_template_switches_to_downloads_when_healthy_and_downloading():
    templates = load_builtin_templates()
    scenes = templates["node-health"].scenes + templates["torrent"].scenes
    from ors_render import select_scene

    healthy = RenderContext(data={"prom": PROM, "qbit": QBIT, "params": {}})
    degraded = RenderContext(
        data={"prom": {**PROM, "alerts": 2}, "qbit": {"active": [], "count": 0}, "params": {}}
    )
    assert select_scene(scenes[::-1], healthy).name == "downloads"
    assert select_scene(scenes[::-1], degraded).name == "nodes"


@pytest.mark.parametrize("scene_name", ["stale", "connecting", "error", "identify"])
def test_system_scenes_render(assert_golden, scene_name):
    system = load_builtin_templates()["system"]
    scene = next(s for s in system.scenes if s.name == scene_name)
    ctx = RenderContext(data={"params": {"message": "prometheus timeout", "ordinal": "2"}})
    assert_golden(render_screen([scene.model_copy(update={"when": None})], ctx), f"system_{scene_name}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/ors-render/tests/test_builtin_templates.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ors_render.templates'`

- [ ] **Step 3: Write minimal implementation**

`packages/ors-render/src/ors_render/templates/__init__.py`:

```python
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ors_schema.scene import Template

BUILTIN_DIR = Path(__file__).parent / "builtin"


@lru_cache(maxsize=1)
def load_builtin_templates() -> dict[str, Template]:
    templates: dict[str, Template] = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        template = Template.model_validate(json.loads(path.read_text()))
        templates[template.name] = template
    return templates
```

`packages/ors-render/src/ors_render/templates/builtin/ring-gauge.json`:

```json
{
  "name": "ring-gauge",
  "category": "gauge",
  "builtin": true,
  "params_schema": {
    "title":    { "type": "string",  "label": "Title",     "default": "CPU" },
    "value":    { "type": "binding", "label": "Value 0-100", "default": "0" },
    "big":      { "type": "binding", "label": "Centre text", "default": "0%" },
    "subtitle": { "type": "binding", "label": "Subtitle",  "default": "" },
    "hint":     { "type": "binding", "label": "Hint line", "default": "" },
    "palette":  { "type": "palette", "label": "Palette",   "default": "cyan" }
  },
  "scenes": [
    {
      "name": "default",
      "background": "#000000",
      "elements": [
        { "type": "ring", "r": 0.875, "thickness": 0.092, "cap": "dot",
          "value": "{{params.value}}", "palette": "{{params.palette}}", "track": "#16181e" },
        { "type": "text", "cy": 0.275, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "@palette" },
        { "type": "text", "cy": 0.517, "size": 52, "font": "bold", "text": "{{params.big}}" },
        { "type": "text", "cy": 0.708, "size": 13, "font": "bold",
          "text": "{{params.subtitle}}", "color": "#8c96a5" },
        { "type": "text", "cy": 0.8, "size": 10, "font": "bold",
          "text": "{{params.hint}}", "color": "#555f69" }
      ]
    }
  ]
}
```

**Note for the implementer:** `"palette": "{{params.palette}}"` is a binding in a `PaletteRef` field. `resolve_palette` receives the raw string, so binding resolution must happen before palette resolution. Add this to `draw_element` in `render.py` immediately before the palette is resolved:

```python
    if isinstance(palette_ref, str) and "{{" in palette_ref:
        from ors_render.bindings import resolve

        resolved = resolve(palette_ref, ctx.data)
        palette_ref = resolved if resolved else "mono"
```

Write a test for exactly that in this task's test file:

```python
def test_palette_can_come_from_a_binding():
    from ors_schema.scene import Scene

    scene = Scene.model_validate(
        {"elements": [{"type": "ring", "value": 100, "palette": "{{params.palette}}", "track": None}]}
    )
    cyan = render_screen([scene], RenderContext(data={"params": {"palette": "cyan"}}))
    red = render_screen([scene], RenderContext(data={"params": {"palette": "red"}}))
    assert cyan.getpixel((120, 6)) != red.getpixel((120, 6))
```

`packages/ors-render/src/ors_render/templates/builtin/big-number.json` — same shape as `ring-gauge`, but the centre text dominates and there is no hint line:

```json
{
  "name": "big-number",
  "category": "gauge",
  "builtin": true,
  "params_schema": {
    "title":    { "type": "string",  "label": "Title",       "default": "PODS" },
    "value":    { "type": "binding", "label": "Ring 0-100",  "default": "0" },
    "big":      { "type": "binding", "label": "Centre text", "default": "0" },
    "subtitle": { "type": "binding", "label": "Subtitle",    "default": "" },
    "hint":     { "type": "binding", "label": "Hint line",   "default": "" },
    "palette":  { "type": "palette", "label": "Palette",     "default": "lime" }
  },
  "scenes": [
    {
      "name": "default",
      "background": "#000000",
      "elements": [
        { "type": "ring", "r": 0.875, "thickness": 0.092, "cap": "dot",
          "value": "{{params.value}}", "palette": "{{params.palette}}", "track": "#16181e" },
        { "type": "text", "cy": 0.275, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "@palette" },
        { "type": "text", "cy": 0.517, "size": 52, "font": "bold", "text": "{{params.big}}" },
        { "type": "text", "cy": 0.708, "size": 13, "font": "bold",
          "text": "{{params.subtitle}}", "color": "#8c96a5" },
        { "type": "text", "cy": 0.8, "size": 10, "font": "bold",
          "text": "{{params.hint}}", "color": "#555f69" }
      ]
    }
  ]
}
```

`packages/ors-render/src/ors_render/templates/builtin/multi-ring.json`:

```json
{
  "name": "multi-ring",
  "category": "gauge",
  "builtin": true,
  "params_schema": {
    "title":  { "type": "string",  "label": "Title",       "default": "RINGS" },
    "items":  { "type": "binding", "label": "List binding", "default": "" },
    "field":  { "type": "string",  "label": "Progress field", "default": "progress" },
    "big":    { "type": "binding", "label": "Centre text", "default": "" },
    "subtitle": { "type": "binding", "label": "Subtitle",  "default": "" }
  },
  "scenes": [
    {
      "name": "default",
      "background": "#000000",
      "elements": [
        { "type": "group",
          "repeat": { "over": "{{params.items}}", "as": "item", "limit": 3 },
          "step": { "r": -0.125, "thickness": -0.017 },
          "palettes": ["blue", "orange", "violet"],
          "elements": [
            { "type": "ring", "r": 0.858, "thickness": 0.083,
              "value": "{{item.progress}}", "track": "#16181e" }
          ] },
        { "type": "text", "cy": 0.24, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "#38bdf8" },
        { "type": "text", "cy": 0.517, "size": 36, "font": "bold", "text": "{{params.big}}" },
        { "type": "text", "cy": 0.7, "size": 13, "font": "bold",
          "text": "{{params.subtitle}}", "color": "#8c96a5" }
      ]
    }
  ]
}
```

`packages/ors-render/src/ors_render/templates/builtin/node-health.json` — one scene named `nodes`, with a threshold-free palette chosen by `when`-gated duplicates would be wasteful, so it uses a threshold palette on readiness percentage:

```json
{
  "name": "node-health",
  "category": "status",
  "builtin": true,
  "params_schema": {
    "title": { "type": "string", "label": "Title", "default": "NODES" },
    "hint":  { "type": "binding", "label": "Hint line", "default": "" }
  },
  "scenes": [
    {
      "name": "nodes",
      "background": "#000000",
      "elements": [
        { "type": "ring", "r": 0.875, "thickness": 0.092, "cap": "dot", "track": "#16181e",
          "value": "{{prom.nodes_ready / prom.nodes_total * 100}}",
          "palette": { "kind": "threshold", "thresholds": [
            { "at": 0,   "palette": "red" },
            { "at": 60,  "palette": "amber" },
            { "at": 100, "palette": "green" } ] } },
        { "type": "text", "cy": 0.275, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "@palette" },
        { "type": "text", "cy": 0.517, "size": 52, "font": "bold",
          "text": "{{prom.nodes_ready}}/{{prom.nodes_total}}" },
        { "type": "text", "cy": 0.708, "size": 13, "font": "bold", "color": "#8c96a5",
          "text": "ALL OK", "when": "prom.nodes_ready == prom.nodes_total and prom.alerts == 0" },
        { "type": "text", "cy": 0.708, "size": 13, "font": "bold", "color": "#8c96a5",
          "text": "{{prom.alerts}} ALERTS", "when": "prom.alerts > 0" },
        { "type": "text", "cy": 0.708, "size": 13, "font": "bold", "color": "#8c96a5",
          "text": "{{prom.nodes_total - prom.nodes_ready}} NOT READY",
          "when": "prom.alerts == 0 and prom.nodes_ready < prom.nodes_total" },
        { "type": "text", "cy": 0.8, "size": 10, "font": "bold",
          "text": "{{params.hint}}", "color": "#555f69" }
      ]
    }
  ]
}
```

`packages/ors-render/src/ors_render/templates/builtin/torrent.json` — the scene is named `downloads` and carries the `when` that makes a screen auto-switch to it:

```json
{
  "name": "torrent",
  "category": "status",
  "builtin": true,
  "params_schema": {
    "title": { "type": "string", "label": "Title", "default": "TORRENT" }
  },
  "scenes": [
    {
      "name": "downloads",
      "when": "len(qbit.active) > 0",
      "background": "#000000",
      "elements": [
        { "type": "group",
          "repeat": { "over": "{{qbit.active}}", "as": "t", "limit": 3 },
          "step": { "r": -0.125, "thickness": -0.017 },
          "palettes": ["blue", "orange", "violet"],
          "elements": [
            { "type": "ring", "r": 0.858, "thickness": 0.083,
              "value": "{{t.progress}}", "track": "#16181e" } ] },
        { "type": "text", "cy": 0.24, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "#38bdf8" },
        { "type": "text", "cy": 0.517, "size": 36, "font": "bold",
          "text": "{{qbit.min_eta | duration}}" },
        { "type": "text", "cy": 0.7, "size": 13, "font": "bold", "color": "#8c96a5",
          "text": "{{qbit.total_speed | bytes}}/s" },
        { "type": "text", "cy": 0.783, "size": 10, "font": "bold", "color": "#3f6f8f",
          "text": "{{qbit.active[0].name | trunc:10}}" }
      ]
    }
  ]
}
```

`packages/ors-render/src/ors_render/templates/builtin/text-only.json`:

```json
{
  "name": "text-only",
  "category": "basic",
  "builtin": true,
  "params_schema": {
    "title":    { "type": "string",  "label": "Title",       "default": "" },
    "big":      { "type": "binding", "label": "Centre text", "default": "" },
    "subtitle": { "type": "binding", "label": "Subtitle",    "default": "" },
    "color":    { "type": "color",   "label": "Centre colour", "default": "#ffffff" }
  },
  "scenes": [
    {
      "name": "default",
      "background": "#000000",
      "elements": [
        { "type": "text", "cy": 0.29, "size": 15, "font": "bold",
          "text": "{{params.title}}", "color": "#8c96a5" },
        { "type": "text", "cy": 0.5, "size": 46, "font": "bold",
          "text": "{{params.big}}", "color": "{{params.color}}" },
        { "type": "text", "cy": 0.72, "size": 13, "font": "bold",
          "text": "{{params.subtitle}}", "color": "#8c96a5" }
      ]
    }
  ]
}
```

**Note:** `"color": "{{params.color}}"` means `Color` fields must also accept bindings. `resolve_color` already handles the binding case (Task 7), but the schema pattern rejects it — widen `Color` in `ors_schema/palette.py` to `^(#[0-9a-fA-F]{6}|@palette|\{\{.*\}\})$`. Add tests: `packages/ors-schema/tests/test_palette.py` gains

```python
def test_binding_is_a_legal_color():
    assert Holder(color="{{params.color}}", palette="cyan").color == "{{params.color}}"
```

and `packages/ors-render/tests/test_text.py` gains

```python
def test_bound_color_is_resolved():
    scene = Scene.model_validate(
        {"elements": [{"type": "rect", "w": 1.0, "h": 1.0, "fill": "{{params.color}}"}]}
    )
    image = render_scene(scene, RenderContext(data={"params": {"color": "#00ff00"}}))
    assert image.getpixel((120, 120)) == (0, 255, 0)
```

`packages/ors-render/src/ors_render/templates/builtin/system.json`:

```json
{
  "name": "system",
  "category": "system",
  "builtin": true,
  "params_schema": {
    "message": { "type": "string", "label": "Error message", "default": "" },
    "ordinal": { "type": "string", "label": "Identify digit", "default": "1" }
  },
  "scenes": [
    { "name": "connecting", "background": "#000000", "elements": [
      { "type": "ring", "r": 0.875, "thickness": 0.092, "value": 0, "track": "#12121e" },
      { "type": "text", "cy": 0.517, "size": 52, "font": "bold", "text": "WAIT", "color": "#325aa0" },
      { "type": "text", "cy": 0.708, "size": 13, "font": "bold", "text": "connecting", "color": "#284682" } ] },

    { "name": "stale", "background": "#000000", "elements": [
      { "type": "ring", "r": 0.875, "thickness": 0.092, "value": 0, "track": "#151515" },
      { "type": "text", "cy": 0.375, "size": 52, "font": "bold", "text": "NO", "color": "#414141" },
      { "type": "text", "cy": 0.625, "size": 52, "font": "bold", "text": "DATA", "color": "#414141" } ] },

    { "name": "error", "background": "#000000", "elements": [
      { "type": "ring", "r": 0.875, "thickness": 0.092, "value": 100, "palette": "red", "track": "#1a0d0d" },
      { "type": "text", "cy": 0.4, "size": 34, "font": "bold", "text": "ERR", "color": "#ff5252" },
      { "type": "text", "cy": 0.62, "size": 11, "font": "bold", "max_width": 0.7,
        "text": "{{params.message}}", "color": "#8c96a5" } ] },

    { "name": "identify", "background": "#000000", "elements": [
      { "type": "ring", "r": 0.875, "thickness": 0.092, "value": 100, "palette": "cyan", "track": "#16181e" },
      { "type": "text", "cy": 0.5, "size": 110, "font": "bold", "text": "{{params.ordinal}}" } ] }
  ]
}
```

Ship the JSON in the wheel — the `force-include` of `src/ors_render/assets` added in Task 6 does not cover `templates/builtin`. Extend `packages/ors-render/pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/ors_render/assets" = "ors_render/assets"
"src/ors_render/templates/builtin" = "ors_render/templates/builtin"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UPDATE_GOLDEN=1 uv run pytest packages/ors-render/tests/test_builtin_templates.py -v`

Then **open every generated golden and compare it against a photo or screenshot of the current rack**: `screen_cpu`, `screen_mem`, `screen_pods`, `screen_nodes`, `screen_torrent`, `system_stale`, `system_connecting`, `system_error`, `system_identify`. They must look like the existing screens. If they do not, fix the template JSON — not the golden.

Then run the whole suite: `uv run pytest -v && uv run ruff check . && uv run ruff format --check .`
Expected: PASS — everything green.

- [ ] **Step 5: Commit**

```bash
git add packages
git commit -m "feat(render): built-in templates reproducing all four rack screens"
```

---

## Definition of done for M1

- `uv run pytest` passes from a clean checkout on Linux with no hardware.
- `uv run ruff check .` and `uv run ruff format --check .` pass.
- CI is green on GitHub Actions.
- Golden images exist and have been visually confirmed for: text, shapes, rings (0/42/100), palette token, arc, repeated rings, sparkline, all four rack screens, and all four system scenes.
- `ors_render` exposes `render_scene`, `render_screen`, `select_scene`, `RenderContext`, `Geometry`.
- `ors_render.templates.load_builtin_templates()` returns seven validated templates.
- Nothing in either package imports from `server/` or `daemon/`, performs network I/O, or reads the clock.

## What M2 picks up

M2 (standalone daemon) consumes exactly these interfaces: `load_builtin_templates()`, `render_screen(scenes, ctx, size, supersample)`, `RenderContext(data=..., assets=...)`, and the `ors_schema` models. It adds the display backends, the poller, the screen workers, night mode and a local config file. No M1 code changes should be needed — if M2 finds it needs one, that is a signal the interface was wrong and should be raised, not patched around.
