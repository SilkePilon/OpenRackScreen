from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TextIO

# Everything the logging module itself puts on a record, so that only caller
# `extra=` keys survive into the JSON payload. Deriving it from a throwaway
# record rather than a hand-written list keeps it correct across versions:
# `taskName`, for instance, exists only on 3.12+. Verified on 3.13 by diffing
# this set against a real record captured in a handler (with args, exc_info,
# stack_info and an extra) -- nothing leaked. `message` and `asctime` are the
# two the *formatter*, not the record, adds, so they are named explicitly:
# `Formatter.format` sets them on the shared record, and a second handler
# formatting the same record would otherwise see them here.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so journald and the log shipper agree."""

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


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Install the JSON handler on the ors_daemon logger, replacing any prior one."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("ors_daemon")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
