"""Scene JSON in, image out -- the whole public surface of the render engine.

Importing this package imports `ors_render.render`, whose side-effect imports
are what register the element families. Nothing here may reach past `render`
to the element modules directly: an API that did would import cleanly and then
draw every scene blank.

The three consumers of this surface are the daemon (renders a screen to SPI),
the server (renders a preview PNG for the editor) and the built-in templates
(validated and rendered in tests). Everything else -- `Canvas`, the expression
evaluator, the palettes -- is an implementation detail of that, and stays
reachable at its own module path for anyone who genuinely needs it.

`Geometry` is exported despite neither entry point taking one: both build their
own from `size`/`supersample`, so a consumer needs it only to ask what a scene's
normalized coordinates come out to in pixels -- which a preview overlay or a
hardware bring-up check legitimately does.
"""

from ors_render.context import RenderContext
from ors_render.geometry import Geometry
from ors_render.render import render_scene, render_screen, select_scene

__version__ = "0.1.0"

__all__ = ["Geometry", "RenderContext", "render_scene", "render_screen", "select_scene"]
