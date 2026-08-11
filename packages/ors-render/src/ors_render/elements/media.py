"""The sparkline and image elements: a series, and bytes from the asset map.

Both families take their content from *outside* the scene -- a list of readings
resolved from live data, a blob resolved from `RenderContext.assets` -- so both
spend most of their code deciding what to do when that content is not what the
scene assumed.

**A sparkline is a polyline, not a chart.** It has no axes, no baseline and no
scale of its own: the series is normalized to its own minimum and maximum and
stretched across the element's box, so a flat series is a flat line in the
middle of nothing rather than a line at zero. That is what makes it readable at
144x48 final pixels, and it is why a series needs at least two points to draw
anything at all.

**An image is composited, not blitted.** Assets arrive as encoded bytes, are
decoded to RGBA and pasted through their own alpha, so a logo with a
transparent surround sits on the panel background rather than on a black tile.
The three `fit` modes differ only in how the source is mapped onto the box:
`stretch` ignores the aspect ratio, `contain` shrinks until the whole source
fits inside it (never enlarging -- Pillow's `thumbnail` is explicit that it
produces an image "no larger than the given size"), and `cover` fills the box
and drops what hangs over the edges, cropping from the centre.

`cover` is done as a single `resize(..., box=...)` rather than as a full-size
resize followed by a `crop`. The two produce the same picture, but the naive
order sizes its intermediate by whichever axis needs the *most* scaling: a
4000x1 strip covering a 480 px box is a 1.9-gigapixel intermediate for a 240 px
panel. `Image.resize` has taken a source-space `box` since Pillow 4.3, well
under this package's 10.3 floor, so the crop rectangle is computed in source
coordinates and only the visible part is ever scaled.
"""

from __future__ import annotations

import io
import math
from typing import Literal

from ors_schema.palette import GradientPalette
from ors_schema.scene import ImageElement, SparklineElement
from PIL import Image

from ors_render.bindings import resolve_list, resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import pixel_width, register
from ors_render.geometry import Geometry
from ors_render.palettes import gradient_color

_LINE_WIDTH = 0.008
"""Sparkline stroke, as a fraction of the panel size: 4 px supersampled, 2 final.

Thick enough to survive the LANCZOS downsample as a solid line rather than a
grey suggestion, thin enough that a nine-point series in a 48 px tall box still
reads as nine separate moves.
"""

_FILL_STOP = 0.25
"""Where in the palette the area under the line is taken from.

The stroke is the palette's accent (stop 1.0), so the fill has to come from
somewhere else in the same gradient or the two merge into one flat shape. The
low end is the palette's own lighter stop, which is what every other element
here uses for "the same colour, less emphasis".
"""

_MAX_SOURCE_PIXELS = 4096 * 4096
"""Ceiling on a decoded asset, so an oversized upload cannot stall the panel.

Pillow does guard this, but at a threshold sized for desktop image editing: it
*warns* above `Image.MAX_IMAGE_PIXELS` (~89 megapixels) and only *raises*
above twice that. A 25-megapixel PNG therefore passes upstream untouched and
costs 100 MB and a visible stall on a screen that refreshes several times a
second. 16 megapixels is 4096x4096 -- 70x the supersampled canvas, far past any
logo or icon a rack panel can show -- and being below Pillow's warning
threshold it also means `DecompressionBombWarning` can never be emitted from
here. Same purpose as `ors_render.elements.ring`'s `_MAX_SEGMENTS`: a bound the
scene cannot see, so that untrusted input cannot buy unbounded work.
"""


@register("sparkline")
def render_sparkline(
    canvas: Canvas, element: SparklineElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    points = _series(resolve_list(element.values, ctx.data))
    if len(points) < 2:
        # One point is a dot with no direction and no scale to normalize
        # against; none at all is a binding that resolved to nothing.
        return

    geometry = canvas.geometry
    width, height = geometry.span(element.w), geometry.span(element.h)
    left = geometry.x(element.cx) - width / 2
    top = geometry.y(element.cy) - height / 2
    if not (math.isfinite(left) and math.isfinite(top)) or width <= 0 or height <= 0:
        # `cx`, `cy`, `w` and `h` are unbounded floats in the schema, so a
        # non-finite corner is schema-valid input rather than a bug here. The
        # finiteness test covers a NaN or infinite `w`/`h` too, both of which
        # reach `left`/`top` through the halving. A box with no area is skipped
        # for the reason `ors_render.elements.shapes` skips an empty rect: there
        # is nothing to show, and a degenerate polygon still leaves a hairline.
        return
    if left + width <= 0 or top + height <= 0 or left >= geometry.px or top >= geometry.px:
        # Wholly off the canvas. Finiteness is not enough on its own: a *finite*
        # `cx` of 1e7 is schema-valid and lands the box ten million panel widths
        # away, and Pillow's rasteriser wraps such a coordinate rather than
        # clipping it -- measured, a full-width band of ink across the middle of
        # the panel at every magnitude from 1e7 to 1e300 and on both signs. Same
        # cull, for the same reason, as `render_image` below and
        # `ors_render.elements.text`.
        return

    low, high = min(points), max(points)
    span = high - low
    if not math.isfinite(span):
        # Every point is finite, but their range need not be: a series holding
        # both 1e308 and -1e308 overflows. `(v - low) / span` would then be
        # NaN for every point, which Pillow accepts and draws as garbage.
        return
    span = span or 1.0

    bottom = top + height
    step = width / (len(points) - 1)
    coords = [
        (left + index * step, bottom - (value - low) / span * height)
        for index, value in enumerate(points)
    ]

    if element.fill:
        # Closed down to the baseline and back, so the area under the line is
        # one polygon rather than a per-segment trapezoid stack.
        canvas.draw.polygon(
            [*coords, (coords[-1][0], bottom), (coords[0][0], bottom)],
            fill=gradient_color(palette, _FILL_STOP),
        )
    canvas.draw.line(
        coords,
        fill=gradient_color(palette, 1.0),
        width=pixel_width(geometry, _LINE_WIDTH),
        # Round joins, so a sharp spike does not open a notch on its outside
        # edge where two thick segments meet at an acute angle.
        joint="curve",
    )


def _series(raw: list[object]) -> list[float]:
    """The plottable numbers in `raw`, in order, with everything else dropped.

    `values` resolves against live upstream data, so its entries are whatever
    arrived: text, nulls, a Prometheus NaN. Dropping them rather than refusing
    the whole series keeps a graph on screen through a gap in the feed, which is
    the reading the panel exists to give.

    ``bool`` is excluded explicitly because it is a subclass of ``int``: a feed
    that starts sending ``true``/``false`` would otherwise plot as a series of
    ones and zeroes and, being normalized to its own range, would look exactly
    like a real signal rather than like the broken one it is.
    """
    return [
        float(value)
        for value in raw
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    ]


@register("image")
def render_image(
    canvas: Canvas, element: ImageElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    blob = ctx.assets.get(resolve_text(element.src, ctx.data))
    if not blob:
        # A missing key is the normal case, not an error: a template can name an
        # asset the current install has never uploaded, and the panel around it
        # still has to render.
        return

    geometry = canvas.geometry
    width = _box_px(geometry, element.w)
    height = _box_px(geometry, element.h)
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    if width is None or height is None or not (math.isfinite(cx) and math.isfinite(cy)):
        return

    source = _decode(blob)
    if source is None:
        return
    fitted = _fit(source, element.fit, width, height)

    x = round(cx - fitted.width / 2)
    y = round(cy - fitted.height / 2)
    if x >= geometry.px or y >= geometry.px or x + fitted.width <= 0 or y + fitted.height <= 0:
        # Wholly off the canvas, so there is nothing to composite -- and the
        # coordinate may by now be far outside what Pillow's rasteriser can
        # index. `Image.paste` converts its box to a C integer, which overflows
        # somewhere above 1e12 (measured), and `cx` is an unbounded float.
        return
    # The alpha band of an RGBA image is a valid `paste` mask, so this
    # composites the asset over whatever is already on the canvas rather than
    # stamping a black tile around a transparent logo.
    canvas.image.paste(fitted, (x, y), fitted)


def _box_px(geometry: Geometry, fraction: float) -> int | None:
    """One side of the target box in whole pixels, or ``None`` if undrawable.

    ``w`` and ``h`` are fractions of the panel size and carry no bounds in the
    schema, so both ends need a decision. A non-finite or non-positive side has
    no box at all and gives ``None``. An over-large one is *clamped to the
    canvas* rather than refused: the excess was never going to be visible, and
    without the clamp a `w` of 1e5 asks Pillow to resize an asset to 48 million
    pixels across. Clamping does change what `cover` shows -- the crop is taken
    against the clamped box, so an absurd `w` reframes the picture -- which is
    accepted, because the alternative for absurd input is a stalled panel.
    """
    scaled = geometry.span(fraction)
    if not math.isfinite(scaled):
        return None
    side = min(round(scaled), geometry.px)
    return side if side > 0 else None


def _decode(blob: bytes) -> Image.Image | None:
    """Decode asset bytes to RGBA, or ``None`` if they are not a usable image.

    Asset bytes come from a web UI upload, so every documented failure mode of
    `Image.open` is reachable input rather than a bug: `UnidentifiedImageError`
    (a subclass of ``OSError``) for something that is not an image at all, a
    plain ``OSError`` for a truncated one -- `Image.open` is lazy, so that one
    surfaces here at `convert`, not at open -- ``ValueError`` for a bad mode,
    and `Image.DecompressionBombError`, which does not derive from either.

    The size test reads the *header*, which `Image.open` has already parsed
    without decoding a single pixel, so an oversized asset costs nothing beyond
    that. A zero-sided image is refused alongside it: `cover` divides by both
    sides.
    """
    try:
        with Image.open(io.BytesIO(blob)) as opened:
            if (
                opened.width <= 0
                or opened.height <= 0
                or opened.width * opened.height > _MAX_SOURCE_PIXELS
            ):
                return None
            return opened.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _fit(
    source: Image.Image, fit: Literal["contain", "cover", "stretch"], width: int, height: int
) -> Image.Image:
    """Map `source` onto a `width` x `height` box according to `fit`.

    `source` is the private RGBA copy `_decode` just made, which is what makes
    `thumbnail` -- the one Pillow call here that edits in place and returns
    ``None`` -- safe to use.
    """
    if fit == "stretch":
        return source.resize((width, height), Image.Resampling.LANCZOS)
    if fit == "contain":
        source.thumbnail((width, height), Image.Resampling.LANCZOS)
        return source
    # cover: scale by whichever axis needs the most, then keep the middle.
    scale = max(width / source.width, height / source.height)
    crop_width = min(source.width, width / scale)
    crop_height = min(source.height, height / scale)
    left = (source.width - crop_width) / 2
    top = (source.height - crop_height) / 2
    # `crop_width` is `width / scale` and `scale` is at least `width /
    # source.width`, so the box is never wider than the source and never
    # empty -- which is what stops the centring arithmetic from going negative.
    return source.resize(
        (width, height),
        Image.Resampling.LANCZOS,
        box=(left, top, left + crop_width, top + crop_height),
    )
