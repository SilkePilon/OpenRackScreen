"""The text element, and the three ways it declines to draw.

Text is the family with the least tolerance for a strange number, because it is
the only one that hands its coordinates to freetype and to Pillow's glyph
blitter rather than to a shape primitive that clips them. `cx`, `cy` and `size`
are plain unbounded floats in the schema, so everything below is valid scene
JSON someone can author directly -- no `step` on a group required -- and each
case used to arrive as a traceback out of `render_scene`:

* **A position that is not a number** -- NaN or an infinity, or a finite value
  so large that scaling it to pixels overflows (``cy: 1e308``). The element is
  *skipped*. There is no honest place to put it: a NaN is not a coordinate at
  all, and clamping an overflowed one to the canvas edge would invent a
  position the scene never asked for and drop the label on top of real content.
  Compare `ors_render.elements.pixel_width`, which clamps a non-finite stroke
  width instead of skipping: a rect with a broken stroke still has a fill and a
  place to be, so there is something left to draw. Here the position *is* the
  element.
* **A size that is not a number**, likewise skipped, for the reason
  `ors_render.elements.ring` skips a ring whose `thickness` is non-finite: the
  size is the element, and a glyph with no size is nothing.
* **A size that is merely absurd**, which is *clamped* rather than skipped -- a
  number the scene meant, just bigger than the panel. The cap is one panel
  height, past which a glyph shows at most a fragment of one stroke. It is also
  well below where the stack gives up: measured, ``size: 1e4`` trips Pillow's
  own decompression-bomb guard inside `getmask2` and ``size: 1e5`` freetype's
  "invalid pixel size". The clamp is two-sided, because the *bottom* end
  overflows too: `Geometry.font_px` already floors a negative size at one pixel,
  but it does so with a `round` that raises on the ``-inf`` that scaling
  ``size: -1e308`` produces before the floor is ever reached.

A fourth case is not degradation but plain culling: text whose box lies wholly
off the canvas is not drawn. Pillow would clip it to the same result, but only
after the coordinate has reached its C rasteriser, which indexes with a signed
64-bit integer -- measured, ``cy: 1e17`` raises ``SystemError`` out of
``draw_bitmap``. Culling first means no finite coordinate can get that far.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from ors_schema.palette import GradientPalette
from ors_schema.scene import TextElement
from PIL import ImageDraw, ImageFont

from ors_render.bindings import resolve_text
from ors_render.canvas import Canvas
from ors_render.context import RenderContext
from ors_render.elements import register, resolve_color
from ors_render.fonts import load_font

_MAX_GLYPHS = 256
"""Longest resolved string this element will measure or draw, in characters.

`text` is a *binding*, so its length belongs to whatever the feed sent -- a
torrent name, a Kubernetes error message, a Prometheus label -- and not to the
scene. Before this cap there was no safe configuration:

* **With** `max_width`, `_truncate` walked one character at a time and
  re-measured the whole string, which is quadratic: measured, 0.28 s at 1 000
  characters, 1.02 s at 2 000, 3.88 s at 4 000, 15.18 s at 8 000, and at 200 000
  it had not finished after four minutes.
* **Without** `max_width`, the string reached `textbbox` whole and Pillow's own
  decompression-bomb guard fired: 200 000 characters warned at 114 MP and
  400 000 raised `Image.DecompressionBombError`.

Both halves were live on shipped templates -- `torrent.json` sets `max_width` on
a torrent name and on `min_eta`, `system.json` on a daemon-supplied error string
-- so this is the same argument the module docstring above makes for `size`,
carried to the other unbounded dimension of the same element. The truncation is
now a bisect as well, but a bound that depends on an algorithm staying fast is
not a bound; this one holds whatever either path costs per measurement.

256 because two things have to be true of it:

*Nothing readable is anywhere near it.* A 240 px panel shows roughly 120 glyphs
at the smallest legible size, so the cap is about twice what the hardware can
display in one line, and an order of magnitude past the 12 characters `trunc`
defaults to or the longest label any built-in draws.

*Even at `_MAX_SIZE` it stays under Pillow's bomb threshold.* The widest glyph in
the bundled face advances 529 px at that size on the supersampled canvas, so 256
of them measure 47 MP against `Image.MAX_IMAGE_PIXELS`' 89.5 MP -- meaning a
`DecompressionBombWarning` can never be emitted from here, exactly as
`ors_render.elements.media`'s `_MAX_SOURCE_PIXELS` guarantees for an asset.
Measured at 512 glyphs it is 94.9 MP and does warn, which is why the cap is not
that. Worst measured cost at the ceiling is 0.20 s, for the absurd combination of
256 glyphs at one panel height; a realistic 13 px label is 0.013 s.

Cutting rather than skipping, for the reason `_MAX_SIZE` clamps: the string is
what is over-long, not the element, and the first 256 characters of an error
message are the part a panel could have shown anyway.
"""

_MAX_SIZE = 240.0
"""Largest `size` honoured, in the schema's own px-at-a-240px-baseline units.

One panel height. `Geometry.font_px` scales it to the supersampled canvas, so
this is a glyph exactly as tall as the render surface whatever the supersample
factor.
"""


@register("text")
def render_text(
    canvas: Canvas, element: TextElement, ctx: RenderContext, palette: GradientPalette
) -> None:
    text = resolve_text(element.text, ctx.data)
    if not text:
        return
    # Ahead of *both* measuring paths -- the truncation loop below and the bare
    # `textbbox` when there is no `max_width` -- because each was unbounded in
    # the length of this string on its own. See `_MAX_GLYPHS`.
    text = text[:_MAX_GLYPHS]

    geometry = canvas.geometry
    cx, cy = geometry.x(element.cx), geometry.y(element.cy)
    if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(element.size)):
        # Checked *after* scaling for the position, because that is where a
        # finite-but-huge field turns into an infinity: `cy: 1e308` is a valid
        # float right up to the moment it is multiplied by the canvas size. See
        # the module docstring for why these skip rather than clamp.
        return

    # Clamped at both ends before scaling, so `font_px` can never be handed a
    # product that overflows. Zero is not a special case: `font_px` floors at
    # one pixel, which is what a negative size already rendered as.
    font = load_font(element.font, geometry.font_px(max(0.0, min(element.size, _MAX_SIZE))))
    fill = resolve_color(element.color, palette, ctx.data)

    # Pillow's `textlength` refuses multiline text, and the schema gives a text
    # element no line-spacing control anyway, so a newline-bearing string is
    # simply drawn as-is rather than measured and clipped.
    if element.max_width is not None and "\n" not in text:
        text = _truncate(
            canvas.draw, text, font, geometry.span(element.max_width), element.ellipsis
        )

    box = canvas.draw.textbbox((0, 0), text, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    if element.align == "left":
        x = cx - box[0]
    elif element.align == "right":
        x = cx - width - box[0]
    else:
        x = cx - width / 2 - box[0]
    y = cy - height / 2 - box[1]

    # `x`/`y` are the *anchor* Pillow draws from; the ink lands at the anchor
    # plus the bbox origin, which is what has to miss the canvas for the label
    # to be invisible. See the module docstring: Pillow would clip to the same
    # picture, but only after handing the coordinate to a rasteriser that
    # indexes with a C integer.
    left, top = x + box[0], y + box[1]
    if left + width <= 0 or top + height <= 0 or left >= geometry.px or top >= geometry.px:
        return
    canvas.draw.text((x, y), text, font=font, fill=fill)


def _truncate(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    limit: float,
    ellipsis: bool,
) -> str:
    """The longest prefix of `text` that measures no wider than `limit`.

    Text that already fits is returned untouched. When `ellipsis` is set the
    last surviving character is traded for a ``.`` as soon as that fits, which
    is why the trailing dot never pushes the result back over the limit. A
    limit too narrow for even one glyph empties the string; drawing "" is a
    no-op, so the element simply disappears rather than overflowing.

    Found by **bisection**, not by walking a character at a time. The walk
    re-measured the whole string on every step, which is quadratic in a length
    the scene does not control -- measured at 3.88 s for 4 000 characters and
    15.18 s for 8 000, on a panel that redraws several times a second.
    `_MAX_GLYPHS` now caps the input as well; the two bounds are independent on
    purpose, since the cap alone still left the cost growing as its square and
    the bisect alone still measured strings big enough to trip Pillow's
    decompression-bomb guard.

    Bisection is sound here because a prefix cannot measure *narrower* than a
    shorter one: glyph advances are non-negative, so width grows monotonically
    with the prefix length, and the same holds for the dotted candidates, which
    differ from each other in exactly the same way. The dotted and plain
    searches are kept separate, and the dotted result wins a tie, because that
    is the order the walk tried them in -- and the two are genuinely different
    questions: ``.`` is *wider* than an ``i`` in this face, so a dotted
    candidate can fail where the plain prefix of the same length fits.
    """
    if math.isnan(limit) or draw.textlength(text, font=font) <= limit:
        # `max_width` is an unbounded float in the schema and `json.loads` parses
        # `NaN`, so a limit that is not a number is reachable input. It means
        # "no limit" here, which is what the walk did with it: every ``>``
        # comparison against a NaN is false, so it never dropped a character.
        return text

    def fits(candidate: str) -> bool:
        return draw.textlength(candidate, font=font) <= limit

    # The full string is known not to fit, so both searches stop one short of it.
    # "Nothing fits" folds into the empty string: a negative `max_width` is the
    # only way there, and the walk emptied the string for it too.
    plain = _longest_fitting(lambda n: text[:n], fits, len(text) - 1) or 0
    if ellipsis:
        dotted = _longest_fitting(lambda n: text[: n - 1] + ".", fits, len(text) - 1, low=1)
        if dotted is not None and dotted >= plain:
            return text[: dotted - 1] + "."
    return text[:plain]


def _longest_fitting(
    build: Callable[[int], str], fits: Callable[[str], bool], high: int, low: int = 0
) -> int | None:
    """The largest length in ``low..high`` whose candidate fits, or ``None``.

    ``None`` rather than ``low - 1`` because "nothing fits at all" is a real
    answer: a negative `max_width` scales to a negative limit, which not even
    the empty string measures under.
    """
    best: int | None = None
    while low <= high:
        middle = (low + high) // 2
        if fits(build(middle)):
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best
