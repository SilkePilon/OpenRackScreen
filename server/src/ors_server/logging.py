from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TextIO

# A near-copy of `ors_daemon.logging`, deliberately: `ors-server` does not
# depend on `ors-daemon` and must not start to over a formatter -- the server
# runs in a container and the daemon on a Pi, and the two are released apart.
# `ors-schema` is the package they do share, but it is the wire contract
# between them and a logging handler is not part of that contract. Twenty lines
# duplicated is the cheaper of the two mistakes; if a third consumer ever
# appears, that is the moment for a shared package rather than this one.

# Everything the logging module itself puts on a record, so that only caller
# `extra=` keys survive into the JSON payload. Deriving it from a throwaway
# record rather than a hand-written list keeps it correct across versions:
# `taskName`, for instance, exists only on 3.12+. `message` and `asctime` are
# the two the *formatter*, not the record, adds, so they are named explicitly:
# `Formatter.format` sets them on the shared record, and a second handler
# formatting the same record would otherwise see them here.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

_DEFAULT_LEVEL = "INFO"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so the container log and the shipper agree."""

    def format(self, record: logging.LogRecord) -> str:
        # Extras go in first so the base fields below always win. `extra=` keys
        # are not LogRecord attributes, so `makeRecord` does not reject a caller
        # passing `level` or `time` -- and a config object logged with a `level`
        # field would otherwise rewrite the record's own severity in the output.
        payload: dict[str, object] = {
            key: value for key, value in record.__dict__.items() if key not in _RESERVED
        }
        payload["time"] = datetime.fromtimestamp(record.created, UTC).isoformat()
        payload["level"] = record.levelname
        payload["logger"] = record.name
        payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = _DEFAULT_LEVEL, stream: TextIO | None = None) -> None:
    """Install the JSON handler on the `ors_server` logger, replacing any prior one.

    Called before `uvicorn.run`, which configures logging itself: it applies
    `uvicorn.config.LOGGING_CONFIG`, and that names only the `uvicorn*` loggers
    and carries `disable_existing_loggers: false`, so what is installed here
    survives being overtaken. Which is why the server keeps uvicorn's default
    `log_config` rather than passing its own: uvicorn's access and error logs
    are formatted by handlers this never touches, and taking them over would
    mean reimplementing `AccessFormatter` to gain nothing.

    Only the `ors_server` tree, and `propagate = False`: the root logger stays
    handler-less, so a library that logs on import cannot start emitting JSON
    lines claiming to be the server.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("ors_server")
    logger.handlers.clear()
    logger.addHandler(handler)
    # Normalised rather than passed through, because this comes from an
    # environment variable in a compose file: `debug` is the obvious way to
    # write it and `setLevel("debug")` raises. A level nobody recognises falls
    # back to INFO for the same reason -- a typo there must not be why a
    # container refuses to boot, when the cost of guessing is one wrong verbosity.
    logger.setLevel(logging.getLevelNamesMapping().get(level.upper(), logging.INFO))
    logger.propagate = False
