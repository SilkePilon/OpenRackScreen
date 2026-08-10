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
