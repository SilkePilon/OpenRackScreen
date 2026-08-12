"""Turning a pydantic failure into the one line that names what to fix."""

from __future__ import annotations

from pydantic import ValidationError


def first_error(error: ValidationError) -> str:
    """The field path and the reason, as `screens.1.rotation: Input should be ...`.

    Lives here rather than in either consumer because both ends validate the
    *same* model and both have the same audience: someone who just edited a
    screen, over SSH in the daemon's case and in a browser in the server's. A
    message that names the field is the difference between a one-key fix and a
    hunt, and two copies of this formatting would be two chances to drift apart
    on a document both ends have to agree about.

    The first error only: pydantic reports every branch of a discriminated union
    it tried, and a wall of them buries the one line that matters. `(root)` when
    the failure is about the document as a whole and `loc` is therefore empty --
    an extra top-level key, say -- because a bare `: Input should be` reads as a
    formatting bug rather than as a message.
    """
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "(root)"
    return f"{location}: {first['msg']}"
