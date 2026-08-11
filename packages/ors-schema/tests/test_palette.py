import ors_schema
import pytest
from ors_schema.palette import PALETTE_TOKEN, Color, GradientPalette, PaletteRef, ThresholdPalette
from pydantic import BaseModel, ValidationError


class Holder(BaseModel):
    color: Color
    palette: PaletteRef


def test_named_palette_is_a_plain_string():
    h = Holder(color="#ff0000", palette="cyan")
    assert h.palette == "cyan"


def test_palette_token_is_a_legal_color():
    # Through the constant, not the literal: `Color`'s pattern and
    # `ors_render.elements.resolve_color` have to agree on this exact string,
    # and a drift between them would render white rather than fail anywhere.
    assert "PALETTE_TOKEN" in ors_schema.__all__, "the renderer imports it from the package root"
    assert Holder(color=PALETTE_TOKEN, palette="cyan").color == PALETTE_TOKEN


def test_gradient_palette_parses_from_dict():
    h = Holder(
        color="#ffffff",
        palette={
            "kind": "gradient",
            "stops": [{"at": 0.0, "color": "#00e5ff"}, {"at": 1.0, "color": "#2979ff"}],
        },
    )
    assert isinstance(h.palette, GradientPalette)
    assert h.palette.stops[1].color == "#2979ff"


def test_threshold_palette_parses_from_dict():
    h = Holder(
        color="#ffffff",
        palette={
            "kind": "threshold",
            "thresholds": [
                {"at": 0, "palette": "green"},
                {"at": 70, "palette": "amber"},
                {"at": 90, "palette": "red"},
            ],
        },
    )
    assert isinstance(h.palette, ThresholdPalette)
    assert h.palette.thresholds[2].palette == "red"


@pytest.mark.parametrize("bad", ["ff0000", "#f00", "#gggggg", "red", ""])
def test_invalid_colors_are_rejected(bad):
    with pytest.raises(ValidationError):
        Holder(color=bad, palette="cyan")


def test_gradient_stops_must_be_ordered_and_nonempty():
    with pytest.raises(ValidationError):
        GradientPalette(stops=[])
    with pytest.raises(ValidationError):
        GradientPalette(stops=[{"at": 1.0, "color": "#000000"}, {"at": 0.0, "color": "#ffffff"}])


def test_threshold_entries_must_be_ordered_and_nonempty():
    with pytest.raises(ValidationError):
        ThresholdPalette(thresholds=[])
    with pytest.raises(ValidationError):
        ThresholdPalette(
            thresholds=[
                {"at": 90, "palette": "red"},
                {"at": 70, "palette": "amber"},
                {"at": 0, "palette": "green"},
            ]
        )


def test_threshold_palette_with_inline_palette_entries():
    tp = ThresholdPalette(
        thresholds=[
            {
                "at": 0,
                "palette": {
                    "kind": "gradient",
                    "stops": [
                        {"at": 0.0, "color": "#00e5ff"},
                        {"at": 1.0, "color": "#2979ff"},
                    ],
                },
            },
            {
                "at": 50,
                "palette": {
                    "kind": "threshold",
                    "thresholds": [
                        {"at": 0, "palette": "green"},
                        {"at": 90, "palette": "red"},
                    ],
                },
            },
            {"at": 100, "palette": "named_palette"},
        ]
    )
    assert isinstance(tp.thresholds[0].palette, GradientPalette)
    assert tp.thresholds[0].palette.stops[1].color == "#2979ff"
    assert isinstance(tp.thresholds[1].palette, ThresholdPalette)
    assert isinstance(tp.thresholds[2].palette, str)
    assert tp.thresholds[2].palette == "named_palette"


def test_binding_is_a_legal_color():
    # A template parameterises its centre colour as `"color": "{{params.color}}"`,
    # so the pattern has to admit a binding the renderer resolves later.
    assert Holder(color="{{params.color}}", palette="cyan").color == "{{params.color}}"
