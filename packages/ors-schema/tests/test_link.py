import base64
import json

import pytest
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
    MAX_FRAME_BYTES,
    MAX_REQUESTED_FPS,
    PROTOCOL_VERSION,
    Ack,
    Command,
    ConfigPush,
    Frame,
    FramesRequest,
    Heartbeat,
    Hello,
    LogLine,
    Nack,
    Paired,
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
        "Frame",
        "FramesRequest",
        "Heartbeat",
        "Hello",
        "LogLine",
        "Nack",
        "Paired",
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
