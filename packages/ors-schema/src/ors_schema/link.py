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

It is bounded from above as well, and by a specific number rather than by taste.
uvicorn's `ws_max_size` defaults to 16,777,216 -- sixty-four of these -- and
that is the reader limit on the server's end of this link. This bound is only
worth having while it is comfortably *under* the transport's: at 16 MiB the two
coincide and the reconnect loop above is reachable again, and long before that a
daemon holding a valid key is making the server allocate whatever it sends,
per message, before any field bound can look at it. The server therefore sets
`ws_max_size` explicitly from this number (see `ors_server.__main__`) instead of
inheriting a default that has nothing to do with a panel.
"""


def not_a_flag(value: object, *, what: str = "a screen id is a row id") -> object:
    """Refuse a JSON `true` where a row id belongs, on every message that carries one.

    `bool` is an `int` in Python and pydantic's lax mode takes it as one, so
    `{"screen_id": true}` validates as `1` and addresses whatever screen holds
    row id 1. On a `Frame` that is one rack's panel painted with another's
    picture, with nothing anywhere saying so; on a `Command` it is `identify`
    lighting up somebody else's glass.

    Here rather than at the two ends that read these fields, which is where it
    was answered twice already -- `ws_ui._Request` on the browser socket and
    `ws_daemon._about` in a log field. A rule about what a field may hold belongs
    to the field, and a third copy at a third caller is a fourth caller waiting
    to forget it.

    Public, and that is the point of it. A request body upstream of one of these
    messages is a field of the same kind: `daemons.CommandBody.screen_id` widened
    `true` to `1` before `Command` was ever constructed, so the validator below
    saw an integer and passed it, and `sleep` darkened whichever panel holds row
    id 1. The fix for a field that has to obey this rule is to call this, not to
    write it again.

    A validator and not `StrictInt`, because strictness would also refuse the
    `"7"` a JSON producer may legitimately send for an integer; what is wrong
    with `true` is that it is not a number at all.

    `what` names the kind of number the field holds, because the rule is now
    obeyed by fields that are not row ids: a `ProbeRequest.dc` refusing `true`
    with "a screen id is a row id" would be a correct refusal carrying a false
    reason, and the reason is the whole of what an operator reads out of a
    `ValidationError`. The default is the original sentence, so every caller
    that had one before this argument existed produces the same message.
    """
    if isinstance(value, bool):
        raise ValueError(f"{what}, not a flag")
    return value


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

    @field_validator("screen_id", mode="before")
    @classmethod
    def _screen_id_is_a_row_id(cls, screen_id: object) -> object:
        return not_a_flag(screen_id)


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
    """Which panel, or None for the whole rack -- which is what `sleep` usually means.

    None passes the validator below untouched: an absence is not a flag, and a
    check written as `if not screen_id` would refuse the commonest command there
    is.
    """

    @field_validator("screen_id", mode="before")
    @classmethod
    def _screen_id_is_a_row_id(cls, screen_id: object) -> object:
        return not_a_flag(screen_id)


MAX_REQUESTED_FPS = 60.0
"""The fastest rate this link will carry in a `frames` request at all.

A bound on the *message*, which is this module's business, and not the daemon's
ceiling, which is not: `ors_daemon.frames.MAX_FPS` is five, it is a policy about
how much of one Pi's CPU a browser may spend, and a rack with different hardware
could reasonably choose differently. Both exist, and the redundancy is the
point -- `MAX_FPS` says why in the threat-model terms this end cannot see.

What it is really for is the values that are not rates. `fps` was a bare
`float`, and pydantic's `allow_inf_nan` defaults to true, so `{"fps": NaN}`
parsed off the wire and then defeated every comparison a rate limiter makes:
`min(nan, cap)` is `nan`, `nan <= 0.0` is False so it does not read as "no
frames", and `now - last < nan` is False for ever. Measured: 100 offers accepted
out of 100 with the clock frozen, against a cap that allows one -- and sticky,
because nothing reset the number afterwards. Sixty because it is past anything a
panel could show and far past anything one would ask a Pi for, so a legitimate
request never meets it.
"""


MAX_WATCHED_SCREENS = 64
"""How many screens one `frames` request may name.

The third hole of the family the `fps` bound closed two of, and the last one
left open. `screen_ids` is not a filter the daemon applies to work it was doing
anyway: `FrameStream.enable` does `self._enabled = set(screen_ids)`, so this
list *is* the daemon's streaming state, and `_MAX_TRACKED_SCREENS` prunes the
rate limiter's memory and the per-screen sequence counters down to it. An
unbounded list is therefore one message deciding how much of a Pi's memory a
subscription costs, and -- because the prune can only shrink those tables to
what is enabled -- one that also decides whether the prune can do anything at
all.

Sixty-four because of what the number has to accommodate, which is not "how many
screens exist" but "how many screens of *one* daemon are being watched at once".
A rack is the four panels a Pi drives over SPI; a large one is a handful more.
An order of magnitude past that leaves room for a rack nobody has built yet
without ever meeting a legitimate request, and it sits well under the daemon's
own `_MAX_TRACKED_SCREENS` of 256, which is the table this list resizes -- the
two are pinned against each other in the daemon's suite, at the end that owns
the smaller number.

The server bounds what it *sends* as well, rather than letting a rack with more
watched screens than this raise inside the handler that assembles a request:
`ors_server.link.hub.Hub.frames_for` truncates and logs, and is the only place
either socket builds one. A bound that turns a large rack into an exception
would freeze every panel on it, which is worse than streaming sixty-four of
them -- and it did worse than that at the daemon's end, where the exception
landed after the connection was registered and left the rack reconnecting in a
loop while every browser showed it online.
"""


class FramesRequest(_Message):
    type: Literal["frames"] = "frames"
    enabled: bool
    screen_ids: list[int] = Field(default_factory=list, max_length=MAX_WATCHED_SCREENS)
    # `allow_inf_nan=False` as well as the two bounds, although either would
    # refuse a NaN on its own -- every comparison against one is False, so
    # `ge` rejects it. Said explicitly because the *reason* it must be rejected
    # is the infinity and the not-a-number rather than the range, and a later
    # edit that loosened the range would otherwise quietly take the other bound
    # with it. See `MAX_REQUESTED_FPS`.
    fps: float = Field(default=2.0, ge=0.0, le=MAX_REQUESTED_FPS, allow_inf_nan=False)


MAX_REQUEST_ID = 64
"""How long a correlation id either end will carry.

The server mints these and the daemon echoes whatever it was given, which is
what makes this a bound worth having: the id is the *key* of the server's table
of waits, held there until the reply arrives or the wait expires, so its size is
chosen by whatever the daemon sends back. A uuid4 is 32 hex digits, or 36 with
its dashes; sixty-four leaves room for a prefix without ever meeting an honest
id.

Bounded below at 1 as well, on both the request and the reply. An empty id is
not a correlation key: it either matches no wait at all, or -- if anything ever
registers one under `""` -- matches whichever wait a bug put there, which is the
failure this whole field exists to prevent.
"""

MAX_PANEL_CANDIDATES = 64
"""How many SPI devices one `DetectResult` may name.

The daemon builds this by listing `/dev/spidev*`, so its length is decided by
the filesystem of a machine the server cannot see, and the interface renders one
row per entry. A Pi exposes a handful -- `spi0.0`, `spi0.1` and whatever the
overlays add -- and a rack is four panels. Sixty-four is the same order of
magnitude past reality as `MAX_WATCHED_SCREENS`, and for the same reason: far
enough past a rack nobody has built yet that a legitimate result never meets it.
"""

MAX_SCREEN_NAME = 64
"""How long a `claimed_by` may be: the screen name, and nothing else.

The same number the server bounds a screen name with when one is created
(`ors_server.api.screens.MAX_NAME`), stated again rather than imported, because
this package may not import the server and the two ends have to agree anyway. A
name that could not have been created cannot honestly be a claim on a device.
"""

MAX_WIRING_NUMBER = 65_535
"""The ceiling on `bus`, `cs`, `dc` and `rst` -- a bound on an integer, not on a board.

Deliberately not a GPIO range. `DisplayConfig` does not validate pin numbers at
all, and says why: which buses, chip selects and GPIO lines exist is a fact
about one specific board, unknown to a schema that a server on another machine
also parses. Nothing here knows better, and a probe that failed with "pin 47 is
out of range" when the board has 54 of them would be a schema inventing hardware.

What is left is the wire's own business: an int off a JSON socket is arbitrary
precision in Python, so `{"dc": 1e400}` -- or a literal with twenty thousand
digits -- is a number nobody wrote down as a pin, and it travels on into a
`DisplayConfig` and from there into the database. Sixty-five thousand is past
every numbering scheme any board uses (a Pi has 28 header GPIOs; Linux's global
GPIO space is configured in the hundreds), so this refuses magnitudes rather
than pins: everything inside it still reaches the hardware, which is the only
thing that can give the real error.

Bounded below at 0 for the same reason and no more: these are indices --
`/dev/spidev<bus>.<cs>` and a GPIO line number -- and index-ness is not a fact
about any particular rack's wiring.
"""

MAX_SPI_HZ = 2**32 - 1
"""The ceiling on a probe's clock, and it is an ABI fact rather than a rack's.

`max_speed_hz` is a 32-bit field in Linux's `spidev` ioctl, so a number past
this cannot be asked of any SPI driver on any board -- it is not a slow clock or
a fast one, it is not a clock. That makes it the one bound this schema can state
about `hz` without guessing at hardware: what a *particular* panel will tolerate
is the driver's business (the daemon's own default is 40 MHz), and a probe at a
speed the glass cannot follow is exactly the thing the operator ran the probe to
find out.

Zero is refused because a probe has to name the speed it is proving; `spidev`
reads a zero as "keep whatever was set", which would make the probe pass at a
speed nobody chose and the wizard write that speed into a config.
"""

MAX_PROBE_HOLD_S = 30.0
"""How long a probe may hold a panel lit, on the wire.

This is not merely a picture on one panel. Per spec 6.3 a probe takes the same
bus guard `apply` does, so for its whole duration every worker sharing that bus
is held off the bus and every panel on it is frozen -- which is the price of not
repeating M2's interleaved writes. The number therefore bounds a rack-wide
stall, not a decoration.

Thirty seconds because the thing being waited on is a human looking at a shelf
and saying "that one", which takes a second or two; the daemon's `identify`
takes `--hold` in the same units and for the same purpose. Thirty is an order of
magnitude past that and still short enough that the server's bounded wait for
the reply can outlast it -- ten minutes, which is what an unbounded `hold_s`
invited, is a request whose answer nobody is left to hear.

The daemon bounds it again with the number it can actually afford. Both exist on
purpose: this end owns what the *message* may say, and the Pi owns what its own
panels can be asked for, which is the same division `MAX_REQUESTED_FPS` and
`ors_daemon.frames.MAX_FPS` already draw.
"""

MAX_PROBE_ERROR = 512
"""How much of a failure a `ProbeResult` may carry back.

The thing that lands here is an `OSError` from the SPI driver or a line from the
daemon's own refusal, and it is shown to whoever pressed the button. A line or
two is the whole of what is useful.

It is bounded, and the daemon must truncate to it rather than trusting a driver
to be brief, because of what an over-long one costs: the message fails to parse,
the server drops it, the wait expires, and a probe that failed for a reason the
daemon knew answers with a timeout instead. A refused reply is strictly worse
than a shortened one.
"""


class DetectRequest(_Message):
    """Ask a rack what SPI devices it has. Carries nothing else, because nothing else is asked.

    Correlated rather than fire-and-forget, unlike `Command`: the point of it is
    the answer, and an HTTP handler is waiting on one. `request_id` is what pairs
    this with the `DetectResult` that comes back, and it is the server's to mint.
    """

    type: Literal["detect"] = "detect"
    request_id: str = Field(min_length=1, max_length=MAX_REQUEST_ID)


class PanelCandidate(BaseModel):
    """One `/dev/spidev<bus>.<cs>` the daemon found, and who is already driving it.

    Not a `_Message`: it is a member of one, it carries no `type`, and nothing
    dispatches on it. It forbids extra fields all the same, and for a sharper
    reason than the envelope's -- `claimed_by` is the field a probe consults
    before it touches a device, so a daemon that sent `claimedBy` or `claimed`
    would have every device read as *free*, and the wizard would offer an
    operator a panel a live worker is mid-frame on. A misspelling has to be an
    error naming the field, not a silent "nobody is using this".

    There is no `dc` or `rst` here, and that is the whole shape of the feature:
    a GC9A01 has no readable id over 4-wire SPI and the GPIO lines were chosen
    with a screwdriver, so enumeration can say which devices exist and nothing
    more. The operator supplies the wiring and `ProbeRequest` proves it.
    """

    model_config = ConfigDict(extra="forbid")

    bus: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    cs: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    claimed_by: str | None = Field(min_length=1, max_length=MAX_SCREEN_NAME)
    """The name of the screen currently driving this device, or None when it is free.

    A name and not a flag or an id, because it is read by a person choosing a
    panel: "SPI0.1 -- CPU" answers the question the wizard actually raises, and
    a screen id would send them to look it up.

    Required, with no default, although None is a perfectly good value for it. A
    default would make a daemon that forgot the field say the most dangerous of
    the two answers -- "free" -- which is plausible enough that nothing
    downstream would question it, and the probe would then take a device out
    from under a running worker.

    Bounded below at 1 as well as above, for `MAX_REQUEST_ID`'s reason and one
    more of its own. A screen name cannot be created empty -- both
    `ors_server.api.screens` and `daemon.ScreenConfig.name` require a character
    -- so a name that could not have been created cannot honestly be a claim on
    a device. And `""` is *falsy*: the natural `if candidate.claimed_by:` reads
    a claimed device as free, the probe then takes it out from under a live
    worker, and that is the interleaving spec 6.3 exists to prevent. `None` is
    the one value that reads as free, and it reads that way correctly.
    """

    @field_validator("bus", "cs", mode="before")
    @classmethod
    def _wiring_is_a_number_not_a_flag(cls, value: object) -> object:
        # The same rule as `ProbeRequest`'s, on the reply that populates the list
        # the operator chooses a device from: `true` names device 1.x, which
        # exists on a Pi, so the row would look like a device the daemon never
        # found. See `not_a_flag`.
        return not_a_flag(value, what="a bus or chip select is a number")


class DetectResult(_Message):
    """What the rack found. The reply half of `DetectRequest`."""

    type: Literal["detect_result"] = "detect_result"
    request_id: str = Field(min_length=1, max_length=MAX_REQUEST_ID)
    panels: list[PanelCandidate] = Field(max_length=MAX_PANEL_CANDIDATES)
    """Every SPI device the daemon can see, claimed or not.

    Required rather than defaulted to empty, for `claimed_by`'s reason: a rack
    with no SPI devices at all is a real answer, an absent field is a bug, and a
    default makes the two indistinguishable -- the wizard would say "no panels
    found" with equal confidence in both cases. An empty list is how a rack says
    it found nothing.
    """


class ProbeRequest(_Message):
    """Light one candidate panel with wiring the operator supplied, and hold it there.

    The other half of detection: enumeration cannot discover `dc` and `rst`, so
    this is the guess being tested, and the proof is a human seeing the glass
    come on. Everything needed to open the device is here, because the device is
    not in any configuration yet.

    There is no `pattern` field, deliberately. The daemon paints one fixed
    thing -- a large ordinal on a coloured field, the same as `identify` draws --
    because a pattern arriving from the wire is a rendering instruction the
    daemon would have to validate before it obeyed, and there is nothing about it
    an operator needs to choose.
    """

    type: Literal["probe"] = "probe"
    request_id: str = Field(min_length=1, max_length=MAX_REQUEST_ID)
    bus: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    cs: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    dc: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    rst: int = Field(ge=0, le=MAX_WIRING_NUMBER)
    hz: int = Field(ge=1, le=MAX_SPI_HZ)
    # `allow_inf_nan=False` as well as the two bounds, exactly as `fps` has it
    # and for the reason written there: either bound refuses a NaN on its own,
    # because every comparison against one is False, but what must be rejected
    # is the infinity and the not-a-number rather than the range -- and an edit
    # that loosened the range, on the argument that the daemon bounds this
    # anyway, would otherwise quietly take the other rejection with it.
    hold_s: float = Field(ge=0.0, le=MAX_PROBE_HOLD_S, allow_inf_nan=False)
    """Seconds to leave the pattern up. Zero is allowed: a script does not look.

    See `MAX_PROBE_HOLD_S` for what the upper bound is protecting, which is every
    other panel on that bus.
    """

    @field_validator("bus", "cs", "dc", "rst", "hz", mode="before")
    @classmethod
    def _wiring_is_a_number_not_a_flag(cls, value: object) -> object:
        # Every one of these takes `true` as 1 without this, and 1 is a
        # plausible value for all five: bus 1 and chip select 1 exist on a Pi,
        # GPIO1 is a real line, and 1 Hz is inside the clock's range. So the
        # probe would open some *other* device, or reset some other pin, and
        # report honestly on whatever it lit. See `not_a_flag`.
        return not_a_flag(value, what="a bus, chip select, pin or clock is a number")

    @field_validator("hold_s", mode="before")
    @classmethod
    def _a_hold_is_a_duration_not_a_flag(cls, value: object) -> object:
        # A float is not exempt from the rule the five ints obey: `true` is 1 in
        # pydantic's lax mode here too, and 1.0 is squarely inside this field's
        # range. `FramesRequest.fps` leaves the same coercion open and is not a
        # regression to match, because what it buys there is a stream at the
        # wrong rate, which the next request corrects. This holds every worker
        # on the bus off the bus (spec 6.3) for a second nobody asked for, and
        # then reports as though the requested duration was honoured -- there is
        # no later message that corrects a stall that already happened.
        #
        # Its own validator rather than another field on the one above, because
        # the reason is what an operator reads: a `hold_s` refused with "a bus,
        # chip select, pin or clock is a number" would be a correct refusal
        # carrying a false reason, which is the defect `what` was added to stop.
        return not_a_flag(value, what="a hold is a duration in seconds")


class ProbeResult(_Message):
    """Whether the daemon could drive that wiring, and what stopped it if not.

    `ok` is genuinely a flag here -- the one field on these messages that is --
    and it means "the device opened and the pattern was written", never "the
    operator saw it". Only the person in front of the rack can answer that, which
    is why the wizard asks them afterwards rather than trusting this.

    The pair is deliberately unconstrained: nothing here forces an `error` when
    `ok` is false, and nothing forbids one when it is true. A `model_validator`
    tying them would turn a daemon that forgot the reason into a *refused
    message*, and a refused reply is answered by the server's wait expiring --
    so the operator would read "timed out" for a probe that failed for a reason
    the daemon knew. Filling `error` on failure and leaving it out on success is
    a producer obligation on the daemon (task 11), not a wire rule; a reader
    should treat `ok` as the verdict and `error` as commentary.
    """

    type: Literal["probe_result"] = "probe_result"
    request_id: str = Field(min_length=1, max_length=MAX_REQUEST_ID)
    ok: bool
    error: str | None = Field(default=None, min_length=1, max_length=MAX_PROBE_ERROR)
    """Why it failed, in the driver's words, or None. Defaulted because success has none.

    Bounded below at 1, exactly as `claimed_by` is, and for the same reason
    inverted: `""` is falsy, so a refusal carrying a zero-length explanation is
    indistinguishable from one carrying no explanation to every truthiness check
    that will read it -- and "a refusal with no reason in it" is a bug this repo
    has already shipped and fixed once. A daemon with nothing to say sends
    nothing, and `None` says exactly that.
    """


DaemonMessage = Annotated[
    Hello | Heartbeat | Ack | Nack | SourceStatus | Frame | LogLine | DetectResult | ProbeResult,
    Field(discriminator="type"),
]
ServerMessage = Annotated[
    ConfigPush | Command | FramesRequest | Paired | DetectRequest | ProbeRequest,
    Field(discriminator="type"),
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
