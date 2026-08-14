"""What the websocket teardown guard may absorb, and what it may not.

The guard exists because of a race inside `starlette.testclient`, not because of
anything in `ors_server` -- `conftest.py` carries the measurement and the
reasoning. These tests are about the *containment*: a helper that quietly ate a
real failure would be worse than the flake it was written for, so all but the
first of them are about something it has to let through.

It is reached through a fixture rather than imported, because `--import-mode=
importlib` puts no test directory on `sys.path` and `conftest` is therefore not
a module any test file can name. The fixture is the supported way in.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import CancelledError

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient


class _Session:
    """A context manager whose exit does whatever a test tells it to."""

    def __init__(self, on_exit) -> None:
        self._on_exit = on_exit
        self.args: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.args = args
        return self._on_exit()


def _raise(error: BaseException):
    def raiser():
        raise error

    return raiser


@pytest.fixture
def guarded(teardown_race_guard):
    """A `_Session` subclass whose `__exit__` has been through the guard."""

    def build(on_exit) -> _Session:
        wrapped = type(
            "_Guarded", (_Session,), {"__exit__": teardown_race_guard(_Session.__exit__)}
        )
        return wrapped(on_exit)

    return build


def test_a_cancelled_teardown_is_absorbed(guarded) -> None:
    with guarded(_raise(CancelledError())):
        pass


def test_the_body_still_fails_when_the_teardown_is_also_cancelled(guarded) -> None:
    """The one thing this may never do: the body's failure has to reach pytest.

    The guard returns a falsy value on the path where it swallows, which is what
    stops `with` suppressing whatever the body raised -- so a test that fails
    *and* meets the race fails, rather than passing because its teardown was
    noisy.
    """
    with pytest.raises(ValueError, match="the body"), guarded(_raise(CancelledError())):
        raise ValueError("the body failed")


def test_a_cancel_from_the_body_is_not_absorbed(guarded) -> None:
    """A cancel raised *inside* the block is the body's, and it is not this race.

    The guard stands in front of one method and catches what that method raises;
    a `CancelledError` out of the block never reaches it. Asserted because the
    two are the same exception type, and "only the teardown" is the whole
    promise.
    """
    with pytest.raises(CancelledError), guarded(lambda: None):
        raise CancelledError


def test_any_other_teardown_failure_is_not_absorbed(guarded) -> None:
    with (
        pytest.raises(RuntimeError, match="left open"),
        guarded(_raise(RuntimeError("the socket was left open"))),
    ):
        pass


def test_a_suppressing_exit_is_still_a_suppressing_exit(guarded) -> None:
    """`ExitStack.__exit__` may legitimately answer True, and that has to survive.

    Nothing in `WebSocketTestSession` does today, but the guard stands in front
    of a method whose contract allows it -- and one that flattened the answer to
    None would change what `with` does for a reason that has nothing to do with
    the race.
    """
    with guarded(lambda: True):
        raise ValueError("suppressed by the exit")


def test_the_exception_details_reach_the_wrapped_exit(guarded) -> None:
    """The real `__exit__` hands its arguments to an `ExitStack`, which uses them."""
    session = guarded(lambda: None)
    with pytest.raises(ValueError), session:
        raise ValueError("boom")
    assert session.args is not None
    assert session.args[0] is ValueError


class _CancellingStack:
    """The real exit stack, plus the cancel starlette's teardown can raise.

    It closes the stack it wraps first, so this test tears the session down for
    real -- what is being asserted is that the cancel *after* that is absorbed,
    not that skipping the teardown is survivable.
    """

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    def __exit__(self, *args):
        self._wrapped.__exit__(*args)
        raise CancelledError


def test_the_guard_is_applied_to_a_real_test_client_session() -> None:
    """The autouse fixture is what makes the guard reach the 26 `with` blocks.

    Asserted through the real class rather than by reading its `__qualname__`:
    the claim is that a cancel out of a real `WebSocketTestSession.__exit__` is
    absorbed, and the only honest way to make one is to put it there.
    """
    app = FastAPI()

    @app.websocket("/ws")
    async def handler(socket: WebSocket) -> None:
        await socket.accept()
        with contextlib.suppress(WebSocketDisconnect):
            await socket.receive_text()

    client = TestClient(app)
    with client.websocket_connect("/ws") as session:
        session.send_text("hello")
        session.exit_stack = _CancellingStack(session.exit_stack)
