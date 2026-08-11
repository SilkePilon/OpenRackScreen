"""What the daemon looks like from outside, as one JSON file.

With no server yet, this file is the whole answer to "why does that panel look
wrong": per-screen scene and state, per-integration health with the last error
and its latency, uptime. One `cat` over SSH explains a rack.

It is a contract as much as a debugging aid. M3's link client reports this same
structure upstream verbatim rather than inventing a second one, so a field
renamed here is a field renamed on the wire -- and every value is left in a
shape JSON already has, because whatever reads it next is not Python.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ors_daemon.screen import ScreenWorker
from ors_daemon.snapshot import Snapshot


@dataclass(frozen=True)
class UnavailableScreen:
    """A configured screen with no worker behind it, and why.

    A panel whose backend would not open has no `ScreenWorker` to read state
    off, and the shape of that hole matters: reporting only the screens that
    started makes a four-panel rack with two dead panels write a file
    byte-identical to a healthy two-panel rack. The one ERROR line at startup
    is not a substitute -- it is never repeated, and this file is what a
    headless rack is read through, verbatim, by M3.

    It lives here rather than in the supervisor because this module owns the
    wire shape: every key the status file can carry is defined in one place,
    and the supervisor supplies facts rather than JSON. The supervisor is what
    *knows* -- it caught the exception -- so it constructs these and passes
    them in panel order alongside the workers.
    """

    name: str
    reason: str


def _screen(entry: ScreenWorker | UnavailableScreen) -> dict[str, Any]:
    """One screen's line of the report, whether or not it has a worker."""
    if isinstance(entry, UnavailableScreen):
        return {
            "name": entry.name,
            "scene": None,
            "state": "unavailable",
            "last_render": None,
            "renders": 0,
            "error": entry.reason,
        }
    return {
        "name": entry.screen_name,
        "scene": entry.current_scene,
        # Faulted first: a panel that fell over inside the night window
        # is asleep *and* faulted, and the fault is the news.
        "state": "faulted" if entry.faulted else ("asleep" if entry.asleep else "awake"),
        "last_render": entry.last_render.isoformat() if entry.last_render else None,
        "renders": entry.renders,
        # Present always, and null for a screen that has a worker at all --
        # the same bargain `stale` makes on an integration. A *faulted* panel
        # is the one gap: its reason is in the log rather than on the worker,
        # so it reports `faulted` with no `error`. Closing that means giving
        # `ScreenWorker` a public fault reason, which is a change to the render
        # loop and not to this file.
        "error": None,
    }


def build_status(
    started_at: datetime,
    now: datetime,
    config_version: int,
    screens: Sequence[ScreenWorker | UnavailableScreen],
    snapshot: Snapshot,
) -> dict[str, Any]:
    """Assemble what a person over SSH -- and later the server -- needs to see.

    Reads the workers' public fields without their lock. They are single
    machine words written by one thread each, so the worst this can produce is
    a report a fraction of a frame out of date -- against a file rewritten
    about once a second, describing panels that redraw about as often. Taking
    four locks to shave that would put a status report in the path of the
    render loop, which is the wrong way round: the panels are the product.

    `now` is passed in rather than read here so the caller's injected clock is
    the only clock in the daemon, uptime included.

    `screens` carries every *configured* screen, in panel order -- a worker
    where there is one, an `UnavailableScreen` where the panel would not open.
    A screen missing from this list is a screen the daemon has forgotten, not a
    screen that is broken, and the file must be able to tell those apart.
    """
    return {
        "uptime_s": int((now - started_at).total_seconds()),
        "config_version": config_version,
        "screens": [_screen(entry) for entry in screens],
        "integrations": [
            {
                "name": name,
                # `.value`, so the file carries "healthy" rather than
                # "Health.HEALTHY" -- the enum's `str()` is a Python detail, and
                # this file has readers that have never heard of Python.
                "state": health.state.value,
                "stale": health.stale,
                "latency_ms": health.latency_ms,
                "last_success": health.last_success.isoformat() if health.last_success else None,
                # An integration that has never answered reports `connecting`
                # with a reason, not `unhealthy`: the store draws that
                # distinction deliberately (see `SnapshotStore.fail`) and this
                # file reports it rather than flattening it.
                "last_error": health.reason,
            }
            for name, health in snapshot.health.items()
        ],
    }


def write_status(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically, so a reader polling this file never sees half of it.

    Three things make that true, and all three are load-bearing:

    * The temporary file is created **beside the target**, not in `/tmp`. A
      rename is only atomic within one filesystem; across two it degrades into
      a copy, which is exactly the partial file this exists to prevent.
    * `fsync` before the rename, so a power cut cannot leave the name pointing
      at a file whose contents never reached the disk.
    * `os.replace` rather than a write in place. A plain `write_text` truncates
      first and flushes later, and a reader landing in that window gets an
      empty file -- measured at 100 corrupt reads in 200 writes, so this is a
      certainty at 1 Hz, not a race worth gambling on.

    Failures are raised, not swallowed. Whether a status file is worth stopping
    for is the caller's call, and the answer is no: the supervisor logs an
    unwritable path and keeps driving panels, because a rack that shows the
    right thing and cannot say so beats a rack that says nothing because it
    stopped. Deciding that here would also make a one-shot "write the status to
    this path" silently do nothing, which is worse than an error message.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "w") as handle:
        # Indented and newline-terminated because the audience is someone
        # holding `cat` over SSH; every parser is indifferent. `default=str` is
        # a net under a payload that is already JSON-native throughout -- a
        # test pins that -- so an unforeseen value degrades to its string form
        # instead of costing the file that explains the daemon.
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
