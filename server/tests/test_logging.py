from __future__ import annotations

import io
import json
import logging
import logging.config

import pytest
import uvicorn
import uvicorn.config
from ors_server.logging import setup_logging

# Every logger these tests reconfigure. `logging` is process-global, so a test
# that installs a handler and walks away changes the run for everyone after it.
_TOUCHED = ("ors_server", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging():
    saved = [(logging.getLogger(name), list(logging.getLogger(name).handlers)) for name in _TOUCHED]
    levels = [(logger, logger.level, logger.propagate) for logger, _ in saved]
    yield
    for logger, handlers in saved:
        logger.handlers[:] = handlers
    for logger, level, propagate in levels:
        logger.setLevel(level)
        logger.propagate = propagate


def handlers_reached_from(name: str) -> list[logging.Handler]:
    """Every handler a record logged to `name` would actually reach.

    Which is the question the shipped server got wrong: `ors_server.link.hub`
    had a logger and no handler anywhere above it, so both of its log calls --
    including the one recording a daemon taken offline by a failed send, the
    single failure mode with no automatic recovery -- went nowhere at all.
    """
    logger: logging.Logger | None = logging.getLogger(name)
    found: list[logging.Handler] = []
    while logger is not None:
        found.extend(logger.handlers)
        if not logger.propagate:
            break
        logger = logger.parent
    return found


def test_a_record_from_the_hub_reaches_a_handler_with_its_extra_intact():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.getLogger("ors_server.link.hub").info(
        "daemon send failed; dropping", extra={"daemon": 3, "error": "gone"}
    )

    assert handlers_reached_from("ors_server.link.hub")
    line = json.loads(stream.getvalue().strip())
    assert line["message"] == "daemon send failed; dropping"
    assert line["level"] == "INFO"
    assert line["logger"] == "ors_server.link.hub"
    assert line["daemon"] == 3, "the hub's log calls are all `extra=`; a formatter that drops it"
    assert line["error"] == "gone", "records the fact and loses the daemon it happened to"
    assert "time" in line


def test_a_warning_carries_the_fields_it_was_given():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.getLogger("ors_server.link.hub").warning(
        "daemon send timed out; dropping", extra={"daemon": 3, "timeout_s": 5.0}
    )

    line = json.loads(stream.getvalue().strip())
    assert line["level"] == "WARNING"
    assert line["timeout_s"] == 5.0


def test_an_extra_cannot_overwrite_the_records_own_severity():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.getLogger("ors_server.test").info(
        "real", extra={"level": "SPAM", "time": "nope", "logger": "hijack"}
    )

    line = json.loads(stream.getvalue().strip())
    assert line["level"] == "INFO"
    assert line["logger"] == "ors_server.test"
    assert line["time"] != "nope"


def test_an_exception_is_rendered_rather_than_swallowed():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    try:
        raise ConnectionResetError("gone")
    except ConnectionResetError:
        logging.getLogger("ors_server.test").exception("socket died")

    line = json.loads(stream.getvalue().strip())
    assert "ConnectionResetError" in line["exception"]


def test_debug_is_suppressed_at_info_level():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.getLogger("ors_server.link.hub").debug("dropped a frame for a slow watcher")

    assert stream.getvalue() == ""


def test_the_dropped_frame_line_is_readable_at_debug_level():
    stream = io.StringIO()
    setup_logging(level="DEBUG", stream=stream)

    logging.getLogger("ors_server.link.hub").debug("dropped a frame", extra={"screen": 2})

    assert json.loads(stream.getvalue().strip())["screen"] == 2


def test_a_level_is_normalised_rather_than_taken_literally():
    # `ORS_LOG_LEVEL=debug` in a compose file is the obvious way to write it,
    # and `setLevel("debug")` raises -- a typo in an env var must not be the
    # reason a container will not boot.
    stream = io.StringIO()
    setup_logging(level="debug", stream=stream)

    logging.getLogger("ors_server.test").debug("quiet")

    assert stream.getvalue() != ""


def test_an_unknown_level_falls_back_to_info_instead_of_refusing_to_start():
    stream = io.StringIO()
    setup_logging(level="verbose", stream=stream)

    logging.getLogger("ors_server.test").info("audible")
    logging.getLogger("ors_server.test").debug("not")

    assert len(stream.getvalue().strip().splitlines()) == 1


def test_setup_replaces_its_own_handler_rather_than_stacking_them():
    first, second = io.StringIO(), io.StringIO()
    setup_logging(level="INFO", stream=first)
    setup_logging(level="INFO", stream=second)

    logging.getLogger("ors_server.test").info("once")

    assert first.getvalue() == ""
    assert len(second.getvalue().strip().splitlines()) == 1


def test_a_line_carries_what_it_was_given_and_nothing_the_logging_module_added():
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.getLogger("ors_server.test").info("plain", extra={"daemon": 1})

    # `taskName` is the one that bites: it is a real `LogRecord` attribute on
    # 3.12+ and would ride along, None and unasked for, in every line.
    assert set(json.loads(stream.getvalue())) == {"time", "level", "logger", "message", "daemon"}


def test_a_handler_formatting_the_record_first_does_not_leak_its_fields_into_ours():
    # `Formatter.format` writes `message` and `asctime` onto the record itself,
    # which every later handler then sees. Uvicorn's own default formatter is
    # exactly such a handler, one `propagate` away.
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    logger = logging.getLogger("ors_server.test")
    first = logging.StreamHandler(io.StringIO())
    first.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.handlers.insert(0, first)

    logger.info("plain")

    assert set(json.loads(stream.getvalue())) == {"time", "level", "logger", "message"}


def test_a_root_handler_does_not_get_a_second_copy_of_every_line():
    # Something else in the process -- a library, a harness -- putting a handler
    # on the root logger must not turn every server line into two, one of them
    # in a format the log shipper cannot parse.
    doubled = io.StringIO()
    root = logging.StreamHandler(doubled)
    logging.getLogger().addHandler(root)
    try:
        setup_logging(level="INFO", stream=io.StringIO())
        logging.getLogger("ors_server.test").info("once")
    finally:
        logging.getLogger().removeHandler(root)

    assert doubled.getvalue() == ""


def test_uvicorns_logging_config_leaves_the_server_logger_alone():
    # The interaction the whole approach rests on: `uvicorn.run` calls
    # `dictConfig(LOGGING_CONFIG)` from inside itself, after `main` has already
    # set up ours. That config carries `disable_existing_loggers: false` and
    # names only the `uvicorn*` loggers, so ours survives -- and uvicorn keeps
    # its own access and error logs, which is why this does not pass
    # `log_config=None` and take them over.
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)

    logging.getLogger("ors_server.link.hub").info("still here", extra={"daemon": 1})
    assert json.loads(stream.getvalue().strip())["daemon"] == 1
    assert handlers_reached_from("uvicorn.access"), "uvicorn's access log must keep working"
    assert handlers_reached_from("uvicorn.error"), "and so must its error log"


def test_main_installs_logging_before_it_serves(tmp_path, monkeypatch):
    from ors_server import __main__

    # Asserted from inside `uvicorn.run`, because "configured at some point" is
    # not the claim: the server must be logging by the time it is serving.
    reached: list[list[logging.Handler]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: reached.append(handlers_reached_from("ors_server"))
    )
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ORS_LOG_LEVEL", raising=False)

    assert __main__.main() == 0

    assert reached and reached[0], "the shipped server had no handler on `ors_server` at all"
    assert logging.getLogger("ors_server.link.hub").isEnabledFor(logging.INFO)
