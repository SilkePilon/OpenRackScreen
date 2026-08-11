"""The YAML on disk, turned into the things the daemon runs on.

Three products, all decided once at startup rather than per frame:

- a validated `DaemonConfig`, with a failure that names the field that broke;
- each enabled screen's template scenes and bound parameters, in panel order;
- each screen's *dependencies* -- which integrations it needs before it can show
  anything real, which is what drives the worker's two-stage scene selection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig, ScreenConfig
from ors_schema.scene import Scene, Template
from pydantic import ValidationError

log = logging.getLogger(__name__)

_NAMESPACE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*[.\[]")
"""The leading name of a binding: `{{prom.cpu}}`, `{{ prom.active[0].name }}`.

Anchored on `{{` so an expression's *later* operands are not scanned, and closed
on `.` or `[` so it names a namespace rather than a bare `{{value}}`. The name is
matched whole, so `{{prometheus.x}}` yields `prometheus` -- never the configured
`prom` it happens to start with.
"""


class ConfigError(Exception):
    """The config could not be read, parsed or validated."""


@dataclass(frozen=True)
class ResolvedScreen:
    """One enabled screen, with everything a worker needs to draw it.

    Frozen because it is shared across threads unchanged for the life of a
    config: the worker reads it, the supervisor holds it, nothing edits it.
    `scenes` and `params` are ordinary mutable containers inside that frozen
    shell -- `params` is a fresh dict per screen (see `Template.bind_params`),
    but `scenes` holds the *cached* built-in models, which nothing may mutate.
    """

    config: ScreenConfig
    scenes: list[Scene]
    params: dict[str, Any]
    depends_on: frozenset[str]


def load_config(path: Path) -> DaemonConfig:
    """Read, parse and validate the rack's config, naming what went wrong.

    Every failure is a `ConfigError` carrying the path, because the audience is
    someone editing YAML over SSH: an unreadable file, invalid YAML and a field
    that fails validation are one class of problem to them, and they need the
    file and the field, not a traceback.
    """
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return DaemonConfig.model_validate(parsed)
    except ValidationError as exc:
        # The first error only: pydantic reports every branch of a discriminated
        # union it tried, and a wall of them buries the one line that matters.
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "(root)"
        raise ConfigError(f"{path}: {location}: {first['msg']}") from exc


def _templates(config: DaemonConfig) -> dict[str, Template]:
    """The built-ins, overlaid with the config's own templates.

    The config wins, which is what lets a rack owner amend a built-in without
    forking it -- and is also silent, so a user-defined `ring-gauge` changes
    *every* screen naming `ring-gauge`, not just theirs. That is worth a line in
    the log: it is invisible in the YAML, and the symptom (a screen drawing
    someone else's layout) points nowhere near the cause.
    """
    builtin = load_builtin_templates()
    shadowed = sorted(set(builtin) & set(config.templates))
    if shadowed:
        log.warning(
            "config templates shadow built-ins: %s",
            ", ".join(shadowed),
            extra={"templates": shadowed},
        )
    return {**builtin, **config.templates}


def system_scenes() -> dict[str, Scene]:
    """The `system` template's scenes, keyed by name.

    They carry no `when`, so the daemon selects them by name rather than by
    condition -- see the screen worker's health stage. The scenes are the cached
    built-in models, shared with every other caller: read, never mutate.
    """
    return {scene.name: scene for scene in load_builtin_templates()["system"].scenes}


def _dependencies(scenes: list[Scene], params: dict[str, Any], known: set[str]) -> frozenset[str]:
    """Which of `known` this screen needs before it can show anything real.

    Both halves matter. A screen's params name the integrations *it* chose
    (`{{prom.cpu}}` in a `ring-gauge`), while a template's scenes can name one
    the params never mention (`node-health` binds `{{prom.nodes_ready}}` itself).
    Scanning only params would leave such a screen depending on nothing and
    rendering a blank ring where it should show `connecting`.

    Scenes are scanned as JSON because a binding can sit in any string field at
    any depth -- an element's `text`, a ring's `value`, a group's `repeat.over`.
    The alternative is a walk that must be revisited every time an element gains
    a field. What the dump adds beyond those strings is field *names* and the
    element `type` values, and none of those can contain `{{`, so the extra text
    cannot match. What it does not add is any reading of intent: a `when`
    expression carries no braces, so a namespace named only in a condition is
    not found here.
    """
    text = " ".join([str(value) for value in params.values()])
    text += " " + " ".join(scene.model_dump_json() for scene in scenes)
    return frozenset(name for name in _NAMESPACE.findall(text) if name in known)


def resolve_screens(config: DaemonConfig) -> list[ResolvedScreen]:
    """The enabled screens, in panel order, ready to render.

    Ordered by `position` with a stable sort, so two screens sharing a position
    -- which nothing in the schema forbids, because uniqueness is a rule about
    the set and not about any one screen -- keep the order they were written in
    rather than swapping between runs.
    """
    available = _templates(config)
    known = {integration.name for integration in config.integrations}

    resolved: list[ResolvedScreen] = []
    for screen in sorted(config.screens, key=lambda item: item.position):
        if not screen.enabled:
            continue
        template = available.get(screen.template)
        if template is None:
            raise ConfigError(
                f"screen {screen.name!r} names template {screen.template!r}, which is not defined"
            )
        params = template.bind_params(screen.params)
        resolved.append(
            ResolvedScreen(
                config=screen,
                scenes=list(template.scenes),
                params=params,
                depends_on=_dependencies(list(template.scenes), params, known),
            )
        )
    return resolved
