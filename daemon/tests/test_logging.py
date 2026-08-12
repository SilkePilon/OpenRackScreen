import io
import json
import logging

from ors_daemon.logging import setup_logging


def test_package_importable():
    import ors_daemon

    assert isinstance(ors_daemon.__version__, str)


def test_records_are_json_lines_with_the_fields_journald_needs():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logging.getLogger("ors_daemon.test").info("panel online", extra={"screen": "CPU"})

    line = json.loads(stream.getvalue().strip())
    assert line["message"] == "panel online"
    assert line["level"] == "INFO"
    assert line["logger"] == "ors_daemon.test"
    assert line["screen"] == "CPU"
    assert "time" in line


def test_debug_is_suppressed_at_info_level():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logging.getLogger("ors_daemon.test").debug("noisy")

    assert stream.getvalue() == ""


def test_an_extra_cannot_overwrite_the_records_own_severity():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logging.getLogger("ors_daemon.test").info(
        "real", extra={"level": "SPAM", "time": "nope", "logger": "hijack"}
    )

    line = json.loads(stream.getvalue().strip())
    assert line["level"] == "INFO"
    assert line["logger"] == "ors_daemon.test"
    assert line["time"] != "nope"
