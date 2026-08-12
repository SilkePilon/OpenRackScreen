import base64
import json

import pytest
from ors_schema.daemon import DaemonConfig
from ors_schema.link import (
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
        "ServerMessage",
        "SourceStatus",
        "parse_daemon_message",
        "parse_server_message",
    }

    assert exported <= set(ors_schema.__all__)
    assert all(hasattr(ors_schema, name) for name in exported)
