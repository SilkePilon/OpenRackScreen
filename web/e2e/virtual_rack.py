"""A real `ors-daemon`, with the two things a machine without a rack cannot have.

This is `ors-daemon` -- the same `main`, the same argument parser, the same
supervisor, the same link, the same frame pump -- with exactly two module
globals replaced before it is called. Both are seams the daemon already has,
and both are replaced for the same reason: the end-to-end run drives the setup
wizard, and the wizard's whole subject is hardware.

*`SPI_ROOT`, so `detect` enumerates a directory this test made.* Detection is a
directory listing: `enumerate_panels(root)` takes its root as a parameter
precisely "so that no test of this ever reads `/dev`", and `__main__` passes the
module constant. A developer machine has no `/dev/spidev*` at all, so an
unpatched daemon answers a detect with an empty list and the wizard's first step
has nothing to offer. What is replaced is *where* it looks; the listing, the
regular expression, the numeric sort, the cut at `MAX_PANEL_CANDIDATES` and the
pairing with `claimed_devices()` are all the daemon's own.

*`build_display`, so every panel is virtual.* `VirtualDisplay` writes a PNG per
screen, which is what makes a rack testable with no SPI -- but the wizard
creates a **gc9a01** screen, because that is the only backend it offers, and a
gc9a01 backend on this machine raises before it opens anything (luma is not
installed, and there would be no bus under it if it were). So the factory is
substituted rather than the configuration: the screen the wizard writes is the
screen the server stores and pushes, and only the last inch -- the object the
frame is handed to -- is a directory instead of a panel.

**What that costs, stated plainly, because it is what the hardware checklist is
for.** Nothing here proves that a real `/dev` enumerates as expected, that a
GC9A01 opens on a real bus, that a probe with the wrong DC line raises rather
than blocking, or that 40 MHz is a clock the wiring survives. Those need the
rack. Everything above the backend -- the wizard, the two routes, the daemon's
claim on a device, the bus guard, the hold, the render, the encode, the socket
and the canvas -- is the real thing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    # Imported inside the function so that the two substitutions below happen
    # against modules this process has just loaded, in one place, with nothing
    # between the import and the patch that could hold a reference to either.
    import ors_daemon.__main__ as entry
    import ors_daemon.supervisor as supervisor
    from ors_daemon.displays.virtual import VirtualDisplay

    panels = Path(os.environ["ORS_E2E_PANEL_DIR"])
    entry.SPI_ROOT = Path(os.environ["ORS_E2E_SPI_ROOT"])
    # `_display_factory` defaults to `lambda screen, name: build_display(...)`,
    # which resolves this global at call time -- so replacing it here reaches
    # every panel the supervisor opens *and* the one a probe opens, which is the
    # path the wizard's third step walks.
    supervisor.build_display = lambda display, name: VirtualDisplay(panels, name)
    return entry.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
