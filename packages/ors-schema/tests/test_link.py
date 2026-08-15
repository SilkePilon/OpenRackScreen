import base64
import json

import pytest
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    MAX_FRAME_BYTES,
    MAX_PANEL_CANDIDATES,
    MAX_PROBE_ERROR,
    MAX_PROBE_HOLD_S,
    MAX_REQUEST_ID,
    MAX_REQUESTED_FPS,
    MAX_SCREEN_NAME,
    MAX_SPI_HZ,
    MAX_WATCHED_SCREENS,
    MAX_WIRING_NUMBER,
    PROTOCOL_VERSION,
    Ack,
    Command,
    ConfigPush,
    DetectRequest,
    DetectResult,
    Frame,
    FramesRequest,
    Heartbeat,
    Hello,
    LogLine,
    Nack,
    Paired,
    PanelCandidate,
    ProbeRequest,
    ProbeResult,
    SourceStatus,
    parse_daemon_message,
    parse_server_message,
)
from pydantic import ValidationError

CONFIG = {
    "version": 1,
    "timezone": "UTC",
    "integrations": [
        {
            "name": "prom",
            "type": "prometheus",
            "url": "http://p:9090",
            "fields": {"cpu": {"query": "up"}},
        }
    ],
    "screens": [
        {
            "name": "CPU",
            "position": 1,
            "display": {"backend": "virtual", "out_dir": "/tmp/p"},
            "template": "ring-gauge",
            "params": {},
        }
    ],
}


def test_hello_carries_what_the_server_needs_to_identify_a_daemon():
    hello = Hello(
        token="abc", hostname="pi-rack", daemon_version="0.1.0", capabilities={"spi": [0, 1]}
    )

    assert hello.type == "hello"
    assert hello.protocol_version == PROTOCOL_VERSION


def test_a_daemon_says_which_config_version_it_is_already_running():
    # Without this the reconnect rule in the spec cannot be implemented at all:
    # the server has no way to learn what the daemon has, so every reconnect is
    # a push, and a push tears down and repaints the whole rack. A wifi blip
    # would flicker every panel.
    hello = Hello(token="abc", hostname="pi-rack", daemon_version="0.1.0", config_version=7)

    assert hello.config_version == 7
    assert parse_daemon_message(hello.model_dump_json()).config_version == 7


def test_a_daemon_with_no_config_says_so_rather_than_claiming_a_version():
    # A fresh Pi, and a rebooted one whose cache is gone. Zero would be a
    # version -- and the version an empty database counts from.
    fresh = Hello(token="abc", hostname="pi-rack", daemon_version="0.1.0")

    assert fresh.config_version is None
    assert parse_daemon_message(fresh.model_dump_json()).config_version is None


def test_a_hello_is_bounded_because_it_arrives_before_anything_is_authenticated():
    """Every field here is attacker-controlled and none of it has been checked.

    A socket reaches `hello` by connecting, and `hello` is the whole of what the
    server reads before it knows who -- if anyone -- is on the other end. Two of
    these fields are then stored, and `hostname` is written to a log line the
    server emits for a credential that matched nothing, so an unbounded one is
    an unbounded write per refused connect.

    Every limit is far past anything a real daemon sends: 253 is the longest a
    DNS name can be, and the credentials this server mints are 43 and 64
    characters.
    """
    plenty = {"token": "t", "hostname": "pi-rack", "daemon_version": "0.1.0"}

    for field, size in (("token", 257), ("hostname", 254), ("daemon_version", 65)):
        with pytest.raises(ValidationError) as refused:
            Hello(**{**plenty, field: "x" * size})
        assert refused.value.errors()[0]["type"] == "string_too_long"
        assert Hello(**{**plenty, field: "x" * (size - 1)}), "and the limit itself is allowed"


def test_a_capabilities_dict_is_bounded_by_what_it_weighs_not_by_how_many_keys():
    """One key is enough to carry four megabytes, so counting keys bounds nothing.

    `capabilities` is `dict[str, Any]`, which is what lets a daemon describe
    hardware this build has never heard of -- and what stops any per-field limit
    from applying to the values. The bound has to be on the serialised whole,
    because that is the thing that gets stored: this column is written verbatim
    into `daemon.capabilities` by a server that has just finished deciding the
    credential was good, and from there into every export of the database.
    """
    plenty = {"token": "t", "hostname": "pi-rack", "daemon_version": "0.1.0"}

    with pytest.raises(ValidationError) as refused:
        Hello(**plenty, capabilities={"spi": "x" * 5000})

    assert "capabilities" in str(refused.value)
    assert Hello(**plenty, capabilities={"spi": [0, 1], "displays": ["gc9a01"] * 20})


def test_a_config_push_carries_a_whole_validated_snapshot():
    push = ConfigPush(version=7, snapshot=DaemonConfig.model_validate(CONFIG))

    assert push.snapshot.screens[0].name == "CPU"
    assert push.version == 7


def test_a_push_whose_snapshot_is_invalid_is_rejected_at_the_edge():
    with pytest.raises(ValidationError):
        ConfigPush(version=1, snapshot={"screens": [{"rotation": 45}]})


def test_daemon_messages_are_discriminated_by_type():
    parsed = parse_daemon_message(Ack(config_version=7).model_dump_json())

    assert isinstance(parsed, Ack)
    assert parsed.config_version == 7


def test_server_messages_are_discriminated_by_type():
    parsed = parse_server_message(Command(command="identify").model_dump_json())

    assert isinstance(parsed, Command)
    assert parsed.command == "identify"


def test_an_unknown_message_type_is_rejected_rather_than_ignored():
    with pytest.raises(ValidationError):
        parse_daemon_message('{"type": "shutdown_everything"}')


def test_a_nack_carries_the_reason_a_snapshot_was_refused():
    nack = Nack(config_version=7, reason="screens.0.rotation: Input should be 0, 90, 180 or 270")

    assert "rotation" in nack.reason


def test_a_frames_request_names_the_screens_and_the_rate():
    request = FramesRequest(enabled=True, screen_ids=[1, 2], fps=2.0)

    assert request.screen_ids == [1, 2]
    assert FramesRequest(enabled=False).screen_ids == []


@pytest.mark.parametrize("fps", ["NaN", "Infinity", "-Infinity"])
def test_a_rate_that_is_not_a_number_does_not_parse_off_the_wire(fps: str):
    """The one value that gets past a rate limit written with `min` and `<`.

    Pydantic's `allow_inf_nan` defaults to true and a bare `float` takes it, so
    `{"fps": NaN}` used to parse. Every comparison a limiter makes against NaN is
    then False: `min(nan, cap)` is `nan`, `nan <= 0` is False so it is not read
    as "no frames", and `now - last < nan` is False for ever -- so the daemon
    accepts every frame the render loop draws, for four panels, for the life of
    the process. Measured before this bound: 100 offers accepted out of 100 with
    the clock frozen, where the cap allows one.
    """
    with pytest.raises(ValidationError):
        parse_server_message(
            f'{{"type": "frames", "enabled": true, "screen_ids": [1], "fps": {fps}}}'
        )


@pytest.mark.parametrize("fps", [-1.0, MAX_REQUESTED_FPS + 1.0, 1e9])
def test_a_rate_outside_what_a_screen_could_ever_show_does_not_parse(fps: float):
    """The far end of this socket is a browser, and the bound is a security one.

    Not the daemon's ceiling -- `ors_daemon.frames.MAX_FPS` is far lower and is
    a policy about one Pi's CPU -- but the bound on the *message*, which is what
    the two ends have to agree about. Negative is refused rather than read as
    "no frames" because a rate below nought is not a rate; the daemon clamps one
    anyway, and that redundancy is the point.
    """
    with pytest.raises(ValidationError):
        FramesRequest(enabled=True, screen_ids=[1], fps=fps)


def test_the_fastest_rate_a_server_may_ask_for_still_parses():
    assert FramesRequest(enabled=True, fps=MAX_REQUESTED_FPS).fps == MAX_REQUESTED_FPS
    assert FramesRequest(enabled=True, fps=0.0).fps == 0.0


def test_a_request_may_not_name_more_screens_than_a_rack_could_have():
    """The third hole of the `fps: NaN` family, and the one left for task 12.

    `screen_ids` sizes the daemon's `_enabled` set and everything keyed off it,
    and the daemon replaces that set wholesale on every request. Unbounded, one
    message decides how much of a Pi's memory a subscription costs.
    """
    with pytest.raises(ValidationError):
        FramesRequest(enabled=True, screen_ids=list(range(MAX_WATCHED_SCREENS + 1)))


def test_a_request_naming_exactly_as_many_screens_as_are_allowed_still_parses():
    request = FramesRequest(enabled=True, screen_ids=list(range(MAX_WATCHED_SCREENS)))

    assert len(request.screen_ids) == MAX_WATCHED_SCREENS


def test_an_over_long_screen_list_does_not_parse_off_the_wire_either():
    """The bound has to hold where the list is chosen by the far end, not only
    where a server assembles one in Python."""
    over = json.dumps(
        {
            "type": "frames",
            "enabled": True,
            "screen_ids": list(range(MAX_WATCHED_SCREENS + 1)),
            "fps": 2.0,
        }
    )

    with pytest.raises(ValidationError):
        parse_server_message(over)


def test_the_bound_on_watched_screens_is_an_order_of_magnitude_past_a_rack():
    """The one absolute assertion about this number, for the reason the frame
    bound has one: every other test here uses the constant relatively, so
    nothing else would notice it drifting.

    Sixty-four is far past the four panels a Pi drives over SPI and well under
    `ors_daemon.frames._MAX_TRACKED_SCREENS`, which is the table this list
    resizes. The two are pinned against each other in the daemon's own suite,
    which is the end that owns the smaller number.
    """
    assert MAX_WATCHED_SCREENS == 64


def test_a_frame_carries_bytes_and_a_sequence_number():
    frame = Frame(screen_id=1, seq=42, webp=b"RIFF....WEBP")

    # Round-tripped through JSON as base64, because the envelope is JSON and
    # bytes are not: a frame that silently became a str would decode to garbage.
    restored = Frame.model_validate_json(frame.model_dump_json())
    assert restored.webp == b"RIFF....WEBP"
    assert restored.seq == 42


# A real WebP header, byte for byte: the RIFF magic, a little-endian length, the
# `WEBP` form type and the start of a lossy bitstream. Nothing after index 15 is
# valid UTF-8, which is the whole point -- an ASCII stand-in like `b"RIFF..WEBP"`
# round-trips under pydantic's *default* bytes handling and proves nothing.
REAL_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00\xf0\xfe\x9d\x01\x2a\x01\x00\x01\x00"


def test_a_frame_of_real_webp_bytes_survives_the_json_envelope():
    frame = Frame(screen_id=1, seq=7, webp=REAL_WEBP)

    restored = Frame.model_validate_json(frame.model_dump_json())

    assert restored.webp == REAL_WEBP


def test_a_frame_travels_as_base64_rather_than_as_a_decoded_string():
    """Pin the encoding, because the consumer three tasks away is a browser.

    Pydantic's default for `bytes` in JSON is `utf8`, not base64: it decodes the
    payload as text, which raises on a real WebP and would have silently mangled
    one that happened to decode. `ser_json_bytes="base64"` is what makes the wire
    form a str that carries an encoding rather than a str that lost bytes -- and
    the alphabet is URL-safe (`-_`), which is what the far end has to decode with.
    """
    wire = json.loads(Frame(screen_id=1, seq=7, webp=REAL_WEBP).model_dump_json())

    assert wire["webp"] == base64.urlsafe_b64encode(REAL_WEBP).decode()
    # ... and the reader is tolerant of the standard alphabet, so an end that
    # encodes with `+/` is understood rather than rejected as corrupt.
    standard = json.dumps({**wire, "webp": base64.b64encode(REAL_WEBP).decode()})
    assert Frame.model_validate_json(standard).webp == REAL_WEBP


def test_a_frame_stays_bytes_when_it_is_not_going_through_json():
    """`model_dump()` is not the wire; a caller holding a `Frame` holds bytes."""
    assert Frame(screen_id=1, seq=7, webp=REAL_WEBP).model_dump()["webp"] == REAL_WEBP


DAEMON_MESSAGES = [
    Hello(token="t", hostname="pi", daemon_version="0.1.0"),
    Heartbeat(uptime_s=12),
    Ack(config_version=7),
    Nack(config_version=7, reason="no"),
    SourceStatus(integration="prom", state="ok"),
    Frame(screen_id=1, seq=1, webp=REAL_WEBP),
    LogLine(level="info", message="hello"),
    DetectResult(request_id="r-a", panels=[PanelCandidate(bus=3, cs=1, claimed_by="CPU")]),
    ProbeResult(request_id="r-b", ok=True),
]


@pytest.mark.parametrize("message", DAEMON_MESSAGES, ids=lambda m: m.type)
def test_every_daemon_message_parses_back_to_its_own_class(message):
    assert parse_daemon_message(message.model_dump_json()) == message


@pytest.mark.parametrize(
    "message",
    [
        ConfigPush(version=7, snapshot=DaemonConfig.model_validate(CONFIG)),
        Command(command="sleep", screen_id=2),
        FramesRequest(enabled=True, screen_ids=[1], fps=1.0),
        Paired(daemon_id=3, key="a-key"),
        DetectRequest(request_id="r-c"),
        ProbeRequest(request_id="r-d", bus=2, cs=3, dc=25, rst=27, hz=16_000_000, hold_s=2.5),
    ],
    ids=lambda m: m.type,
)
def test_every_server_message_parses_back_to_its_own_class(message):
    assert parse_server_message(message.model_dump_json()) == message


def test_the_union_tries_only_the_member_its_tag_names():
    """The discrimination itself, not merely "some member happened to fit".

    An `ack` missing its `config_version` must fail *as an ack* -- one error,
    located under the `ack` member -- rather than as seven failed attempts, and
    rather than succeeding as some other member whose fields all have defaults.
    """
    with pytest.raises(ValidationError) as caught:
        parse_daemon_message('{"type": "ack"}')

    assert [(error["type"], error["loc"]) for error in caught.value.errors()] == [
        ("missing", ("ack", "config_version"))
    ]


def test_a_message_naming_no_type_is_rejected():
    with pytest.raises(ValidationError) as caught:
        parse_daemon_message('{"config_version": 7}')

    assert caught.value.errors()[0]["type"] == "union_tag_not_found"


def test_an_unknown_type_fails_as_a_bad_tag_and_names_what_is_known():
    """The version-skew failure: loud, at the edge, and legible in a log."""
    with pytest.raises(ValidationError) as caught:
        parse_daemon_message('{"type": "shutdown_everything"}')

    error = caught.value.errors()[0]
    assert error["type"] == "union_tag_invalid"
    assert "'hello'" in error["msg"] and "'frame'" in error["msg"]


def test_a_body_that_fits_one_member_is_not_smuggled_in_under_another_tag():
    """`extra="forbid"` is what stops an ack from arriving as a silent heartbeat.

    Every `Heartbeat` field has a default, so without it this payload would
    validate -- the server would record a heartbeat and the ack it was waiting
    for would be gone with no error anywhere.
    """
    with pytest.raises(ValidationError):
        parse_daemon_message('{"type": "heartbeat", "config_version": 7}')


def test_neither_direction_accepts_the_other_direction_s_messages():
    with pytest.raises(ValidationError):
        parse_server_message(Ack(config_version=7).model_dump_json())
    with pytest.raises(ValidationError):
        parse_daemon_message(Command(command="identify").model_dump_json())


def test_a_pushed_snapshot_is_a_daemon_config_all_the_way_across_the_wire():
    """The property this module exists for: one validated object, both ends."""
    config = DaemonConfig.model_validate(CONFIG)

    parsed = parse_server_message(ConfigPush(version=7, snapshot=config).model_dump_json())

    assert isinstance(parsed, ConfigPush)
    assert parsed.snapshot == config
    assert isinstance(parsed.snapshot, DaemonConfig)


def test_the_generation_counter_and_the_schema_version_do_not_collide():
    """Three versions travel here; two of them are in this one message.

    `ConfigPush.version` counts pushes and is what an `Ack` answers.
    `DaemonConfig.version` is the schema's, and is a constant.
    """
    wire = json.loads(
        ConfigPush(version=7, snapshot=DaemonConfig.model_validate(CONFIG)).model_dump_json()
    )

    assert wire["version"] == 7
    assert wire["snapshot"]["version"] == 1


def test_the_link_models_are_reachable_from_the_package_root():
    """Both ends import `ors_schema`, not `ors_schema.link`, and `__all__` is
    hand-maintained -- so a name added to the module and forgotten here is a
    working model that neither end can reach."""
    import ors_schema

    exported = {
        "PROTOCOL_VERSION",
        "Ack",
        "Command",
        "ConfigPush",
        "DaemonMessage",
        "DetectRequest",
        "DetectResult",
        "Frame",
        "FramesRequest",
        "Heartbeat",
        "Hello",
        "LogLine",
        "Nack",
        "Paired",
        "PanelCandidate",
        "ProbeRequest",
        "ProbeResult",
        "ServerMessage",
        "SourceStatus",
        "parse_daemon_message",
        "parse_server_message",
    }

    assert exported <= set(ors_schema.__all__)
    assert all(hasattr(ors_schema, name) for name in exported)


def test_pairing_hands_back_a_key_the_daemon_reconnects_with():
    """The token is single-use, so something else has to identify the daemon next time.

    Its own message rather than a field on `ConfigPush`, because a pushed
    config is a document the daemon caches to disk to boot from when the server
    is down -- a credential riding along inside it would be written into that
    cache every push.
    """
    paired = parse_server_message(Paired(daemon_id=3, key="a-key").model_dump_json())

    assert isinstance(paired, Paired)
    assert paired.daemon_id == 3
    assert paired.key == "a-key"


def test_a_daemon_cannot_hand_itself_a_key():
    """`Paired` travels one way. In the other direction it is not a message at all."""
    with pytest.raises(ValidationError):
        parse_daemon_message(Paired(daemon_id=3, key="a-key").model_dump_json())


def test_the_pairing_token_stays_out_of_a_repr():
    """A repr is what reaches a log or an exception without anyone deciding to."""
    hello = Hello(token="s3cret-pairing-token", hostname="pi-rack", daemon_version="0.1.0")

    assert "s3cret-pairing-token" not in repr(hello)
    assert "pi-rack" in repr(hello), "the rest of the message is still useful in a log"
    assert hello.model_dump()["token"] == "s3cret-pairing-token", "it still travels"
    assert "s3cret-pairing-token" in hello.model_dump_json()


def test_the_daemon_key_stays_out_of_a_repr_too():
    """The same rule as `Hello.token`, for the credential that outlives it.

    This one is worse if it leaks: a pairing token is spent once, and this is
    what the daemon presents on every connect for the life of the pairing.
    """
    paired = Paired(daemon_id=3, key="s3cret-daemon-key")

    assert "s3cret-daemon-key" not in repr(paired)
    assert "3" in repr(paired), "the rest of the message is still useful in a log"
    assert paired.model_dump()["key"] == "s3cret-daemon-key", "it still travels"
    assert "s3cret-daemon-key" in paired.model_dump_json()


def test_a_frame_is_bounded_by_a_number_both_ends_can_read():
    """An oversized frame must be a dropped frame, never a dropped connection.

    A message past the reader's limit makes `websockets` close the socket, so a
    daemon that encoded one too large would reconnect, be pushed to, and send it
    again -- a loop neither end can explain. Bounding the field turns that into
    one unreadable message the server logs and skips, and gives the daemon a
    number to check against before it writes anything.
    """
    assert Frame(screen_id=1, seq=1, webp=b"x" * MAX_FRAME_BYTES).seq == 1

    with pytest.raises(ValidationError):
        Frame(screen_id=1, seq=1, webp=b"x" * (MAX_FRAME_BYTES + 1))


def test_an_oversized_frame_off_the_wire_is_refused_after_it_is_decoded():
    """Base64 is 4/3 of what it carries, so the bound has to be on the bytes."""
    oversized = json.dumps(
        {
            "type": "frame",
            "screen_id": 1,
            "seq": 1,
            "webp": base64.urlsafe_b64encode(b"x" * (MAX_FRAME_BYTES + 1)).decode(),
        }
    )

    with pytest.raises(ValidationError):
        parse_daemon_message(oversized)


def test_a_frame_at_the_bound_still_parses_although_its_wire_form_is_larger():
    """Which of the two lengths `max_length` counts, said with a case that can tell.

    The refusal above cannot: `MAX_FRAME_BYTES + 1` bytes is over the bound as
    decoded payload *and* over it as base64, so it fails either way and pins
    neither reading. This one is 262,144 decoded and 349,528 on the wire, so it
    parses only if the bound counts what the base64 carries -- which is what
    `Frame.webp`'s comment claims and what makes the number comparable to
    anything else measured in frame bytes.

    It matters beyond tidiness, because it says where the reader's own limit has
    to sit: the payload is 4/3 of this before it is decoded, so a `ws_max_size`
    set to `MAX_FRAME_BYTES` would close the socket over a frame this schema
    accepts. See `ors_server.__main__`, which sizes it from here.
    """
    at_bound = json.dumps(
        {
            "type": "frame",
            "screen_id": 1,
            "seq": 1,
            "webp": base64.urlsafe_b64encode(b"x" * MAX_FRAME_BYTES).decode(),
        }
    )
    assert len(at_bound) > MAX_FRAME_BYTES

    parsed = parse_daemon_message(at_bound)

    assert isinstance(parsed, Frame)
    assert len(parsed.webp) == MAX_FRAME_BYTES


def test_the_bound_on_a_frame_is_a_quarter_of_a_mebibyte_and_not_uvicorn_s_default():
    """The one absolute assertion about this number, and it is not decoration.

    Every other test here uses the constant relatively, so raising it changes
    nothing anybody notices -- and there is exactly one value it must not drift
    towards. uvicorn's `ws_max_size` defaults to 16,777,216, which is the reader
    limit on the server end of this link, so a `MAX_FRAME_BYTES` at 16 MiB puts
    the schema's bound at or above the transport's and re-opens the failure this
    bound exists to close: a frame the daemon believes is legal, the socket
    closed under it by `websockets`, a reconnect, a push and the same frame
    again. A daemon holding a valid key would also be making the server allocate
    that much per message before any bound could refuse it.

    A quarter of a mebibyte is eight times the worst 240x240 panel anything can
    encode (32,992 bytes of uniform random noise, which no template draws), so
    the margin is in the direction that costs nothing.
    """
    assert MAX_FRAME_BYTES == 256 * 1024 == 262_144
    assert MAX_FRAME_BYTES * 64 == 16 * 1024 * 1024, "uvicorn's default is 64 of these"


def test_a_frame_addressed_to_a_flag_is_not_a_frame_addressed_to_screen_one():
    """`bool` is an `int` in Python and pydantic's lax mode takes it as one.

    `{"screen_id": true}` therefore validated as 1 -- a frame from a daemon that
    sent a flag where an id belongs would be relayed to whoever is watching row
    id 1, painting one rack's panel with another's picture and telling nobody.
    `ws_ui._Request` refused the same thing on the browser socket and
    `ws_daemon._about` in a log field; this is the third place and the one both
    of those should have been able to lean on.
    """
    with pytest.raises(ValidationError) as refused:
        Frame(screen_id=True, seq=1, webp=b"x")

    assert "flag" in str(refused.value)
    # Off the wire as well as in Python: the JSON literal is how it really arrives.
    with pytest.raises(ValidationError):
        Frame.model_validate_json('{"type": "frame", "screen_id": true, "seq": 1, "webp": "eA=="}')


def test_a_command_addressed_to_a_flag_is_refused_the_same_way():
    """Lower impact -- the server mints its own commands -- and the same defect.

    A command carrying `true` would identify, sleep or wake whichever panel holds
    row id 1, which on a four-panel rack is a one-in-four chance of looking
    correct.
    """
    with pytest.raises(ValidationError) as refused:
        Command(command="identify", screen_id=True)

    assert "flag" in str(refused.value)


def test_a_command_may_still_name_no_screen_at_all():
    """The whole rack, which is what `sleep` and `wake` usually mean.

    None is not a flag and must survive the validator that refuses one -- a
    check written as `if not screen_id` would take this with it.
    """
    assert Command(command="sleep").screen_id is None


def test_a_screen_id_of_one_still_parses_on_both_messages():
    """The value the bug aliased to. Refusing it would be the same bug inverted."""
    assert Frame(screen_id=1, seq=1, webp=b"x").screen_id == 1
    assert Command(command="identify", screen_id=1).screen_id == 1


def test_a_detect_result_names_what_is_already_claimed():
    result = DetectResult(
        request_id="r1",
        panels=[
            PanelCandidate(bus=0, cs=0, claimed_by="CPU"),
            PanelCandidate(bus=1, cs=1, claimed_by=None),
        ],
    )
    assert result.panels[1].claimed_by is None


def test_a_probe_names_the_wiring_it_is_proving():
    probe = ProbeRequest(request_id="r2", bus=1, cs=0, dc=4, rst=27, hz=16_000_000, hold_s=3.0)
    assert probe.hold_s == 3.0


def test_a_probe_may_not_hold_a_bus_for_ever():
    with pytest.raises(ValidationError):
        ProbeRequest(request_id="r3", bus=0, cs=0, dc=25, rst=27, hz=16_000_000, hold_s=600.0)


def test_a_request_id_is_bounded_like_every_other_string_off_the_wire():
    with pytest.raises(ValidationError):
        DetectRequest(request_id="x" * 1000)


def test_a_flag_is_not_a_bus():
    # The defect this project has now fixed three times: `true` coerces to 1.
    with pytest.raises(ValidationError):
        ProbeRequest(request_id="r4", bus=True, cs=0, dc=25, rst=27, hz=16_000_000, hold_s=1.0)


# Every number distinct from every other and from every list index, because the
# whole content of these messages is which number went where: a probe built with
# `bus=0, cs=0, dc=0` would light the right panel however badly the fields were
# crossed. `cs=3` is not a chip select any stock Pi has, which is the point --
# nothing here may be quietly reinterpreted as a plausible default.
WIRING = {"bus": 2, "cs": 3, "dc": 25, "rst": 27, "hz": 16_000_000}


def probe(**overrides: object) -> ProbeRequest:
    return ProbeRequest(request_id="rq", **{**WIRING, "hold_s": 2.5, **overrides})


def test_a_probe_carries_each_number_to_its_own_field():
    """The fields are only useful if they stay apart, and nothing else checks that.

    Five integers in a row, four of which the daemon hands straight to a device
    it then writes to. A transposed `dc` and `rst` is a panel that resets on
    every command byte; a transposed `bus` and `cs` opens a different device
    entirely. So the fixture uses five different numbers and this asserts all
    five, rather than trusting a constructor to be a constructor.
    """
    parsed = parse_server_message(probe().model_dump_json())

    assert isinstance(parsed, ProbeRequest)
    assert (parsed.bus, parsed.cs, parsed.dc, parsed.rst) == (2, 3, 25, 27)
    assert parsed.hz == 16_000_000
    assert parsed.hold_s == 2.5


@pytest.mark.parametrize("field", ["bus", "cs", "dc", "rst", "hz"])
def test_no_number_on_a_probe_may_arrive_as_a_flag(field: str):
    """`true` is 1, and 1 is a *plausible* value for every one of these.

    That is what makes this worse here than on a screen id, where the aliased row
    at least belongs to some screen. SPI bus 1 and chip select 1 exist on a Pi,
    GPIO1 is a real line, and 1 Hz is inside the clock's range -- so a probe
    carrying a flag opens a real device, drives a real pin and reports success on
    whatever it happened to light. The operator then writes that wiring down.
    """
    with pytest.raises(ValidationError) as refused:
        probe(**{field: True})

    assert "flag" in str(refused.value)
    assert "screen id" not in str(refused.value), "the reason has to name the field's kind"
    # Off the wire as well as in Python, which is how it really arrives.
    wire = json.dumps({"type": "probe", "request_id": "rq", "hold_s": 1.0, **WIRING, field: True})
    with pytest.raises(ValidationError):
        parse_server_message(wire)


@pytest.mark.parametrize("field", ["bus", "cs"])
def test_no_number_on_a_candidate_may_arrive_as_a_flag_either(field: str):
    """The reply half carries the same kind of number and takes the same lie.

    A candidate whose `bus` arrived as `true` names device 1.x in a list the
    wizard is about to offer, and the operator picks a row that describes a
    device the daemon never found.
    """
    with pytest.raises(ValidationError):
        PanelCandidate(**{"bus": 2, "cs": 3, "claimed_by": None, field: True})


def test_a_screen_id_still_refuses_a_flag_in_the_words_it_always_used():
    """The shared validator grew an argument; the message it had must not move.

    `not_a_flag` is called from three places outside this module, one of them a
    request body in the server's API, and its sentence is what an operator reads
    out of the 422. Adding a reason for pin fields must not reword the one that
    was already there.
    """
    with pytest.raises(ValidationError) as refused:
        Frame(screen_id=True, seq=1, webp=b"x")

    assert "a screen id is a row id, not a flag" in str(refused.value)


@pytest.mark.parametrize("hold", ["NaN", "Infinity", "-Infinity"])
def test_a_hold_that_is_not_a_number_does_not_parse_off_the_wire(hold: str):
    """The same family as `fps: NaN`, on a field that stops a rack rather than a stream.

    A `hold_s` of infinity is a probe that never ends, and per spec 6.3 a probe
    holds every worker on that bus off the bus for its whole duration -- so it is
    a rack frozen with one panel showing an ordinal, and nothing to time it out.
    NaN is worse for being quiet: every `<` a timer writes against it is False.

    The assertion is on the error's *type*, and that is the whole of what
    distinguishes this from the range check sitting next to it. `ge` and `le`
    refuse all three on their own -- every comparison against NaN is False -- so
    a bare `pytest.raises` passes whether `allow_inf_nan=False` is there or not,
    and dropping it changes nothing any test could see. Measured: it survives.
    `finite_number` is the error only the explicit setting produces, and it is
    what makes the difference visible now rather than on the day someone drops
    `le` because the spec says the daemon bounds this anyway.
    """
    wire = json.dumps({"type": "probe", "request_id": "rq", **WIRING})
    with pytest.raises(ValidationError) as refused:
        parse_server_message(f'{wire[:-1]}, "hold_s": {hold}}}')

    assert [(e["type"], e["loc"]) for e in refused.value.errors()] == [
        ("finite_number", ("probe", "hold_s"))
    ]


def test_a_hold_below_nothing_is_not_a_duration():
    with pytest.raises(ValidationError):
        probe(hold_s=-0.5)


def test_a_probe_may_hold_for_no_time_at_all():
    """`--hold 0` is what the daemon's own `identify` gives a script, and this is
    the same request from a test harness: paint it, prove the device opened, move
    on. Refusing zero would make the schema stricter than the tool it mirrors."""
    assert probe(hold_s=0.0).hold_s == 0.0


def test_the_longest_hold_a_server_may_ask_for_still_parses_and_one_past_it_does_not():
    assert probe(hold_s=MAX_PROBE_HOLD_S).hold_s == MAX_PROBE_HOLD_S

    with pytest.raises(ValidationError):
        probe(hold_s=MAX_PROBE_HOLD_S + 0.5)


def test_the_bound_on_a_hold_is_seconds_and_not_minutes():
    """The one absolute assertion about this number, for the reason the frame
    bound has one: every other test spends it relatively, so it could drift to
    ten minutes without a single failure.

    It bounds a rack-wide stall, not a picture. Thirty seconds is an order of
    magnitude past a person looking at a shelf and saying "that one", and short
    enough that the server's bounded wait for the reply outlasts the probe -- a
    hold longer than the wait is a question whose answer nobody is left to hear.
    """
    assert MAX_PROBE_HOLD_S == 30.0


def test_the_other_numbers_these_messages_are_bounded_by_are_the_ones_they_claim():
    """The rest of the absolute assertions, for the reason the hold bound has one.

    Every other test here spends these constants relatively -- `MAX + 1` must
    fail, `MAX` must parse -- which stays true at any value, so each of these
    could be loosened by an order of magnitude with the suite still green.
    Measured: raising all four in turn failed nothing before this test existed.

    Each is a claim about something outside this file, which is what makes them
    checkable at all rather than taste.
    """
    # 32 bits because `spidev`'s `max_speed_hz` ioctl field is 32 bits wide. Past
    # it is not a fast clock, it is not a clock.
    assert MAX_SPI_HZ == 2**32 - 1

    # The same number the server bounds a screen name with when one is created
    # (`ors_server.api.screens.MAX_NAME`), which this package may not import. A
    # `claimed_by` longer than a name that can exist is not a claim.
    assert MAX_SCREEN_NAME == 64

    # An order of magnitude past a rack, like `MAX_WATCHED_SCREENS`: four panels
    # over SPI, a handful of `/dev/spidev*` nodes even with overlays.
    assert MAX_PANEL_CANDIDATES == 64

    # Half a kilobyte: an `OSError` from a driver, shown to whoever pressed the
    # button. The daemon truncates to it, because an over-long error is not a
    # truncated message but a refused one -- and then a timeout with no reason.
    assert MAX_PROBE_ERROR == 512


@pytest.mark.parametrize("field", ["bus", "cs", "dc", "rst"])
def test_a_wiring_number_is_bounded_as_an_integer_not_as_a_gpio(field: str):
    """A bound on magnitude, and deliberately not a range of pins.

    `DisplayConfig` refuses to validate pin numbers at all and says why: which
    lines exist is a fact about one board, unknown to a schema parsed on another
    machine. So the largest number a Pi could use still parses here, and the
    daemon's driver is what gives the real error for a pin that is not wired.

    What is refused is the number that is not a pin at all. An int off a JSON
    socket is arbitrary precision, and this one travels into a `DisplayConfig`
    and from there into the database.
    """
    assert getattr(probe(**{field: MAX_WIRING_NUMBER}), field) == MAX_WIRING_NUMBER

    for absurd in (MAX_WIRING_NUMBER + 1, 2**64, -1):
        with pytest.raises(ValidationError):
            probe(**{field: absurd})


def test_a_candidate_is_bounded_the_same_way():
    assert PanelCandidate(bus=MAX_WIRING_NUMBER, cs=0, claimed_by=None).bus == MAX_WIRING_NUMBER

    with pytest.raises(ValidationError):
        PanelCandidate(bus=2, cs=2**64, claimed_by=None)


def test_a_clock_is_bounded_by_what_the_kernel_can_be_asked_for():
    """32 bits, because `spidev`'s `max_speed_hz` ioctl field is 32 bits wide.

    Not a claim about what a GC9A01 tolerates -- that is exactly what running the
    probe finds out, and a schema guessing at it would refuse a rack somebody
    built. Zero is refused separately: `spidev` reads it as "keep whatever was
    set", so a probe at zero passes at a speed nobody chose and the wizard writes
    that speed into a configuration.
    """
    assert probe(hz=MAX_SPI_HZ).hz == MAX_SPI_HZ

    for impossible in (0, -1, MAX_SPI_HZ + 1):
        with pytest.raises(ValidationError):
            probe(hz=impossible)


def test_a_probe_takes_no_pattern_from_the_wire():
    """The field the spec removed on purpose, pinned so it cannot come back quietly.

    The daemon paints one fixed thing it chose. A pattern arriving from the
    server is a rendering instruction the daemon would have to validate before
    obeying, on a device it is opening for the first time, and there is nothing
    about it an operator needs to choose. `extra="forbid"` is what turns an end
    that invents one into a loud error naming the field.
    """
    with pytest.raises(ValidationError) as refused:
        parse_server_message(
            json.dumps(
                {
                    "type": "probe",
                    "request_id": "rq",
                    **WIRING,
                    "hold_s": 1.0,
                    "pattern": "rings",
                }
            )
        )

    assert "pattern" in str(refused.value)


def test_a_candidate_that_misspells_its_claim_is_an_error_and_not_a_free_device():
    """`extra="forbid"` on the nested model, where it is doing the most work.

    `claimed_by` is what a probe consults before it touches a device. Without
    this, a daemon sending `claimedBy` would have every candidate read as free --
    the wizard would offer a panel a live worker is mid-frame on, which is the
    interleaving that produced M2's pale grey rectangles, done deliberately.

    The candidate is otherwise complete, and the assertion is on the error's
    *type*. Written as `PanelCandidate(bus=2, cs=3, claimedBy="CPU")` this passes
    against a model that ignores extras -- the required `claimed_by` is then
    missing, and pydantic quotes the offered dict, misspelling and all, into the
    message. It was written that way first and proved nothing.
    """
    with pytest.raises(ValidationError) as refused:
        PanelCandidate(bus=2, cs=3, claimed_by=None, claimedBy="CPU")

    assert [(e["type"], e["loc"]) for e in refused.value.errors()] == [
        ("extra_forbidden", ("claimedBy",))
    ]


def test_a_candidate_that_says_nothing_about_its_claim_is_not_a_free_one():
    """No default on `claimed_by`, although None is a good value for it.

    A default would make "the daemon forgot the field" and "nobody is driving
    this" the same message, and the plausible one is the dangerous one.
    """
    with pytest.raises(ValidationError) as refused:
        PanelCandidate(bus=2, cs=3)

    assert refused.value.errors()[0]["type"] == "missing"


def test_a_claim_is_a_screen_name_and_is_bounded_like_one():
    assert PanelCandidate(bus=2, cs=3, claimed_by="x" * MAX_SCREEN_NAME).claimed_by

    with pytest.raises(ValidationError):
        PanelCandidate(bus=2, cs=3, claimed_by="x" * (MAX_SCREEN_NAME + 1))


def test_a_detect_result_keeps_every_candidate_where_the_daemon_put_it():
    """Three devices, no number equal to its own index or to its neighbour's field.

    The brief's first case uses `bus=0, cs=0`, which is the real default and
    worth keeping -- and is also the fixture that cannot tell a crossed field
    from a correct one. This is the case that can: the busy device is at index 0
    with bus 3, the free one at index 1 with bus 0, and the third names a
    different screen again, so a result that reordered, transposed or reused a
    claim fails here.
    """
    result = parse_daemon_message(
        DetectResult(
            request_id="r5",
            panels=[
                PanelCandidate(bus=3, cs=1, claimed_by="CPU"),
                PanelCandidate(bus=0, cs=2, claimed_by=None),
                PanelCandidate(bus=1, cs=0, claimed_by="NET"),
            ],
        ).model_dump_json()
    )

    assert isinstance(result, DetectResult)
    assert [(p.bus, p.cs, p.claimed_by) for p in result.panels] == [
        (3, 1, "CPU"),
        (0, 2, None),
        (1, 0, "NET"),
    ]


def test_a_rack_with_no_spi_devices_says_so_with_an_empty_list():
    """A real answer -- a Pi with SPI not enabled -- and it has to parse."""
    empty = parse_daemon_message(DetectResult(request_id="r6", panels=[]).model_dump_json())

    assert isinstance(empty, DetectResult)
    assert empty.panels == []


def test_a_detect_result_that_omits_its_panels_is_not_a_rack_with_none():
    with pytest.raises(ValidationError):
        parse_daemon_message('{"type": "detect_result", "request_id": "r7"}')


def test_a_detect_result_may_not_name_more_devices_than_a_rack_could_have():
    """The list is built from a directory listing on a machine the server cannot
    see, and the interface renders a row per entry."""
    at_bound = [PanelCandidate(bus=n, cs=1, claimed_by=None) for n in range(MAX_PANEL_CANDIDATES)]

    assert len(DetectResult(request_id="r8", panels=at_bound).panels) == MAX_PANEL_CANDIDATES

    with pytest.raises(ValidationError):
        DetectResult(
            request_id="r9",
            panels=[*at_bound, PanelCandidate(bus=0, cs=0, claimed_by=None)],
        )


def test_a_probe_result_carries_the_reason_it_failed():
    """A refusal with no reason in it is the failure this project has already
    shipped once. `ok` alone would leave the wizard saying "that did not work"."""
    failed = parse_daemon_message(
        ProbeResult(
            request_id="r10", ok=False, error="[Errno 2] No such file: /dev/spidev2.3"
        ).model_dump_json()
    )

    assert isinstance(failed, ProbeResult)
    assert failed.ok is False
    assert "spidev2.3" in (failed.error or "")


def test_a_probe_that_worked_carries_no_error_at_all():
    assert ProbeResult(request_id="r11", ok=True).error is None


def test_an_error_the_server_would_refuse_to_read_is_worse_than_a_short_one():
    """Bounded, and the daemon truncates to it rather than trusting a driver.

    An over-long error does not arrive truncated: the message fails to parse, the
    server drops it, the wait expires, and a probe whose reason the daemon knew
    answers with a timeout instead.
    """
    assert ProbeResult(request_id="r12", ok=False, error="x" * MAX_PROBE_ERROR).error

    with pytest.raises(ValidationError):
        ProbeResult(request_id="r13", ok=False, error="x" * (MAX_PROBE_ERROR + 1))


@pytest.mark.parametrize(
    "message",
    [
        DetectRequest,
        DetectResult,
        ProbeRequest,
        ProbeResult,
    ],
    ids=lambda m: m.__name__,
)
def test_no_correlation_id_on_any_of_these_may_be_empty_or_enormous(message):
    """An empty id correlates with nothing, and a huge one is a dictionary key
    the server holds until its wait expires -- chosen by whatever echoed it back.

    Both directions, because the request is the server's to mint and the reply is
    the daemon's to echo, and it is the echo that is not trusted.
    """
    rest = {
        DetectRequest: {},
        DetectResult: {"panels": []},
        ProbeRequest: {**WIRING, "hold_s": 1.0},
        ProbeResult: {"ok": True},
    }[message]

    assert message(request_id="x" * MAX_REQUEST_ID, **rest).request_id
    for refused in ("", "x" * (MAX_REQUEST_ID + 1)):
        with pytest.raises(ValidationError):
            message(request_id=refused, **rest)


def test_a_detection_message_travels_in_exactly_one_direction():
    """Union membership, which is the whole of what tasks 10 to 12 build on.

    A request that is not in `ServerMessage` cannot be sent; a result that is not
    in `DaemonMessage` arrives as `union_tag_invalid` and the server's wait
    expires with the reply sitting in a log. And a daemon must not be able to
    send a *request* -- the correlated wait is the server's, and a rack that could
    ask for a probe could ask a server to hold one open.
    """
    outbound = [
        DetectRequest(request_id="r14"),
        probe(),
    ]
    inbound = [
        DetectResult(request_id="r15", panels=[PanelCandidate(bus=3, cs=1, claimed_by="CPU")]),
        ProbeResult(request_id="r16", ok=True),
    ]

    for message in outbound:
        assert parse_server_message(message.model_dump_json()) == message
        with pytest.raises(ValidationError):
            parse_daemon_message(message.model_dump_json())

    for message in inbound:
        assert parse_daemon_message(message.model_dump_json()) == message
        with pytest.raises(ValidationError):
            parse_server_message(message.model_dump_json())


def test_the_new_types_are_named_when_an_unknown_tag_is_refused():
    """The version-skew error lists what is known, so a daemon one release behind
    reads which side is missing the message rather than guessing."""
    with pytest.raises(ValidationError) as caught:
        parse_daemon_message('{"type": "detect_resutl"}')

    message = caught.value.errors()[0]["msg"]
    assert "'detect_result'" in message and "'probe_result'" in message


def test_a_detect_result_is_not_smuggled_in_under_another_tag():
    """`extra="forbid"` on the envelope, on the pair that has a reply for every
    request: a `detect_result` body arriving tagged `heartbeat` would be recorded
    as a heartbeat, and the wait it was answering would time out with nothing
    logged."""
    with pytest.raises(ValidationError):
        parse_daemon_message('{"type": "heartbeat", "request_id": "r17", "panels": []}')


@pytest.mark.parametrize(
    ("body", "invented"),
    [
        ({"type": "detect", "request_id": "rq"}, "force"),
        ({"type": "probe", "request_id": "rq", **WIRING, "hold_s": 1.0}, "rotation"),
    ],
    ids=["detect", "probe"],
)
def test_a_request_carrying_a_field_this_build_never_defined_is_a_named_error(body, invented):
    with pytest.raises(ValidationError) as refused:
        parse_server_message(json.dumps({**body, invented: 1}))

    assert refused.value.errors()[0]["type"] == "extra_forbidden"
    assert invented in str(refused.value)


@pytest.mark.parametrize(
    ("body", "invented"),
    [
        ({"type": "detect_result", "request_id": "rq", "panels": []}, "spi_buses"),
        ({"type": "probe_result", "request_id": "rq", "ok": True}, "seen"),
    ],
    ids=["detect_result", "probe_result"],
)
def test_a_reply_carrying_a_field_this_build_never_defined_is_a_named_error(body, invented):
    """The direction that matters more: a daemon one release ahead answers with a
    field this server does not know, and the operator must read which field
    rather than watch a wait expire."""
    with pytest.raises(ValidationError) as refused:
        parse_daemon_message(json.dumps({**body, invented: 1}))

    assert refused.value.errors()[0]["type"] == "extra_forbidden"
    assert invented in str(refused.value)
