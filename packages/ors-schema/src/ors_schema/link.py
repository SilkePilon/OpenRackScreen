from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ors_schema.daemon import DaemonConfig

PROTOCOL_VERSION = 1
"""Bumped when a message shape changes incompatibly.

Carried in `hello` so a server meeting an older daemon can say so, rather than
failing on a field neither end can explain. The server checks it before it
authenticates and closes the socket on a mismatch, so bumping this is a
deliberate break of every daemon in the field and not a soft one.
"""

# `hello` is the only message either end reads from a peer it has not
# authenticated, so it is the only one whose bounds are a security property
# rather than tidiness. Every number here is far past anything a real daemon
# sends: 253 is the longest a DNS name may be, the credentials the server mints
# are 43 and 64 characters, and a version string is a handful.
MAX_CREDENTIAL = 256
MAX_HOSTNAME = 253
MAX_VERSION = 64
MAX_CAPABILITIES_BYTES = 4096
"""How much serialised `capabilities` a server will read from a stranger.

A bound on the serialised whole rather than `max_length` on the dict, which
counts keys and would let one key carry a four-megabyte value -- measured, at
4,000,011 bytes. The values are `Any` by design, because the point of the field
is to describe hardware this build has never heard of, so there is no per-value
constraint to hang a limit on and the weight is the only thing left to measure.

Four kilobytes because this is stored: it lands verbatim in
`daemon.capabilities` and from there in every export of the database. A dict
naming the SPI buses, the display backends and a kernel version is a few
hundred bytes.
"""


class _Message(BaseModel):
    """What every message on this link agrees to, in both directions.

    `extra="forbid"` is a safety property here rather than tidiness: with it, a
    field one end invented arrives as a loud error naming the field, and without
    it a message can be *silently mistaken for another* -- every `Heartbeat`
    field has a default, so a stray `ack` body would validate as a heartbeat and
    the ack would vanish with nothing logged anywhere.

    The base64 settings are on the envelope rather than on `Frame`, which is the
    only member carrying bytes today, because the reason is the envelope's: this
    is a JSON transport and JSON has no bytes. Pydantic's default is `utf8` --
    it decodes the payload as text, which raises on a real WebP and would mangle
    one that happened to decode -- so any bytes field added later needs the same
    answer, and getting it by default is better than remembering to. Pydantic
    emits the URL-safe alphabet (`-_`), which is what a non-Python consumer must
    decode with; it accepts either alphabet, padded or not, on the way in.
    """

    model_config = ConfigDict(extra="forbid", ser_json_bytes="base64", val_json_bytes="base64")


class Hello(_Message):
    type: Literal["hello"] = "hello"
    token: str = Field(repr=False, max_length=MAX_CREDENTIAL)
    """The credential: the one-time pairing token, or the key `Paired` handed back.

    One field for both, which is what the design calls `token_or_key`. They are
    never ambiguous -- a token is spent the moment it is claimed, so at most one
    of the two can match any row -- and a daemon that had to choose *which*
    field to send would have to know which state the server thinks it is in.
    With one field it sends whichever credential it holds and the server decides
    what that is, which is the only order that survives a pairing whose reply
    was lost.

    Kept out of `repr` because it is the credential. A model's `repr` is what
    reaches a log the moment anyone writes `log.info("hello", extra={"message":
    hello})` or drops one into an exception -- and this one pairs a rack. It
    still serialises normally, because it has to travel; it just does not travel
    into a log by accident.
    """
    hostname: str = Field(max_length=MAX_HOSTNAME)
    """What the daemon calls itself. Bounded because it identifies nobody.

    It is not a credential and is never treated as one, so the only thing it
    does is get logged -- including on the path where the credential matched
    nothing, which is a log line an unauthenticated peer can ask for as many
    times as it can open sockets. Unbounded, that is an unbounded write per
    refused connect.
    """
    daemon_version: str = Field(max_length=MAX_VERSION)
    config_version: int | None = None
    """The `ConfigPush.version` this daemon is running, or None for none at all.

    What makes "daemons reconnect and re-ack their version; nothing is
    re-pushed if the versions already match" implementable: on a reconnect the
    server has a version in its database and no way to learn what is actually
    on the Pi, so without this every reconnect is a push. A push is not cheap
    -- applying a snapshot revokes every panel, joins every worker and reopens
    them -- so an unnecessary one is a full teardown and repaint of the rack,
    and a wifi blip becomes a rack-wide flicker.

    None means "I have no config", which is what a fresh daemon says and what a
    rebooted one says when its cache is gone. Not 0, which is a version like any
    other and the one an empty server counts from: a daemon claiming 0 against a
    server that has pushed nothing would match, and the rack would stay blank.
    None never matches anything, so the answer to it is always to push.

    It is a claim, not proof. The server may only *skip* a push on a match; the
    ack remains the only evidence that a config is applied and running, and this
    field is cleared from the hub's memory on every register for that reason.
    """
    protocol_version: int = PROTOCOL_VERSION
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def _small_enough_to_store(cls, capabilities: dict[str, Any]) -> dict[str, Any]:
        # `default=str` so that a dict assembled in Python rather than parsed
        # from the wire cannot turn a length check into a `TypeError` escaping
        # as something other than a validation failure. Nothing off the wire
        # reaches it; everything there is JSON already.
        weight = len(json.dumps(capabilities, default=str))
        if weight > MAX_CAPABILITIES_BYTES:
            raise ValueError(
                f"capabilities is {weight} bytes, over the {MAX_CAPABILITIES_BYTES} allowed"
            )
        return capabilities


class Heartbeat(_Message):
    type: Literal["heartbeat"] = "heartbeat"
    uptime_s: int = 0
    status: dict[str, Any] = Field(default_factory=dict)


class Ack(_Message):
    """The snapshot is applied and running.

    `config_version` echoes the `ConfigPush.version` being answered -- the
    server's generation counter, not `DaemonConfig.version`. It is the whole
    answer to "did the Pi apply what I sent?", so it names a push rather than
    describing the config, and a stale ack is recognisable as an old number.
    """

    type: Literal["ack"] = "ack"
    config_version: int


class Nack(_Message):
    """The snapshot was refused and the previous config is still running.

    `reason` is carried because the server is where a human is looking: a
    validation error that stays on the Pi is a rack that quietly ignored an
    edit. Same `config_version` as `Ack` -- the push being answered.
    """

    type: Literal["nack"] = "nack"
    config_version: int
    reason: str


class SourceStatus(_Message):
    type: Literal["source_status"] = "source_status"
    integration: str
    state: str
    reason: str | None = None
    latency_ms: float | None = None


MAX_FRAME_BYTES = 256 * 1024
"""The largest encoded panel image either end of this link will carry.

The one number about a frame that both ends have to agree on, which is why it
lives here rather than in the daemon that encodes them or the server that reads
them. A message past a reader's limit is not a dropped message: `websockets`
closes the socket over it, so a daemon that produced one would reconnect, be
pushed to, send the same frame again and close again -- a loop in which the rack
keeps drawing, the browser shows nothing, and neither end's log says why. With a
bound on the field, an oversized frame is one `ValidationError` the server logs
and skips, and a number the daemon can check against before it writes anything.

Measured rather than guessed. A rendered 240x240 panel is 738 bytes
(`multi-ring`) to 5,042 (`ring-gauge`) as WebP at quality 60, method 0; the
absolute worst case for that geometry -- uniform random noise, which no template
can produce -- is 32,992. A quarter of a mebibyte is eight times that worst case
and a fiftieth of the daemon's own `MAX_MESSAGE_BYTES`, so a screen size or an
encoder setting can move by a lot before this has to.
"""


class Frame(_Message):
    """One rendered panel image, on its way to a browser watching the rack.

    `webp` is the only `bytes` on this link, and the reason `_Message` sets
    `ser_json_bytes="base64"`: pydantic's default would try to decode a WebP as
    UTF-8. `seq` is what lets a congested link drop rather than queue -- the
    newest frame wins, and the receiver can tell which one that is.

    `seq` counts per screen and is not reset by a subscription ending, a
    reconfiguration or a worker being replaced, because a receiver that drops
    anything not newer than the last frame it drew cannot tell a restarted
    counter from a flood of stale frames. It does restart when the daemon does,
    so a receiver should read a `seq` that has gone *backwards* as a new stream
    rather than as something to drop.
    """

    type: Literal["frame"] = "frame"
    screen_id: int
    seq: int
    # Bounded on the decoded bytes and not on the base64 that carries them: the
    # wire form is 4/3 of this, and the thing that has to fit in a reader's
    # limit is the message, so the bound has to be on the payload it is derived
    # from. See `MAX_FRAME_BYTES` for what a real panel weighs.
    webp: bytes = Field(max_length=MAX_FRAME_BYTES)


class LogLine(_Message):
    type: Literal["log"] = "log"
    level: str
    message: str


class ConfigPush(_Message):
    """A whole configuration, never a patch.

    `snapshot` is `DaemonConfig` itself -- the same model the daemon loads from
    its YAML file -- so a pushed document goes through exactly the validation a
    hand-written one does, and the apply path and the file path converge on one
    validated object. A partial push would need merge rules that both ends
    implement identically, and the first disagreement would be a rack in a state
    neither end can describe.
    """

    type: Literal["config"] = "config"
    version: int
    """The server's generation counter, and what an `Ack` refers to.

    Not `DaemonConfig.version`, which is the config *schema* version and a
    constant, and not the daemon's status `config_fingerprint`, which is a
    content hash. Three different questions; three different fields.
    """
    snapshot: DaemonConfig


class Paired(_Message):
    """The pairing token is spent; this is what to present from now on.

    Sent once, immediately after a token is claimed, and never again. The
    daemon writes `key` into its local state and sends it as `Hello.token` on
    every later connect -- which is what stops a reconnect from being
    authenticated by something as forgeable as a hostname. The server keeps only
    a hash of it.

    A message of its own rather than a field on `ConfigPush`, for two reasons.
    A pushed config is a document the daemon caches to disk to boot from when
    the server is down, so a credential riding inside it would be written into
    that cache on every push, by whatever writes the cache, forever. And a
    config push is refusable -- a daemon that nacks a snapshot it cannot apply
    must still keep the key it was just given, or a rack with one bad screen
    could never reconnect.

    `daemon_id` is not a secret and is carried because the daemon logs it and
    the interface addresses the rack by it; `key` is, and so is out of `repr`
    for the reason `Hello.token` is.
    """

    type: Literal["paired"] = "paired"
    daemon_id: int
    key: str = Field(repr=False)


class Command(_Message):
    type: Literal["command"] = "command"
    command: Literal["identify", "sleep", "wake", "reload"]
    screen_id: int | None = None


class FramesRequest(_Message):
    type: Literal["frames"] = "frames"
    enabled: bool
    screen_ids: list[int] = Field(default_factory=list)
    fps: float = 2.0


DaemonMessage = Annotated[
    Hello | Heartbeat | Ack | Nack | SourceStatus | Frame | LogLine,
    Field(discriminator="type"),
]
ServerMessage = Annotated[
    ConfigPush | Command | FramesRequest | Paired, Field(discriminator="type")
]

_daemon_adapter: TypeAdapter[DaemonMessage] = TypeAdapter(DaemonMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_daemon_message(raw: str | bytes) -> DaemonMessage:
    """Parse one frame from the daemon, or raise `ValidationError`.

    Raising is the point. A type this build does not know is version skew, and
    the two ends should discover it here -- as `union_tag_invalid`, naming every
    type that *is* known -- rather than one end silently dropping a message the
    other believes it sent. `bytes` is accepted as well as `str` because a
    WebSocket library hands over whichever the peer framed.
    """
    return _daemon_adapter.validate_json(raw)


def parse_server_message(raw: str | bytes) -> ServerMessage:
    """Parse one frame from the server, or raise `ValidationError`.

    A separate union from `parse_daemon_message`, not one union of everything:
    each end accepts only what the other is allowed to say, so a message sent
    down the wrong direction fails here instead of being acted on.
    """
    return _server_adapter.validate_json(raw)
