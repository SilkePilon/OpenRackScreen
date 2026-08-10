from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PALETTE_TOKEN = "@palette"

Color = Annotated[str, Field(pattern=r"^(#[0-9a-fA-F]{6}|@palette|\{\{.*\}\})$")]
"""A `#rrggbb` color, the literal `@palette`, or a binding resolving to either.

A binding is admitted because a colour is a *parameter* of a template as much as
a title is -- `text-only` writes `"color": "{{params.color}}"` -- and the value
only exists at render time. What the binding resolves to is not this pattern's
business: `ors_render.elements.resolve_color` reads the result through the same
literal parser as any other colour and falls back rather than raising, so a
binding pointing at nothing renders white instead of taking a panel down.
"""


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


PaletteRef = str | Annotated[GradientPalette | ThresholdPalette, Field(discriminator="kind")]

ThresholdEntry.model_rebuild()
