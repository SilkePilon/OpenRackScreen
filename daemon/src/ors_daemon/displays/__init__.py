from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ors_schema.daemon import DisplayConfig
from PIL import Image


class DisplayError(Exception):
    """A backend could not be built or could not write to its panel."""


@runtime_checkable
class DisplayBackend(Protocol):
    """Pure transport: a finished panel image goes in, nothing comes back.

    Rotation and h-flip are applied by the screen worker *before* `show`, so a
    virtual backend cannot show something different from the glass.

    `show` takes a whole frame and says nothing about what reaches the bus, so a
    backend is free to diff against the frame it last sent and address only the
    rows that changed. Nothing here would have to change for that; a caller
    cannot tell, and must not assume, that a `show` costs a full panel write.
    """

    def show(self, image: Image.Image) -> None: ...

    def sleep(self) -> None: ...

    def wake(self) -> None: ...

    def close(self) -> None: ...


def build_display(config: DisplayConfig, name: str) -> DisplayBackend:
    if config.backend == "virtual":
        from ors_daemon.displays.virtual import VirtualDisplay

        return VirtualDisplay(Path(config.out_dir or "."), name)

    # `gc9a01` imports nothing optional at module scope -- that is what keeps
    # `pack565` testable on CI -- so there is no import to guard here. luma is
    # reached inside the constructor, and the missing-extra message is raised
    # from there, as a `DisplayError` like every other failure to open a panel.
    from ors_daemon.displays.gc9a01 import GC9A01Display

    # The schema's validator rejects a `gc9a01` display with either pin unset,
    # so by here both are ints; the assert is only how that reaches a reader and
    # a type checker. A runtime `raise` would be a branch no test could ever
    # take, and a `cast` would assert it without checking anything.
    assert config.dc is not None and config.rst is not None

    return GC9A01Display(
        spi_bus=config.spi_bus, spi_cs=config.spi_cs, dc=config.dc, rst=config.rst, hz=config.hz
    )
