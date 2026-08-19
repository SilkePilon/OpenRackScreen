"""One containment, for one race in `starlette.testclient`, applied to this tree.

**The failure.** `WebSocketTestSession.__enter__` builds an `ExitStack` whose
callbacks run, in reverse, as `close(1000)`, then `portal.call(cs.cancel)`, then
`fut.result()`. The task behind `fut` is `_run`, which is `await self.app(...)`
followed by `await anyio.sleep_forever()` inside a cancel scope. Cancelling that
scope while `_run` is parked in `sleep_forever` is the ordinary path: the scope
absorbs its own cancellation and `_run` returns, so `fut` holds a result. When
the cancel instead lands while the *handler* is still unwinding its disconnect,
the `CancelledError` does not come back to the scope as the scope's own, anyio's
`BlockingPortal._call_func` takes its `except get_cancelled_exc_class()` branch
and calls `future.cancel()`, and `fut.result()` then raises `CancelledError` --
at the `with client.websocket_connect(...)` line, in whichever test happened to
be running.

**It is not ours.** Reproduced with a plain FastAPI websocket handler and no
`ors_server` import anywhere: one failure in 500 connect/disconnect rounds, with
the same traceback. The rate does not move with how much work the handler does
after the disconnect. Measured on this branch before it was contained: 1 full
suite run in 4, 1 in 12 runs of `test_ws_daemon.py` alone, 2 in 30 of the two
socket files together.

**Why it is contained rather than upgraded around.** starlette 1.6.0 is the
newest release there is, and it is the one this workspace pins; the exit-stack
ordering above is current in it. There is nothing to upgrade to.

**Why it is contained here and not at 26 call sites.** A guard a test has to
remember to use is a guard the twenty-seventh test forgets, and the symptom of
forgetting is a red CI run once a week that teaches everybody to press the
button again -- which is how a real failure eventually gets waved through. So it
is applied to the one method the race comes out of, for every test in this
directory, and it cannot be opted out of by accident.

**What it is allowed to do, exactly.** Catch `CancelledError`, from
`WebSocketTestSession.__exit__` and nowhere else, and answer with a falsy value.
Not a retry: nothing is run twice. Not a blanket: any other exception out of
that teardown is a real failure and propagates, and a falsy answer is what stops
`with` suppressing whatever the *body* raised, so a test that fails and also
meets the race still fails. `test_ws_teardown.py` pins all of that, including
the case that matters most -- a `CancelledError` raised inside the block, which
is the body's and never reaches this wrapper at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any

import pytest
from starlette.testclient import WebSocketTestSession

log = logging.getLogger(__name__)

Exit = Callable[..., Any]


def tolerating_the_teardown_race(exit_method: Exit) -> Exit:
    """Wrap one `__exit__` so that starlette's teardown cancel is not a failure.

    Returns the wrapped `__exit__` rather than patching anything, so the rule is
    one testable function and the patching is one line elsewhere.

    The swallowing path answers None deliberately. `__exit__` answering a truthy
    value is what suppresses the exception a `with` block raised, and this guard
    has no business doing that: the exception it is standing in front of came
    from the teardown, and the body's is somebody else's business entirely. A
    genuinely suppressing `__exit__` -- which `ExitStack.__exit__` is allowed to
    be -- keeps its answer, because it is returned untouched.
    """

    def guarded(self: Any, *args: Any) -> Any:
        try:
            return exit_method(self, *args)
        # `concurrent.futures.CancelledError`, which since 3.8 *is*
        # `asyncio.CancelledError` -- one class under two names. Imported from
        # the module the failing traceback names, so a reader looking it up
        # lands in the right place; it is a `BaseException` either way, which is
        # why nothing above catches it by accident.
        except CancelledError:
            # Debug rather than warning: this is expected, it is nobody's bug,
            # and a line per occurrence at any louder level would train people
            # to ignore the log. It is here at all so that a run which is
            # somehow full of them can be seen to be.
            log.debug("absorbed starlette's websocket teardown cancel")
            return None

    return guarded


@pytest.fixture
def teardown_race_guard() -> Exit:
    """The guard itself, for the tests that pin what it may and may not absorb.

    A fixture and not an import: `--import-mode=importlib` puts no test
    directory on `sys.path`, so `conftest` is not a module a test file can name.
    """
    return tolerating_the_teardown_race


@pytest.fixture(autouse=True)
def _no_mdns_announcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this directory announces itself on the network.

    `create_app` builds an `Announcer` unless `ORS_ANNOUNCE=0` says otherwise,
    and the four tests here that enter a `TestClient` as a context manager run
    the lifespan that starts it -- which binds sockets, joins a multicast group
    and advertises a port nothing in the test is listening on, out of whatever
    machine the suite is running on. Set here rather than in the four tests
    because the fifth one is the problem: a guard a test has to remember is a
    guard that gets forgotten, and forgetting it is invisible on a developer's
    laptop and a multicast packet out of a CI runner.

    `test_announce.py` is what covers the other side of the switch, and it
    unsets this itself for the tests that need it on -- with a stub in place of
    `Announcer`, so that even those transmit nothing.
    """
    monkeypatch.setenv("ORS_ANNOUNCE", "0")


@pytest.fixture(autouse=True)
def _websocket_teardown_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the guard to every test in this directory. See the module docstring.

    Through `monkeypatch` so it is undone after each test, which is what keeps
    the wrapping from nesting and keeps `teardown_race_guard` above looking at
    the original method.
    """
    monkeypatch.setattr(
        WebSocketTestSession,
        "__exit__",
        tolerating_the_teardown_race(WebSocketTestSession.__exit__),
    )
