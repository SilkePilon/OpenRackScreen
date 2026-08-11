from __future__ import annotations

import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

from PIL import Image

from ors_daemon.displays import DisplayError

_numpy: ModuleType | None
try:  # numpy ships with the `hardware` extra and is absent on x86 CI
    import numpy as _numpy
except ImportError:  # pragma: no cover - the loop path is tested by patching `_numpy`
    _numpy = None

SerialFactory = Callable[..., Any]
"""Builds the object that owns the bus: luma's `spi`, or a fake under test."""

# Carried over byte-for-byte from the `k8s_monitor.py` this project replaces.
# That script is not in this repository and this is the only copy, so it is
# authoritative; it is known-working on this exact hardware. It is not to be
# "cleaned up". Most of these registers are undocumented -- the GC9A01
# datasheet describes 0x36, 0x3A, 0xB6, 0x21, 0x35 and 0x11 and stops there, and
# the rest (0x84..0x98, 0xE8, 0x62..0x74) are the vendor's power, gate and
# gamma trim, published only as a sequence in reference code. There is no symbol
# to give them and no way to derive one, so the bytes stand as they are.
#
# Each entry is a command byte followed by its parameters. Ordering matters
# throughout, and the tail especially: 0x21 (inversion on -- this panel's
# pixels are wired inverted), then 0x11 (sleep out), which starts the booster
# and needs its own delay before anything else is sent. Display on (0x29) is
# deliberately *not* in the table: it belongs after that delay.
_INIT: list[tuple[int, ...]] = [
    (0xEF,), (0xEB, 0x14), (0xFE,), (0xEF,), (0xEB, 0x14),
    (0x84, 0x40), (0x85, 0xFF), (0x86, 0xFF), (0x87, 0xFF),
    (0x88, 0x0A), (0x89, 0x21), (0x8A, 0x00), (0x8B, 0x80),
    (0x8C, 0x01), (0x8D, 0x01), (0x8E, 0xFF), (0x8F, 0xFF),
    (0xB6, 0x00, 0x20), (0x36, 0x08), (0x3A, 0x05),
    (0x90, 0x08, 0x08, 0x08, 0x08), (0xBD, 0x06), (0xBC, 0x00),
    (0xFF, 0x60, 0x01, 0x04), (0xC3, 0x13), (0xC4, 0x13),
    (0xC9, 0x22), (0xBE, 0x11), (0xE1, 0x10, 0x0E),
    (0xDF, 0x21, 0x0C, 0x02),
    (0xF0, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A),
    (0xF1, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F),
    (0xF2, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A),
    (0xF3, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F),
    (0xED, 0x1B, 0x0B), (0xAE, 0x77), (0xCD, 0x63),
    (0x70, 0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03),
    (0xE8, 0x34),
    (0x62, 0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70),
    (0x63, 0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70),
    (0x64, 0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07),
    (0x66, 0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00),
    (0x67, 0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98),
    (0x74, 0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00),
    (0x98, 0x3E, 0x07), (0x35,), (0x21,), (0x11,),
]  # fmt: skip

_SLEEP_IN = 0x10
_SLEEP_OUT = 0x11
_DISPLAY_OFF = 0x28
_DISPLAY_ON = 0x29
_COLUMN_ADDRESS = 0x2A
_PAGE_ADDRESS = 0x2B
_MEMORY_WRITE = 0x2C

_SLEEP_OUT_DELAY = 0.120
"""Seconds after sleep out (0x11) or sleep in (0x10), before the next command.

The datasheet's figure is 120ms for both directions, and it is not padding: the
booster and the charge pump need that long to settle, and a command that lands
inside the window is accepted by a panel that is not yet driving its own supply.
The failure is not an error -- it is a panel that comes back garbled, or blank,
and stays that way until the next power cycle. Both waits are unconditional
because the driver cannot see how long ago the last transition was.
"""

_DISPLAY_ON_DELAY = 0.020
"""Seconds after display on/off (0x29/0x28), for the frame already in flight.

Shorter and less critical than the sleep delay -- it only keeps the panel from
being told to sleep mid-scan -- but it costs nothing to honour.
"""


class GC9A01Display:
    """240x240 round SPI panel. Pure transport: it draws what it is handed.

    Rotation and h-flip belong to the screen worker and have already happened by
    the time an image arrives here.
    """

    def __init__(
        self,
        spi_bus: int,
        spi_cs: int,
        dc: int,
        rst: int,
        hz: int,
        serial_factory: SerialFactory | None = None,
    ) -> None:
        if serial_factory is None:
            try:
                # Imported here, not at module scope, so that `pack565` -- the
                # one part of this file a test can prove -- imports on a machine
                # with no SPI, no GPIO and no luma.
                from luma.core.interface.serial import spi
            except ImportError as exc:
                raise DisplayError(
                    "the gc9a01 backend needs luma, which ships in the hardware extra: "
                    "install it with `uv sync --all-packages --extra hardware` (or "
                    "`pip install 'ors-daemon[hardware]'`), or set backend: virtual "
                    f"on this display to run without a panel ({exc})"
                ) from exc

            serial_factory = spi
        try:
            self._serial = serial_factory(
                port=spi_bus, device=spi_cs, gpio_DC=dc, gpio_RST=rst, bus_speed_hz=hz
            )
        except Exception as exc:
            raise DisplayError(f"cannot open SPI{spi_bus}.{spi_cs}: {exc}") from exc
        self._init_panel()

    def _command(self, cmd: int, *args: int) -> None:
        # The command byte and its parameters go out as two separate calls
        # because they are two different states of the D/C line: luma drops D/C
        # for `command` and raises it for `data`. Passing the parameters to
        # `command` as well -- which luma's varargs signature would accept --
        # would clock them out as further commands.
        try:
            self._serial.command(cmd)
            if args:
                self._serial.data(list(args))
        except Exception as exc:
            raise DisplayError(f"SPI command 0x{cmd:02X} failed: {exc}") from exc

    def _init_panel(self) -> None:
        for entry in _INIT:
            self._command(*entry)
        time.sleep(_SLEEP_OUT_DELAY)
        self._command(_DISPLAY_ON)
        time.sleep(_DISPLAY_ON_DELAY)

    @staticmethod
    def pack565(image: Image.Image) -> bytes:
        """Big-endian RGB565, two bytes per pixel, row-major. No hardware needed.

        Two implementations that must agree byte for byte: numpy when the
        hardware extra is installed, and a Python loop when it is not. The loop
        costs ~14ms per 240x240 frame on an x86 desktop and several times that
        on the Pi this runs on, which is most of a four-panel frame budget; the
        vectorised path is ~55x faster. The loop stays because the daemon has to
        start, and draw, on a machine with no numpy.
        """
        rgb = image.convert("RGB")
        if _numpy is None:
            return GC9A01Display._pack565_loop(rgb)

        pixels = _numpy.asarray(rgb, dtype=_numpy.uint16)
        values = (
            ((pixels[..., 0] & 0xF8) << 8) | ((pixels[..., 1] & 0xFC) << 3) | (pixels[..., 2] >> 3)
        )
        # `>u2` is the big-endian order the panel reads; `tobytes` is C order,
        # which is the row-major order the loop below produces.
        return values.astype(">u2").tobytes()

    @staticmethod
    def _pack565_loop(rgb: Image.Image) -> bytes:
        raw = rgb.tobytes()
        packed = bytearray(len(raw) // 3 * 2)
        offset = 0
        for red, green, blue in zip(raw[0::3], raw[1::3], raw[2::3], strict=True):
            value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
            packed[offset] = value >> 8
            packed[offset + 1] = value & 0xFF
            offset += 2
        return bytes(packed)

    def show(self, image: Image.Image) -> None:
        # A whole frame, every call: 240x240 in RGB565 is 115200 bytes, which is
        # 23ms of bus time at 40MHz before any packing, and four panels share one
        # daemon. Sending only the rows that changed would need a previous frame
        # kept here and compared -- the protocol allows it, since a caller cannot
        # observe what reaches the bus -- but nothing needs it yet.
        width, height = image.size
        self._command(_COLUMN_ADDRESS, 0, 0, (width - 1) >> 8, (width - 1) & 0xFF)
        self._command(_PAGE_ADDRESS, 0, 0, (height - 1) >> 8, (height - 1) & 0xFF)
        self._command(_MEMORY_WRITE)
        try:
            self._serial.data(list(self.pack565(image)))
        except Exception as exc:
            raise DisplayError(f"SPI write failed: {exc}") from exc

    def sleep(self) -> None:
        self._command(_DISPLAY_OFF)
        time.sleep(_DISPLAY_ON_DELAY)
        self._command(_SLEEP_IN)
        time.sleep(_SLEEP_OUT_DELAY)

    def wake(self) -> None:
        self._command(_SLEEP_OUT)
        time.sleep(_SLEEP_OUT_DELAY)
        self._command(_DISPLAY_ON)
        time.sleep(_DISPLAY_ON_DELAY)

    def close(self) -> None:
        # Shutdown, and often shutdown after something else already went wrong:
        # a port that will not release is not worth a second exception here.
        try:
            self._serial.cleanup()
        except Exception:
            pass
