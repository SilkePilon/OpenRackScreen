"""How the shipped server is actually served.

Everything else about the server is tested through `create_app` and a test
client, which never goes near uvicorn -- so the transport settings, which are
the ones a browser and a rack meet first, had nothing asserting them at all.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest
import uvicorn
import uvicorn.config
from ors_schema.link import MAX_FRAME_BYTES
from ors_server.__main__ import WS_MAX_MESSAGE_BYTES, main

_TOUCHED = ("ors_server", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging():
    """`main` calls `setup_logging`, which is global and outlives the test.

    It sets `ors_server` to INFO with `propagate = False`, and a suite that let
    that stand would change what every later test's `caplog` can see -- which it
    did, in `test_ws_daemon`, as two failures about a record that had never been
    emitted before. The same fixture guards `test_logging` for the same reason.
    """
    saved = [(logging.getLogger(name), list(logging.getLogger(name).handlers)) for name in _TOUCHED]
    levels = [(logger, logger.level, logger.propagate) for logger, _ in saved]
    yield
    for logger, handlers in saved:
        logger.handlers[:] = handlers
    for logger, level, propagate in levels:
        logger.setLevel(level)
        logger.propagate = propagate


def served(monkeypatch, tmp_path) -> dict[str, Any]:
    """The keyword arguments `main` hands uvicorn, without binding a port."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.append(kwargs))
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    assert main() == 0

    return captured[0]


def test_the_server_bounds_a_websocket_message_rather_than_inheriting_16_mib(
    monkeypatch, tmp_path
) -> None:
    """The bound that runs before the application sees anything.

    `Frame.webp`'s `max_length` counts the decoded payload, so it can only
    refuse a message this process has already read and base64-decoded. Left at
    uvicorn's default that is 16 MiB of allocation per message, at whatever rate
    a peer can write them, for anybody who can open `/ws/daemon` -- and the
    daemon on the other end of a valid key is one of them.
    """
    assert served(monkeypatch, tmp_path)["ws_max_size"] == WS_MAX_MESSAGE_BYTES

    default = inspect.signature(uvicorn.config.Config.__init__).parameters["ws_max_size"].default
    assert default == 16 * 1024 * 1024, "uvicorn's default moved; re-read this decision"
    assert WS_MAX_MESSAGE_BYTES < default


def test_a_frame_the_schema_allows_still_fits_on_the_wire() -> None:
    """The bound is above `MAX_FRAME_BYTES` and not equal to it, deliberately.

    Base64 is 4/3 of what it carries, so a payload at the schema's bound is
    349,528 bytes before the JSON envelope. A transport limit set to the
    schema's number would close the socket over a frame the schema accepts,
    which is exactly the reconnect loop `MAX_FRAME_BYTES` exists to prevent --
    reached from the transport instead of from the encoder.
    """
    on_the_wire = -(-MAX_FRAME_BYTES // 3) * 4

    assert on_the_wire == 349_528
    assert WS_MAX_MESSAGE_BYTES > on_the_wire


def test_the_server_reads_forwarded_headers_from_a_reverse_proxy(monkeypatch, tmp_path) -> None:
    """`FORWARDED_ALLOW_IPS` is inert without this, and it is what the deploy
    notes tell an operator to set. It is already uvicorn's default, so what is
    being pinned is that a future release flipping it cannot silently turn every
    client address in this deployment into the proxy's own."""
    assert served(monkeypatch, tmp_path)["proxy_headers"] is True

    default = inspect.signature(uvicorn.config.Config.__init__).parameters["proxy_headers"].default
    assert default is True, "uvicorn's default moved; the deploy notes depend on this"
