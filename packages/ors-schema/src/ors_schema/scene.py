from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ors_schema.palette import Color, PaletteRef

Binding = str
"""A string that may contain `{{ ... }}` binding expressions."""

NumberSpec = float | int | str
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
    RingElement
    | ArcElement
    | TextElement
    | RectElement
    | LineElement
    | ImageElement
    | SparklineElement
    | GroupElement,
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
