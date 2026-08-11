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
