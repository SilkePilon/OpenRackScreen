"""The YAML on disk, turned into the things the daemon runs on.

Three products, all decided once at startup rather than per frame:

- a validated `DaemonConfig`, with a failure that names the field that broke;
- each enabled screen's template scenes and bound parameters, in panel order;
- each screen's *dependencies* -- which integrations it needs before it can show
  anything real, which is what drives the worker's two-stage scene selection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ors_render import load_builtin_templates
from ors_schema.daemon import DaemonConfig, ScreenConfig
from ors_schema.errors import first_error
from ors_schema.scene import Scene, Template
from pydantic import ValidationError

log = logging.getLogger(__name__)

_BINDING = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
"""One binding's body. Lazy, so it stops at the first `}}` -- the same pattern,
and the same reasoning, as `ors_render.bindings`."""

_REFERENCE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*[.\[]")
"""The head of a namespace reference: the `prom` of `prom.cpu` or `prom.hot[0]`.

Applied to a whole *expression*, not to a binding's first token, so every operand
counts: `100 - prom.cpu`, `len(prom.hosts)` and `prom.a + qbit.b` all yield their
namespaces. Two guards keep that from over-reaching.

`(?<![\\w.])` refuses a name that follows a dot, so only the head of a chain is a
namespace: `prom.active[0].name` yields `prom` and not `active`, which matters
because a field of one integration may well share a name with another.

The name is matched whole -- word boundary on the left, greedy identifier on the
right -- and then compared for equality against the configured names, so neither
half of a substring pair can be mistaken for the other: `prometheus_extra.x`
yields `prometheus_extra`, never `prom`, and a configured `prometheus_extra` is
not matched by a reference to `prom.x`.
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
        raise ConfigError(f"{path}: {first_error(exc)}") from exc


_FINGERPRINT_CHARS = 12
"""Enough of the digest to identify a config, and short enough to read out loud.

48 bits. What it has to survive is a person comparing two of these over SSH and
a server deciding whether the Pi is running what it pushed -- a set of a few
hundred configs at the very most, not an adversary hunting a collision -- and at
that scale the odds of two differing configs sharing twelve hex characters are
around one in 10^11.
"""


def config_fingerprint(config: DaemonConfig) -> str:
    """A short, stable identity for the *contents* of a config.

    `DaemonConfig.version` cannot answer "did the Pi apply the config I pushed?"
    and never could: it is a `Literal[1]`, the version of the *schema*, so it
    reads the same for every document that has ever validated. The status file
    -- which M3 forwards verbatim -- therefore needs something that moves when
    the config does, and this is it. The two are reported side by side under
    names that cannot be confused: `config_schema_version` and this.

    Taken over the validated model rather than over the file, which is what makes
    it a fact both ends can compute. The server holds a document and the Pi holds
    whatever it parsed; hashing the bytes would make a reformatted YAML file, a
    changed comment or the same JSON with its keys in another order look like a
    different rack. `mode="json"` and `sort_keys` reduce both ends to the same
    canonical form, so what is left is exactly what the daemon will act on --
    every screen, every query, every pin. List order is preserved because it is
    meaningful: two screens swapping position is a different rack.
    """
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:_FINGERPRINT_CHARS]


def _templates(config: DaemonConfig) -> dict[str, Template]:
    """The built-ins, overlaid with the config's own templates.

    The config wins either way, which is what lets a rack owner amend a built-in
    without forking it, and is what keeps a pushed snapshot honest: the server's
    copy of a template is the one the browser drew its preview from, so agreeing
    with it is the point.

    What is worth logging is which of the two just happened, and they are told
    apart by `Template.builtin`:

    *A template the config declares as its own* (`builtin=False`) shadowing a
    built-in is a user override, and a silent one -- a hand-written `ring-gauge`
    changes *every* screen naming `ring-gauge`, not just theirs, which is
    invisible in the YAML and whose symptom (a screen drawing someone else's
    layout) points nowhere near the cause. That is a warning.

    *A built-in arriving from the server* (`builtin=True`) is the normal case:
    every snapshot carries all of them, so warning would be seven lines on every
    config load and no signal at all. Only a *difference* from this build's own
    copy says anything -- the two ends are on different wheels -- and that is one
    INFO line, when it happens.
    """
    builtin = load_builtin_templates()
    overridden = sorted(
        name
        for name, template in config.templates.items()
        if name in builtin and not template.builtin
    )
    if overridden:
        log.warning(
            "config templates shadow built-ins: %s",
            ", ".join(overridden),
            extra={"templates": overridden},
        )
    differing = sorted(
        name
        for name, template in config.templates.items()
        if name in builtin and template.builtin and template != builtin[name]
    )
    if differing:
        log.info(
            "the server's copy of these built-ins differs from this build's: %s",
            ", ".join(differing),
            extra={"templates": differing},
        )
    return {**builtin, **config.templates}


def system_scenes() -> dict[str, Scene]:
    """The `system` template's scenes, keyed by name.

    They carry no `when`, so the daemon selects them by name rather than by
    condition -- see the screen worker's health stage. The scenes are the cached
    built-in models, shared with every other caller: read, never mutate.
    """
    return {scene.name: scene for scene in load_builtin_templates()["system"].scenes}


def _names_in_text(text: str) -> Iterator[str]:
    """Namespaces referenced by the bindings inside a piece of scene text.

    Only inside the braces: `peak: {{prom.hot.node}}` is a label plus a binding,
    and the label is prose that must not be read as an expression.
    """
    for body in _BINDING.findall(text):
        yield from _REFERENCE.findall(body)


def _names_in_dump(node: Any, key: str | None = None) -> Iterator[str]:
    """Every namespace a dumped scene refers to, at any depth.

    A generic walk rather than a typed one because a binding can sit in any
    string field of any element -- an element's `text`, a ring's `value`, a
    group's `repeat.over` -- and a walk that knows the element classes has to be
    revisited every time one gains a field.

    The two string kinds are told apart by the field they arrived in, which is
    the only thing that distinguishes them: a `when` is a bare expression
    (`prom.alerts == 0`), while every other field carries `{{...}}` bindings
    inside surrounding text. Scanning a `when` as if it were binding text finds
    nothing at all -- the gap that left a `torrent` screen not depending on the
    Prometheus its own health gate reads.
    """
    if isinstance(node, Mapping):
        for name, value in node.items():
            yield from _names_in_dump(value, str(name))
    elif isinstance(node, list):
        for item in node:
            yield from _names_in_dump(item, key)
    elif isinstance(node, str):
        yield from _REFERENCE.findall(node) if key == "when" else _names_in_text(node)


def _dependencies(scenes: list[Scene], params: dict[str, Any], known: set[str]) -> frozenset[str]:
    """Which of `known` this screen needs before it can show anything real.

    Both halves matter. A screen's params name the integrations *it* chose
    (`{{prom.cpu}}` in a `ring-gauge`), while a template's scenes can name one
    the params never mention (`node-health` binds `{{prom.nodes_ready}}` itself,
    `torrent` reads `prom.alerts` in its scene's `when`). Scanning only params
    would leave such a screen depending on nothing and rendering a blank ring
    where it should show `connecting`.

    Params are scanned as binding text, never as expressions, whatever they are
    named: a param is a value the template interpolates, and a screen that
    happened to declare one called `when` would otherwise have its *value* read
    as a condition.

    The intersection with `known` is what does the filtering, so an over-broad
    candidate -- a repeat alias, a name inside a string literal in an expression
    -- costs nothing unless it collides with a configured integration name.
    """
    names: set[str] = set()
    for value in params.values():
        names.update(_names_in_text(str(value)))
    for scene in scenes:
        names.update(_names_in_dump(scene.model_dump()))
    return frozenset(names & known)


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
