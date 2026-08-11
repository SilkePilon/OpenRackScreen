import pytest
from ors_render.geometry import Geometry


def test_default_geometry_supersamples_by_two():
    g = Geometry()
    assert g.size == 240
    assert g.px == 480


def test_positions_are_fractions_of_full_size():
    g = Geometry()
    assert g.x(0.5) == 240.0
    assert g.y(1.0) == 480.0
    assert g.span(0.25) == 120.0


def test_radii_are_fractions_of_the_panel_radius():
    g = Geometry()
    # 0.875 of a 240px panel radius == 105px, which is 210px in supersampled space
    assert g.radial(0.875) == 210.0
    assert g.radial(0.092) == pytest.approx(22.08)


def test_font_sizes_scale_from_a_240_baseline():
    assert Geometry().font_px(52) == 104
    assert Geometry(size=480, supersample=1).font_px(52) == 104
