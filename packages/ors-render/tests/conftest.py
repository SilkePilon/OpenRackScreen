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
        assert path.exists(), (
            f"missing golden {path}; regenerate with UPDATE_GOLDEN=1 uv run pytest"
        )
        reference = Image.open(path).convert("RGB")
        assert image.size == reference.size, f"size {image.size} != golden {reference.size}"
        diff = ImageChops.difference(image.convert("RGB"), reference)
        mean = max(ImageStat.Stat(diff).mean)
        assert mean <= tolerance, (
            f"golden {name} differs by mean {mean:.2f} (tolerance {tolerance})"
        )

    return _assert
