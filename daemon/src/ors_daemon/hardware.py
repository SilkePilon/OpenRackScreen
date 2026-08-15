"""What this rack can find out about its own panels, and what it may say about it.

*Detection is enumeration plus a guided probe, and that split is the hardware's
rather than a design choice.* A Pi can list `/dev/spidev*`, so it knows exactly
which buses and chip selects exist. It cannot discover DC and RST: those are GPIO
lines somebody chose with a screwdriver when the panel was wired, nothing on the
bus reports them, and **a GC9A01 has no readable id over 4-wire SPI**. A panel
cannot introduce itself. So enumeration says what is there and who already has
it, the operator supplies the wiring, and `Supervisor.probe` proves the guess by
lighting the glass in front of them.

*This module is the producer half of `ors_schema.link`'s detection messages, and
the obligations it keeps are the ones the schema deliberately does not.* Every
one of them is the same failure -- a reply the server refuses to parse, whose
wait then expires, so an operator reads "timed out" for a question this daemon
answered:

- `ProbeResult.error` is always filled when `ok` is false, and never with `""`.
  The schema permits the pair `ok=False, error=None` on purpose (a
  `model_validator` forcing it would turn a daemon that forgot the reason into a
  refused message), which makes filling it this end's job.
- `error` is truncated to `MAX_PROBE_ERROR`, because an SPI driver is under no
  obligation to be brief and a shortened reason beats a dropped reply.
- `claimed_by` is `None` for a free device and never `""`, which
  `PanelCandidate.claimed_by` refuses -- and it is truncated to
  `MAX_SCREEN_NAME`, because `ScreenConfig.name` has no upper bound at all and a
  hand-written YAML on a Pi can carry a paragraph.
- the candidate list is cut to `MAX_PANEL_CANDIDATES`, for the same reason.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from ors_schema.link import (
    MAX_PANEL_CANDIDATES,
    MAX_PROBE_ERROR,
    MAX_SCREEN_NAME,
    DetectRequest,
    DetectResult,
    PanelCandidate,
    ProbeRequest,
    ProbeResult,
)

from ors_daemon.supervisor import Supervisor

log = logging.getLogger(__name__)

SPI_ROOT = Path("/dev")
"""Where Linux puts the SPI character devices. A module constant, so a test can
replace it without any test in this repo ever reading the real one."""

_SPIDEV = re.compile(r"^spidev(\d+)\.(\d+)$")
"""`/dev/spidev<bus>.<cs>`, and nothing else.

Anchored at both ends deliberately. `/dev` on a Pi holds several hundred entries
and a loose pattern picks up `spidev1.3.old` left behind by an overlay edit, or
`spi0.1` in `/sys`, either of which becomes a row in the wizard for a device that
cannot be opened -- and the operator has no way to tell it from a real one.
"""


def enumerate_panels(root: Path) -> list[tuple[int, int]]:
    """Every SPI device under `root`, as `(bus, chip select)`, in numeric order.

    `root` is a parameter and not a constant, which is what keeps every test of
    this off `/dev`: the answer is a fact about a directory, and a directory is
    something a test can make.

    Sorted by the two numbers rather than by name, because the interface renders
    the list in the order it arrives and `spidev10.0` sorts before `spidev2.0` as
    text. Numeric order is what somebody reading a rack expects.

    A `root` that does not exist enumerates nothing rather than raising. "This
    machine has no SPI" is a real answer -- an unconfigured Pi with no overlay
    enabled gives it -- and a raise here would reach the operator as a detect
    that timed out, which is the one thing this module is arranged to avoid.
    """
    found: list[tuple[int, int]] = []
    try:
        entries = list(Path(root).iterdir())
    except OSError as exc:
        # Logged rather than raised, for the reason above. `iterdir` on a
        # missing, unreadable or not-a-directory path all land here, and none of
        # them is a question the wizard can put to the operator any better than
        # "no panels found".
        log.warning(
            "could not list this rack's devices",
            extra={"path": str(root), "error": str(exc)},
        )
        return []
    for entry in entries:
        match = _SPIDEV.match(entry.name)
        if match is not None:
            found.append((int(match.group(1)), int(match.group(2))))
    return sorted(found)


def detect_handler(supervisor: Supervisor, root: Path) -> Callable[[DetectRequest], DetectResult]:
    """What the rack answers a `DetectRequest` with. Raises nothing worth catching.

    A factory taking the supervisor rather than a closure written at the call
    site, so that what a detection *means* against a rack that is driving panels
    is testable without standing both ends in for.

    Nothing here mutates anything: it lists a directory and reads the running
    configuration. The pairing of the two is the whole answer -- which devices
    exist, and which of them are already spoken for.
    """

    def handle(request: DetectRequest) -> DetectResult:
        claimed = supervisor.claimed_devices()
        found = enumerate_panels(root)
        if len(found) > MAX_PANEL_CANDIDATES:
            # Cut rather than sent, because a list past the schema's bound is a
            # message the server cannot parse: the wizard would show a timeout
            # for a rack that answered with more panels than a rack has.
            log.warning(
                "this machine exposes more SPI devices than the protocol carries; "
                "reporting the first ones",
                extra={"found": len(found), "reported": MAX_PANEL_CANDIDATES},
            )
            found = found[:MAX_PANEL_CANDIDATES]
        return DetectResult(
            request_id=request.request_id,
            panels=[
                PanelCandidate(bus=bus, cs=cs, claimed_by=_claim(claimed.get((bus, cs))))
                for bus, cs in found
            ],
        )

    return handle


def probe_handler(supervisor: Supervisor) -> Callable[[ProbeRequest], ProbeResult]:
    """What the rack answers a `ProbeRequest` with. Always answers.

    `ok` means the device opened and the pattern was written, and never "the
    operator saw it" -- only the person in front of the rack can answer that,
    which is why the wizard asks them afterwards rather than trusting this.

    Every failure is caught, and broadly: `Supervisor.probe` refuses with
    `ProbeRefused`, a backend fails to build with whatever its underlying library
    raises, and an SPI driver raises `OSError` with whatever the kernel said. The
    caller is a link thread whose only job is to put a reply on the socket, so
    the alternative to catching is a request nothing ever answers.
    """

    def handle(request: ProbeRequest) -> ProbeResult:
        try:
            supervisor.probe(
                bus=request.bus,
                cs=request.cs,
                dc=request.dc,
                rst=request.rst,
                hz=request.hz,
                hold_s=request.hold_s,
            )
        except Exception as exc:
            reason = _reason(exc)
            log.error(
                "a probe did not prove a panel",
                extra={"device": f"SPI{request.bus}.{request.cs}", "error": reason},
            )
            return ProbeResult(request_id=request.request_id, ok=False, error=reason)
        return ProbeResult(request_id=request.request_id, ok=True)

    return handle


def _claim(name: str | None) -> str | None:
    """A screen's name as a claim on a device, or None for a device nobody has.

    None and never `""`. `PanelCandidate.claimed_by` refuses the empty string --
    it is falsy, so the natural `if candidate.claimed_by:` reads a claimed device
    as free, and the probe would then take it out from under a live worker --
    and a name that could not have been created cannot honestly be a claim.
    """
    if not name:
        return None
    return name[:MAX_SCREEN_NAME]


def _reason(exc: BaseException) -> str:
    """Why a probe failed, in words that fit on the wire and are never empty.

    The message where there is one, because that is the driver's own account of
    what went wrong and it is shown to whoever pressed the button. The class name
    where there is not -- plenty of exceptions carry no text at all, and
    `ProbeResult.error` refuses `""` for the same reason `claimed_by` does, so a
    reason-shaped hole here is a reply the server drops and a wait that expires.
    """
    said = str(exc).strip()
    return (said or type(exc).__name__)[:MAX_PROBE_ERROR]
