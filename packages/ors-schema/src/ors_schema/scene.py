from __future__ import annotations

from collections.abc import Mapping
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
    # NOT IMPLEMENTED as of M1: validated here and read by no renderer, so an
    # element with `"opacity": 0.2` draws at full strength and an `"opacity": 0`
    # one is fully visible rather than hidden. Kept in the schema because the
    # field is part of the documented scene format and dropping it would reject
    # scenes that already carry it -- but anything reaching for it (M2's night
    # mode is the obvious candidate) has to implement it in the element
    # renderers first, since nothing in the render path composites today.
    # `when` is the only way a scene can currently hide an element.
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
    # What `"color": "@palette"` resolves against, so a title can track its
    # gauge's colour: a template sets the same palette on the ring and on the
    # label. `mono` is the renderer's own fallback for a palette-less element,
    # so a text element that names none renders exactly as it always has.
    palette: PaletteRef = "mono"
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

    def bind_params(self, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """This template's declared defaults, overlaid with a screen's own values.

        The one bridge between `params_schema`, which is where a default is
        written, and ``ctx.data["params"]``, which is what a renderer reads:

            render_screen(template.scenes, RenderContext(data={
                "prom": readings, "params": template.bind_params(screen.params)
            }))

        It lives on the model rather than in the renderer because all three
        consumers need it -- the daemon rendering to a panel, the server
        rendering a preview, the tests rendering a golden -- and a merge written
        three times is three chances to disagree about an edge. Three edges, each
        decided here once:

        *An override naming a parameter the template does not declare is kept.*
        `params_schema` is what an editor offers, not a filter on what a scene
        may read: a user-edited template can draw ``{{params.extra}}`` without
        having declared it, and dropping the value would blank a field that
        renders today. A stray key costs nothing -- no scene reads it.

        *A parameter declared with no default appears anyway, as ``None``.*
        `ParamSpec.default` is `Any`, so ``None`` is both "no default written"
        and a default someone could write, and there is no third state to
        distinguish them by. The renderer treats an absent key and a ``None`` one
        identically -- both resolve to nothing and both take a ``| default:``
        filter -- so including it costs nothing and makes the result total: every
        declared name is present.

        *An override of the wrong type passes through unchanged.* `ParamSpec.type`
        is metadata for the editor's form controls, and the renderer already
        degrades per field -- an unusable number falls back to the element's own,
        an unusable colour renders white. Coercing or refusing here would decide
        that with strictly less context than the field has.

        The result is a fresh `dict` that shares nothing with the template, which
        matters because `ors_render.templates.load_builtin_templates` caches: one
        `Template` is handed to every caller, and a result aliasing part of it
        would let one screen's parameters leak into the next frame's.
        """
        bound = {name: spec.default for name, spec in self.params_schema.items()}
        bound.update(overrides or {})
        return bound
