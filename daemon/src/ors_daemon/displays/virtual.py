from __future__ import annotations

import os
from pathlib import Path

from PIL import Image


class VirtualDisplay:
    """Writes what would have gone to glass, so the daemon runs with no hardware."""

    def __init__(self, out_dir: Path, name: str) -> None:
        self._path = Path(out_dir) / f"{name}.png"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.asleep = False

    def show(self, image: Image.Image) -> None:
        # Written beside the target and renamed, because whatever is watching the
        # directory -- an editor preview, the M3 server, a browser reloading it --
        # otherwise reads a half-written PNG on the frame it happens to catch.
        # `os.replace` is atomic within a filesystem, and both paths are in one.
        temporary = self._path.with_suffix(".png.tmp")
        image.save(temporary, format="PNG")
        os.replace(temporary, self._path)
        self.frames += 1

    def sleep(self) -> None:
        self.asleep = True

    def wake(self) -> None:
        self.asleep = False

    def close(self) -> None:
        pass
