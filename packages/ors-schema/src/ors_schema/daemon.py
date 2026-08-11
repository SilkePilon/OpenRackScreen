from __future__ import annotations

import os.path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ors_schema.scene import Template

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
"""A wall-clock time of day, zero-padded, as `HH:MM`.

A string rather than `datetime.time` because this is the config's on-the-wire
form: it is hand-written in YAML in M2 and pushed as JSON by the server in M3,
and both round-trip a string unchanged where a `time` needs a serializer at each
end. The pattern is anchored at both ends and admits only 00:00-23:59, so the
consumer can split on `:` and trust two integers without re-checking them.
"""


class NightWindow(BaseModel):
    """When a screen sleeps.

    The window wraps midnight when `start > end`, which is the normal case and
    the reason this is two times rather than a start plus a duration: the rack
    owner thinks in "dark between 23:00 and 07:00", not in "dark for 8h".
    Interpreting the wrap is the daemon's job -- this model reads no clock.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    start: HHMM = "23:00"
    end: HHMM = "07:00"


class TunnelConfig(BaseModel):
    """A `kubectl port-forward` to reach a Prometheus that is not routable.

    Present because the author's Prometheus lives in a cluster: without this the
    only way to poll it is to run the daemon inside the cluster too.
    """

    model_config = ConfigDict(extra="forbid")

    kubeconfig: str
    namespace: str
    # "auto" means the daemon discovers the service in `namespace` rather than
    # the config naming it, so a chart that renames its service on upgrade does
    # not need a config edit. Empty is neither: it discovers nothing and names
    # nothing, and only surfaces as a permanent "no service to forward".
    service: str = Field(default="auto", min_length=1)
    remote_port: int = Field(ge=1, le=65535)
    local_port: int = Field(ge=1, le=65535)

    @property
    def kubeconfig_path(self) -> str:
        """`kubeconfig` with `~` resolved -- what to hand an actual `kubectl`.

        Every example config in the spec and the plan writes `~/k8s-monitor.yaml`,
        and nothing expands it: `subprocess` does no shell expansion, and client-go
        does not expand `~` for an explicit `--kubeconfig`. kubectl therefore fails
        to stat the literal path and dies at every launch, forever.

        Derived rather than expanded in a validator, and derived *here* rather than
        at the daemon's call sites, for the same reason: expansion is a fact about
        the machine that runs kubectl, while the model is a document M3's server
        produces and pushes down. A validator would bake the server's home into a
        document destined for the Pi -- silently, since expanding an absolute path
        is a no-op and a round-trip test cannot see it. A property resolves on the
        machine that reads it, leaves `model_dump` verbatim, and is still one
        definition every consumer shares.
        """
        return os.path.expanduser(self.kubeconfig)


class FieldSpec(BaseModel):
    """One named reading a screen can bind to, and how to get a number from it.

    A PromQL query returns a vector, and a 240x240 round panel shows one value,
    so something has to collapse the two: `reduce` is that decision, written
    beside the query instead of in the template, because it depends on the query
    and not on how the result is drawn.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    reduce: Literal["scalar", "top"] = "scalar"
    # Which series label identifies a `top` result -- the row's name, not its
    # value. Ignored under `reduce: scalar`, where there is nothing to name.
    label: str = "instance"
    # `last_octet` shortens `192.168.1.5:9100` to `.5` -- the leading dot kept,
    # because that is what `k8s_monitor.py` put on the glass and what M1's parity
    # goldens show. An instance label is routinely wider than the panel, and
    # truncating it from the left is what keeps the digits that differ between
    # hosts; the dot is what says the rest was truncated. It renders as
    # `peak: .5 71%`.
    strip: Literal["none", "last_octet"] = "none"


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["prometheus"] = "prometheus"
    name: str
    poll_interval: float = Field(default=5.0, gt=0)
    url: str
    timeout: float = Field(default=4.0, gt=0)
    tunnel: TunnelConfig | None = None
    # An integration that queries nothing polls a server for no reason and can
    # feed no screen, so it is a config mistake rather than a degenerate case.
    fields: dict[str, FieldSpec] = Field(min_length=1)


# One member today, plus qBittorrent in M5. No `Union[...]` is written because a
# one-member union collapses to the member itself at runtime -- `Union[X] is X` --
# so the wrapper would be decoration. The `discriminator` is what does the work:
# pydantic builds a real tagged union from it even around a lone model, so an
# unrecognised `type` fails as `union_tag_invalid` and a missing one as
# `union_tag_not_found`, not as a `Literal` mismatch inside a per-member attempt.
# Adding the second member is then `PrometheusConfig | QBittorrentConfig` here and
# nothing else: the error shapes callers see do not change.
IntegrationConfig = Annotated[PrometheusConfig, Field(discriminator="type")]


class DisplayConfig(BaseModel):
    """Where a screen's pixels go: a real panel, or a directory of PNGs.

    The SPI fields describe a GC9A01 wired to a Pi; `out_dir` is the virtual
    backend's. They share a model rather than splitting into a discriminated
    union because switching a screen between the two is a one-key edit during
    development, and a union would make it a rewrite of the whole block.

    Two rules are enforced, and they are the same rule seen from either end: a
    backend's own required field must be present. `virtual` needs `out_dir`, and
    `gc9a01` needs `dc` and `rst` -- both are statements about the document
    alone, decidable by anyone holding it.

    Nothing beyond that. The pin *numbers* are deliberately unvalidated: which
    buses, chip selects and GPIO lines exist is a fact about the specific board,
    unknown to a schema that the M3 server also parses on a different machine
    with no view of the rack's wiring. No range check, and no `dc != rst` rule.
    The backend touches the hardware and gets the real error; guessing earlier
    only produces a worse one.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["gc9a01", "virtual"]
    spi_bus: int = 0
    spi_cs: int = 0
    # No default, unlike the bus and chip select: those have one obviously right
    # value (SPI0.0) and a wrong one only misaddresses a device, while DC and RST
    # are wired wherever the builder had a free header pin. Defaulting both to 0
    # described a panel driving data/command and reset off GPIO0 -- a board that
    # does not exist -- and made `DisplayConfig(backend="gc9a01")` a valid
    # description of nothing, failing hours later as an SPI error on the Pi.
    dc: int | None = None
    rst: int | None = None
    hz: int = 40_000_000
    out_dir: str | None = None

    @model_validator(mode="after")
    def _backend_carries_its_own_requirements(self) -> DisplayConfig:
        if self.backend == "virtual" and not self.out_dir:
            raise ValueError("a virtual display needs out_dir")
        if self.backend == "gc9a01":
            missing = [name for name in ("dc", "rst") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"a gc9a01 display needs {' and '.join(missing)}")
        return self


class ScreenConfig(BaseModel):
    """One panel: what it draws, with what parameters, and how it is mounted."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Which physical panel in the rack, left to right, counted from 1: the rack
    # has a first panel, not a zeroth one, and every example config and every
    # log line written so far says 1. Distinct from the list index so reordering
    # the YAML, or disabling a screen, does not move the remaining ones.
    # Uniqueness across `DaemonConfig.screens` is not checked here -- that is a
    # rule about the set, and it belongs to whatever assigns panels to hardware.
    position: int = Field(ge=1)
    display: DisplayConfig
    # Quarter turns only: a panel is bolted in at whatever angle its ribbon cable
    # allows, and the worker corrects for that by transposing pixels. An
    # arbitrary angle would mean resampling a 240x240 circle every frame.
    rotation: Literal[0, 90, 180, 270] = 0
    hflip: bool = False
    enabled: bool = True
    # Resolved against the built-ins `ors-render` ships and against the *keys* of
    # `DaemonConfig.templates` -- not against `Template.name`, which the key may
    # disagree with and which nothing here reconciles. Empty names no built-in
    # and no key, so it is rejected rather than deferred to a lookup miss.
    template: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    # `None` means "follow `DaemonConfig.night`". A screen sets its own only to
    # differ from the rack -- a lights-out NAS panel that never sleeps, say --
    # and the fallback lives here so the common case stays unwritten.
    sleep_override: NightWindow | None = None


class DaemonConfig(BaseModel):
    """The whole rack's configuration.

    Hand-written as YAML in M2 and pushed verbatim by the server over a WebSocket
    in M3, which is why it lives in `ors-schema` rather than in the daemon: the
    two must agree on it exactly, and the daemon must not be able to tell which
    of the two produced the document it is holding.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    timezone: str = "UTC"
    night: NightWindow = Field(default_factory=NightWindow)
    integrations: list[IntegrationConfig] = Field(default_factory=list)
    screens: list[ScreenConfig] = Field(default_factory=list)
    # User-defined templates, by name, alongside the built-ins that `ors-render`
    # ships. A screen's `template` resolves against both -- and against *this
    # dict's key*, not the `Template.name` inside the value, which may differ and
    # which nothing here reconciles.
    templates: dict[str, Template] = Field(default_factory=dict)
