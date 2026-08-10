"""The templates that ship with the renderer, as JSON on disk.

Together these seven reproduce every screen the rack draws today: `ring-gauge`
(CPU, MEM), `big-number` (PODS), `node-health` and `torrent` (the HEALTH screen
and the download view it switches into), plus `multi-ring`, `text-only` and the
reserved `system` scenes.

They are data rather than Python because a template is exactly what the editor
lets a user copy and change: shipping the built-ins in the same form a
user-authored one takes means the editor, the validator and the daemon have one
kind of template to deal with, and a built-in cannot quietly depend on something
JSON cannot express.

Reading them off disk is the same filesystem exception as the bundled fonts --
package data, next to the code, no network and no clock.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ors_schema.scene import Template

BUILTIN_DIR = Path(__file__).parent / "builtin"


@lru_cache(maxsize=1)
def load_builtin_templates() -> dict[str, Template]:
    """Every built-in template, keyed by name, parsed once per process.

    The cache makes this cheap to call per frame, which is how the daemon uses
    it -- at the cost of handing every caller the *same* dict of the same
    models, so nothing may mutate what it gets back. Pydantic models are the
    natural shape for that discipline: an editor derives a changed template with
    `model_copy`, it does not edit the built-in in place.

    A template whose JSON does not validate raises here rather than degrading to
    a missing screen: unlike a scene from the database, this file is shipped
    inside the wheel, so a bad one is a build that should never have been made.
    """
    templates: dict[str, Template] = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        template = Template.model_validate(json.loads(path.read_text(encoding="utf-8")))
        templates[template.name] = template
    return templates
