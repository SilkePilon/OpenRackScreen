from __future__ import annotations

from typing import Any

import pytest
from ors_daemon.displays import DisplayError, build_display
from ors_daemon.displays import gc9a01 as gc9a01_module
from ors_daemon.displays.gc9a01 import GC9A01Display
from ors_daemon.displays.virtual import VirtualDisplay
from ors_schema.daemon import DisplayConfig
from PIL import Image


def panel(color: tuple[int, int, int] = (255, 0, 0)) -> Image.Image:
    return Image.new("RGB", (240, 240), color)


def test_virtual_display_writes_one_png_per_screen(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.show(panel())

    written = tmp_path / "CPU.png"
    assert written.exists()
    assert Image.open(written).size == (240, 240)
    assert display.frames == 1


def test_virtual_display_overwrites_rather_than_accumulating(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.show(panel((255, 0, 0)))
    display.show(panel((0, 255, 0)))

    assert list(tmp_path.glob("*.png")) == [tmp_path / "CPU.png"]
    assert Image.open(tmp_path / "CPU.png").getpixel((120, 120)) == (0, 255, 0)
    assert display.frames == 2


def test_virtual_display_leaves_no_temporary_file_behind(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.show(panel())

    assert [path.name for path in sorted(tmp_path.iterdir())] == ["CPU.png"]


def test_virtual_display_tracks_sleep_and_wake(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    assert display.asleep is False
    display.sleep()
    assert display.asleep is True
    display.wake()
    assert display.asleep is False


def test_virtual_display_creates_its_directory(tmp_path):
    target = tmp_path / "nested" / "panels"
    VirtualDisplay(target, "CPU").show(panel())

    assert (target / "CPU.png").exists()


def test_virtual_display_close_is_harmless(tmp_path):
    display = VirtualDisplay(tmp_path, "CPU")
    display.close()
    display.close()


# --- pack565 -----------------------------------------------------------------
#
# The one part of the driver a test can prove: it needs no bus, no GPIO and no
# luma, so it is exercised the same way on CI as on the Pi.


def test_pack565_is_two_big_endian_bytes_per_pixel():
    packed = GC9A01Display.pack565(Image.new("RGB", (2, 1), (255, 0, 0)))

    assert len(packed) == 4
    assert packed == b"\xf8\x00\xf8\x00"


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ((255, 255, 255), b"\xff\xff"),
        ((0, 0, 0), b"\x00\x00"),
        ((0, 255, 0), b"\x07\xe0"),
        ((0, 0, 255), b"\x00\x1f"),
    ],
)
def test_pack565_channel_layout(color, expected):
    assert GC9A01Display.pack565(Image.new("RGB", (1, 1), color)) == expected


def test_pack565_walks_the_image_in_row_major_order():
    image = Image.new("RGB", (2, 2))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 255, 0))
    image.putpixel((0, 1), (0, 0, 255))
    image.putpixel((1, 1), (255, 255, 255))

    assert GC9A01Display.pack565(image) == b"\xf8\x00\x07\xe0\x00\x1f\xff\xff"


def test_pack565_converts_a_non_rgb_image():
    assert GC9A01Display.pack565(Image.new("L", (1, 1), 255)) == b"\xff\xff"


def test_pack565_covers_a_full_panel():
    assert len(GC9A01Display.pack565(panel())) == 240 * 240 * 2


def _mixed_panel() -> Image.Image:
    """A frame with every channel value present, so a packing bug cannot hide."""
    image = Image.new("RGB", (240, 240))
    image.putdata([(x % 256, (y * 3) % 256, (x + y) % 256) for y in range(240) for x in range(240)])
    return image


def test_pack565_falls_back_to_the_pure_python_loop_without_numpy(monkeypatch):
    monkeypatch.setattr(gc9a01_module, "_numpy", None)

    assert GC9A01Display.pack565(Image.new("RGB", (2, 1), (255, 0, 0))) == b"\xf8\x00\xf8\x00"


def test_pack565_numpy_and_loop_agree_byte_for_byte(monkeypatch):
    if gc9a01_module._numpy is None:
        pytest.skip("numpy is not installed; the vectorised path cannot be exercised here")

    image = _mixed_panel()
    vectorised = GC9A01Display.pack565(image)
    monkeypatch.setattr(gc9a01_module, "_numpy", None)

    assert vectorised == GC9A01Display.pack565(image)


# --- the driver --------------------------------------------------------------
#
# `time` is replaced wholesale in the driver's namespace, so no test ever sleeps
# for real; every delay lands in the same log as the bytes around it, which is
# the only way to assert that a delay sits *between* two particular commands.
# The assertions below check the recorded delays, so a patch that stopped taking
# effect would fail the suite rather than silently make it slow.


class FakePanel:
    """Serial factory, serial port and clock in one, recording an ordered log."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self.log: list[tuple[str, Any]] = []
        self.cleanups = 0
        self._fail_on = fail_on

    def __call__(self, **kwargs: Any) -> FakePanel:
        self.kwargs = kwargs
        return self

    def command(self, cmd: int) -> None:
        if cmd == self._fail_on:
            raise OSError("bus error")
        self.log.append(("command", cmd))

    def data(self, payload: list[int]) -> None:
        self.log.append(("data", tuple(payload)))

    def cleanup(self) -> None:
        self.cleanups += 1

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))


@pytest.fixture
def fake_panel(monkeypatch) -> FakePanel:
    panel_ = FakePanel()
    monkeypatch.setattr(gc9a01_module, "time", panel_)
    return panel_


def build_driver(fake_panel: FakePanel) -> GC9A01Display:
    return GC9A01Display(spi_bus=0, spi_cs=1, dc=6, rst=5, hz=40_000_000, serial_factory=fake_panel)


def commands(log: list[tuple[str, Any]]) -> list[int]:
    return [payload for kind, payload in log if kind == "command"]


def delay_after(log: list[tuple[str, Any]], command: int) -> float:
    """Seconds slept between `command` and whatever command follows it."""
    index = next(i for i, entry in enumerate(log) if entry == ("command", command))
    total = 0.0
    for kind, payload in log[index + 1 :]:
        if kind == "sleep":
            total += payload
        elif kind == "command":
            break
    return total


def test_driver_opens_the_port_with_lumas_keyword_names(fake_panel):
    build_driver(fake_panel)

    assert fake_panel.kwargs == {
        "port": 0,
        "device": 1,
        "gpio_DC": 6,
        "gpio_RST": 5,
        "bus_speed_hz": 40_000_000,
        # luma defaults both of these to zero, which gives the controller no
        # settling time at all before a fifty-command init sequence starts --
        # and this panel has no software reset to fall back on.
        "reset_hold_time": 0.010,
        "reset_release_time": 0.150,
    }


def test_driver_reports_a_port_it_cannot_open(monkeypatch):
    def refuse(**kwargs: Any) -> Any:
        raise OSError("No such file or directory")

    monkeypatch.setattr(gc9a01_module, "time", FakePanel())
    with pytest.raises(DisplayError, match=r"SPI0\.1"):
        GC9A01Display(spi_bus=0, spi_cs=1, dc=6, rst=5, hz=1, serial_factory=refuse)


def test_init_sends_the_whole_sequence_in_order_then_display_on(fake_panel):
    build_driver(fake_panel)

    assert commands(fake_panel.log) == [entry[0] for entry in gc9a01_module._INIT] + [0x29]


def test_init_sends_parameters_as_data_so_the_dc_line_is_right(fake_panel):
    build_driver(fake_panel)

    assert ("command", 0xB6) in fake_panel.log
    index = fake_panel.log.index(("command", 0xB6))
    assert fake_panel.log[index + 1] == ("data", (0x00, 0x20))


def test_init_sends_a_parameterless_command_without_a_data_phase(fake_panel):
    build_driver(fake_panel)

    index = fake_panel.log.index(("command", 0xFE))
    assert fake_panel.log[index + 1][0] != "data"


def test_init_waits_the_datasheet_delay_after_sleep_out(fake_panel):
    build_driver(fake_panel)

    assert delay_after(fake_panel.log, 0x11) >= 0.120


def test_init_ends_with_sleep_out_before_display_on(fake_panel):
    build_driver(fake_panel)

    assert commands(fake_panel.log)[-2:] == [0x11, 0x29]


def test_init_reports_a_command_that_fails_on_the_bus(monkeypatch):
    monkeypatch.setattr(gc9a01_module, "time", FakePanel())
    with pytest.raises(DisplayError, match="0xEF"):
        GC9A01Display(
            spi_bus=0, spi_cs=0, dc=6, rst=5, hz=1, serial_factory=FakePanel(fail_on=0xEF)
        )


def test_show_addresses_the_panel_then_streams_the_frame(fake_panel):
    driver = build_driver(fake_panel)
    fake_panel.log.clear()
    driver.show(panel())

    assert fake_panel.log[:6] == [
        ("command", 0x2A),
        ("data", (0, 0, 0, 239)),
        ("command", 0x2B),
        ("data", (0, 0, 0, 239)),
        ("command", 0x2C),
        ("data", tuple(GC9A01Display.pack565(panel()))),
    ]


def test_show_reports_a_failed_write(fake_panel):
    driver = build_driver(fake_panel)

    def explode(payload: list[int]) -> None:
        # Only the pixel stream fails; the address windows before it are `data`
        # writes too, and wrapping those is `_command`'s job, tested separately.
        if fake_panel.log[-1] == ("command", 0x2C):
            raise OSError("bus error")
        fake_panel.log.append(("data", tuple(payload)))

    fake_panel.data = explode  # type: ignore[method-assign]
    with pytest.raises(DisplayError, match="SPI write failed"):
        driver.show(panel())


def test_sleep_waits_the_datasheet_delay_after_sleep_in(fake_panel):
    driver = build_driver(fake_panel)
    fake_panel.log.clear()
    driver.sleep()

    assert commands(fake_panel.log) == [0x28, 0x10]
    assert delay_after(fake_panel.log, 0x10) >= 0.120


def test_wake_waits_the_datasheet_delay_before_display_on(fake_panel):
    driver = build_driver(fake_panel)
    fake_panel.log.clear()
    driver.wake()

    assert commands(fake_panel.log) == [0x11, 0x29]
    assert delay_after(fake_panel.log, 0x11) >= 0.120


def test_close_releases_the_port(fake_panel):
    build_driver(fake_panel).close()

    assert fake_panel.cleanups == 1


def test_close_swallows_a_port_that_will_not_release(fake_panel):
    driver = build_driver(fake_panel)

    def explode() -> None:
        raise OSError("already gone")

    fake_panel.cleanup = explode  # type: ignore[method-assign]
    driver.close()


# --- the factory -------------------------------------------------------------


def test_build_display_returns_a_virtual_backend(tmp_path):
    config = DisplayConfig(backend="virtual", out_dir=str(tmp_path))
    assert isinstance(build_display(config, "CPU"), VirtualDisplay)


def test_build_display_writes_where_the_config_says(tmp_path):
    config = DisplayConfig(backend="virtual", out_dir=str(tmp_path))
    build_display(config, "NET").show(panel())

    assert (tmp_path / "NET.png").exists()


def test_build_display_reports_a_missing_hardware_dependency_clearly():
    config = DisplayConfig(backend="gc9a01", spi_bus=0, spi_cs=0, dc=6, rst=5)
    try:
        import luma.lcd  # noqa: F401
    except ImportError:
        with pytest.raises(DisplayError, match="luma"):
            build_display(config, "CPU")
    else:
        pytest.skip("luma is installed; the import-error path cannot be exercised here")


def test_a_frame_that_is_not_panel_sized_is_refused_rather_than_written(fake_panel):
    display = build_driver(fake_panel)
    fake_panel.log.clear()

    for size in ((120, 120), (240, 239), (480, 480)):
        with pytest.raises(DisplayError, match="240x240"):
            display.show(Image.new("RGB", size, (255, 0, 0)))

    assert fake_panel.log == [], "a refused frame must not reach the bus at all"
