"""The two questions the wizard asks a rack, and every way neither is answered.

`POST /api/daemons/{id}/detect` and `POST /api/daemons/{id}/probe` are the only
routes in this server that ask a Pi something and wait for the reply, so they are
the only ones that have to *report* a silence. `hub.request` answers `None` to
all four ways that happens -- no socket, a send that never left, nobody
answering, the rack going away mid-question -- and one `None` is the right
signature for the hub and the wrong answer for an operator: "the rack is not
plugged in" and "the rack is connected and ignoring me" send somebody to
different places. The tests below pin the two apart, and pin the reason each
refusal carries.

**Every number in a fixture here is different from every other one.** The rack
under test is id 3 because two spares are created ahead of it, the wiring is
bus 2, cs 5, dc 27, rst 17, and the panels a rack reports are (6, 4) and (2, 5)
at list indices 0 and 1. `(0, 0)` never appears, because it is the real default
for `DisplayConfig.spi_bus` and `spi_cs` and a route that ignored the body
entirely would pass against it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import closing

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from ors_schema import link
from ors_schema.link import MAX_REQUEST_ID, DetectResult, PanelCandidate, ProbeResult
from ors_server.api import daemons
from ors_server.app import AppSettings, create_app
from ors_server.link.hub import REQUEST_TIMEOUT, SEND_TIMEOUT

# The AST checks the two rules below are read with, taken from the sweep that
# owns them rather than copied. A second copy is a second thing to keep true:
# `test_api_routes.py`'s `writes` reads an f-string's leading text and treats a
# statement it cannot see as a write, and the copy that stood here did neither --
# so a route this file swore wrote nothing could have been believed by the weaker
# of two checkers with the same name. Imported by path because the suite runs
# with `--import-mode=importlib`, under which a bare `test_api_routes` is not a
# module name.
from server.tests.test_api_routes import opens_a_change, writes

DETECT_PATH = "/api/daemons/{daemon_id}/detect"
PROBE_PATH = "/api/daemons/{daemon_id}/probe"

WIRING = {"bus": 2, "cs": 5, "dc": 27, "rst": 17, "hz": 32_000_000, "hold_s": 2.5}
"""The wiring an operator typed into the wizard. No two numbers in it coincide.

`hz` is not the daemon's own 40 MHz default and `hold_s` is not the bound, so a
route that dropped a field and let the schema's default stand would be visible
in what reached the rack.
"""

FOUND = [(6, 4, "CPU"), (2, 5, None)]
"""What a rack answers a detect with: a claimed device first, then a free one.

The claimed one leads deliberately -- a route that filtered the list down to what
the wizard can offer would answer with one panel here, and the operator would
never learn that SPI6.4 exists and is spoken for.
"""

TURNS_LATE = 4
"""How many turns of the event loop `LateRack` takes to answer. More than one, so
that a wait whose deadline has already passed gives up first however the loop
orders the two."""

A_NUMBER = "a bus, chip select, pin or clock is a number"
A_DURATION = "a hold is a duration in seconds"
"""The two sentences `ProbeBody` refuses a flag with, and the reason there are
two of them: a `hold_s` turned down as a chip select is a correct refusal
carrying a false reason, and the reason is all an operator gets. Written here
rather than imported, because what is being checked is what reaches the browser
-- a validator that stopped passing `what=` would still satisfy a comparison
against whatever it passes now."""

CLAIMED = "SPI2.5 is already driving the screen 'CPU'; change that screen's wiring first"
"""A daemon's own refusal, flattened to `ok=False` plus a sentence -- see
`ors_daemon.supervisor.ProbeRefused`, which deliberately does not distinguish a
claimed device from one that opened and would not take a frame."""


def detect_of(daemon_id: int) -> str:
    return DETECT_PATH.format(daemon_id=daemon_id)


def probe_of(daemon_id: int) -> str:
    return PROBE_PATH.format(daemon_id=daemon_id)


def build(tmp_path) -> TestClient:
    client = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    client.post("/api/auth/setup", json={"password": "pw"})
    client.post("/api/auth/login", json={"password": "pw"})
    return client


@pytest.fixture
def client(tmp_path) -> TestClient:
    client = build(tmp_path)
    # Two racks nobody here asks about, so that the rack under test is id 3 and
    # never coincides with a bus, a chip select, a list index or a count.
    client.post("/api/daemons", json={"name": "spare-rack"})
    client.post("/api/daemons", json={"name": "another-rack"})
    return client


@pytest.fixture
def daemon_id(client: TestClient) -> int:
    return client.post("/api/daemons", json={"name": "pi-rack"}).json()["id"]


def answering(
    panels: list[tuple[int, int, str | None]] | None = None,
    *,
    ok: bool = True,
    error: str | None = None,
):
    """A rack that answers whatever it is asked, echoing the id it was given."""

    listed = FOUND if panels is None else panels

    def reply(asked: dict) -> DetectResult | ProbeResult | None:
        if asked["type"] == "detect":
            return DetectResult(
                request_id=asked["request_id"],
                panels=[
                    PanelCandidate(bus=bus, cs=cs, claimed_by=claimed_by)
                    for bus, cs, claimed_by in listed
                ],
            )
        if asked["type"] == "probe":
            return ProbeResult(request_id=asked["request_id"], ok=ok, error=error)
        return None

    return reply


def silent(asked: dict) -> None:
    """A rack that takes the question and says nothing back. Not an error: a Pi
    mid-apply holds its bus guard and answers late or never."""
    return None


class Rack:
    """A daemon's end of the hub, with no daemon behind it.

    The hub holds a `send` callable and nothing else, so this is the whole of
    what a connected rack is as far as these routes are concerned. The reply is
    delivered from *inside* the send, which is where a fast rack's answer really
    arrives -- `Hub.request` registers its wait before it sends for exactly that
    reason -- so nothing here has to be scheduled or slept on.

    Registered from the test thread, which is safe only because no request is in
    flight while it happens: `TestClient` runs the app on a portal thread that is
    idle between calls.
    """

    def __init__(self, app: FastAPI, daemon_id: int, reply=None) -> None:
        self.hub = app.state.hub
        self.daemon_id = daemon_id
        self.asked: list[dict] = []
        self.reply = answering() if reply is None else reply
        self.connection = self.hub.register(daemon_id, self.send)

    async def send(self, payload: str | bytes) -> None:
        asked = json.loads(payload)
        self.asked.append(asked)
        answer = self.reply(asked)
        if answer is not None:
            self.hub.deliver_reply(self.daemon_id, answer)

    def questions(self, kind: str) -> list[dict]:
        return [asked for asked in self.asked if asked["type"] == kind]


class VanishingRack(Rack):
    """A rack whose socket goes away between the question and the answer.

    The race the 503/504 split has to survive: it is online when the route looks,
    and gone by the time the answer does not come. `drop` abandons the wait, so
    `hub.request` answers `None` immediately and the route has to decide which
    silence it was from the state it can still see.
    """

    async def send(self, payload: str | bytes) -> None:
        self.asked.append(json.loads(payload))
        self.hub.drop(self.connection)


class GatedRack(Rack):
    """A rack whose send really suspends, so a second request can arrive mid-question.

    `Rack.send` reaches no await, so nothing else on the loop runs while the hub
    is inside it -- and "two probes in flight against one rack" is exactly the
    window that never opens. This one holds the send open until the test says so.
    """

    def __init__(self, app: FastAPI, daemon_id: int, reply=None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        super().__init__(app, daemon_id, reply)

    async def send(self, payload: str | bytes) -> None:
        asked = json.loads(payload)
        self.asked.append(asked)
        self.started.set()
        await self.release.wait()
        answer = self.reply(asked)
        if answer is not None:
            self.hub.deliver_reply(self.daemon_id, answer)


class LateRack(Rack):
    """A rack whose answer arrives some turns of the loop after the question.

    `Rack` delivers from inside the send, which is a rack that had already
    answered before `Hub.request` reached its wait -- so the timeout it was given
    is never consulted at all, and a zero one returns the answer through
    `asyncio.wait_for`'s fast path. This is the other rack: the one the wait is
    for.

    Turns of the loop and not seconds, because no test in this suite may spend
    wall clock. It is enough: what decides between an answer and a 504 is whether
    the future was done when the wait arrived, and a deadline already in the past
    fires before the first of these turns has been taken.
    """

    def __init__(self, app: FastAPI, daemon_id: int, reply=None) -> None:
        super().__init__(app, daemon_id, reply)
        # Held, because the loop keeps only a weak reference to a running task
        # and a collected one is an answer that never arrives.
        self.answering: list[asyncio.Task] = []

    async def send(self, payload: str | bytes) -> None:
        asked = json.loads(payload)
        self.asked.append(asked)
        answer = self.reply(asked)
        if answer is not None:
            self.answering.append(asyncio.create_task(self._answer_late(answer)))

    async def _answer_late(self, answer) -> None:
        for _ in range(TURNS_LATE):
            await asyncio.sleep(0)
        self.hub.deliver_reply(self.daemon_id, answer)


def caller_for(app: FastAPI) -> httpx2.AsyncClient:
    """A caller that does not block the loop the app runs on.

    `TestClient` drives the app from a portal thread and returns one response at
    a time, so nothing built on it can have two requests in flight at once --
    which is the only state in which "one probe at a time per rack" means
    anything.
    """
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://rack.test")


async def sign_in(caller: httpx2.AsyncClient) -> None:
    await caller.post("/api/auth/setup", json={"password": "pw"})
    await caller.post("/api/auth/login", json={"password": "pw"})


async def a_rack_on(caller: httpx2.AsyncClient) -> int:
    """The same two spares and the same third rack the `daemon_id` fixture makes."""
    await caller.post("/api/daemons", json={"name": "spare-rack"})
    await caller.post("/api/daemons", json={"name": "another-rack"})
    created = await caller.post("/api/daemons", json={"name": "pi-rack"})
    return created.json()["id"]


# --- what a rack answers ----------------------------------------------------


def test_detect_answers_with_what_the_rack_reported(client, daemon_id):
    """Every device the rack found, claimed ones included, in the order it listed
    them. The claim is the field the wizard reads before it offers a device, so a
    route that dropped it would offer a panel a live worker is mid-frame on."""
    rack = Rack(client.app, daemon_id)

    answer = client.post(detect_of(daemon_id))

    assert answer.status_code == 200
    assert answer.json() == {
        "panels": [
            {"bus": 6, "cs": 4, "claimed_by": "CPU"},
            {"bus": 2, "cs": 5, "claimed_by": None},
        ]
    }
    assert [asked["type"] for asked in rack.asked] == ["detect"]


def test_a_rack_with_no_spi_devices_answers_an_empty_list_rather_than_a_failure(client, daemon_id):
    """An empty list is a real answer -- a Pi with no SPI overlay enabled gives
    it -- and it is not a silence. `Reply` has no falsy member for this reason."""
    Rack(client.app, daemon_id, reply=answering(panels=[]))

    answer = client.post(detect_of(daemon_id))

    assert answer.status_code == 200
    assert answer.json() == {"panels": []}


def test_a_probe_the_rack_proved_is_reported_with_the_wiring_it_was_given(client, daemon_id):
    rack = Rack(client.app, daemon_id)

    answer = client.post(probe_of(daemon_id), json=WIRING)

    assert answer.status_code == 200
    assert answer.json() == {"ok": True, "error": None}
    asked = rack.questions("probe")[0]
    assert {key: asked[key] for key in WIRING} == WIRING


def test_probe_refuses_a_device_the_rack_says_is_claimed(client, daemon_id):
    """The rack's refusal, carried back whole.

    `ok` is the verdict and a reply that arrived is not one: a probe that was
    refused because SPI2.5 is already driving a screen answers 200 with
    `ok: false` and the sentence the daemon wrote, because the probe *ran* and
    the answer is no. Reporting the arrival of a reply as a success is this
    file's own history -- `send_command` once answered `delivered: true` for a
    command nothing received.
    """
    Rack(client.app, daemon_id, reply=answering(ok=False, error=CLAIMED))

    answer = client.post(probe_of(daemon_id), json=WIRING)

    assert answer.status_code == 200
    assert answer.json() == {"ok": False, "error": CLAIMED}


# --- the silences, which are not one thing ----------------------------------


def test_detect_on_an_offline_rack_says_so_rather_than_hanging(client, daemon_id):
    """No socket at all: 503, and a reason naming what to do about it."""
    detected = client.post(detect_of(daemon_id))
    probed = client.post(probe_of(daemon_id), json=WIRING)

    assert detected.status_code == 503
    assert probed.status_code == 503
    for answer in (detected, probed):
        detail = answer.json()["detail"]
        assert str(daemon_id) in detail
        assert "not connected" in detail
        # And it names the thing that does work, which is the standard
        # `send_command`'s 501s set: a refusal that only says no leaves the
        # reader waiting for a button that is not coming.
        assert "GET /api/daemons" in detail


def test_a_rack_that_does_not_answer_times_out_with_a_reason(client, daemon_id, monkeypatch):
    """Connected and silent: 504, which is the *other* answer and has to stay
    distinguishable from 503. One collapsed status sends an operator to check a
    cable on a rack that is streaming frames."""
    # Zero rather than a short wait: `asyncio.wait_for` gives up without ever
    # scheduling a timer, so nothing here sleeps for time to pass.
    monkeypatch.setattr(daemons, "DETECT_TIMEOUT", 0.0)
    monkeypatch.setattr(daemons, "PROBE_TIMEOUT", 0.0)
    rack = Rack(client.app, daemon_id, reply=silent)

    detected = client.post(detect_of(daemon_id))
    probed = client.post(probe_of(daemon_id), json=WIRING)

    assert detected.status_code == 504
    assert probed.status_code == 504
    for answer in (detected, probed):
        detail = answer.json()["detail"]
        assert str(daemon_id) in detail
        assert "did not answer" in detail
        # Where to look next, and it is a different place from the 503's.
        assert f"GET /api/events?daemon_id={daemon_id}" in detail
    assert [asked["type"] for asked in rack.asked] == ["detect", "probe"], "both really went out"


def test_a_rack_that_goes_away_mid_question_is_reported_as_gone_rather_than_silent(
    client, daemon_id
):
    """The race the split has to survive, and the reason the deciding check is
    the one *after* the call: the rack was online when the question was asked and
    is not by the time there is no answer, so 503 is the true report."""
    VanishingRack(client.app, daemon_id)

    answer = client.post(detect_of(daemon_id))

    assert answer.status_code == 503
    assert "not connected" in answer.json()["detail"]


def test_a_rack_that_answers_the_other_question_is_not_reported_as_a_result(client, daemon_id):
    """`deliver_reply` matches the id and the rack, and nothing on the wire says
    which of the two questions a reply belongs to. A `ProbeResult` handed to a
    detect has no `panels`, so the alternative to this is a 500."""

    def crossed(asked: dict) -> DetectResult | ProbeResult:
        if asked["type"] == "detect":
            return ProbeResult(request_id=asked["request_id"], ok=True)
        return DetectResult(request_id=asked["request_id"], panels=[])

    rack = Rack(client.app, daemon_id, reply=crossed)

    detected = client.post(detect_of(daemon_id))
    probed = client.post(probe_of(daemon_id), json=WIRING)

    assert detected.status_code == 502
    assert probed.status_code == 502
    assert "probe_result" in detected.json()["detail"]
    assert "detect_result" in probed.json()["detail"]

    # And the rack is still probeable afterwards: the 502 is composed outside the
    # `with`, so the guard has already released -- the fourth release path, and
    # the only one the three named release tests do not reach. Held instead, a
    # rack running one build ahead of this server would refuse every later probe
    # until a restart, which is the wrong rack to take out of service.
    rack.reply = answering()

    assert client.post(probe_of(daemon_id), json=WIRING).status_code == 200


def test_a_question_for_a_daemon_that_does_not_exist_is_a_404(client):
    """Before "is it connected", because a rack that was never created is not an
    offline rack and "not connected" would send somebody to look at a Pi."""
    assert client.post(detect_of(87)).status_code == 404
    assert client.post(probe_of(87), json=WIRING).status_code == 404


# --- one probe at a time per rack, which is spec 6.4 ------------------------


async def test_a_second_probe_while_one_is_in_flight_is_refused_with_a_reason(tmp_path):
    """A probe is a real panel init that holds the rack's SPI bus for its whole
    hold, so two at once interleave on the bus. Refused with 409 and a sentence
    rather than left to answer an indistinguishable silence."""
    app = create_app(AppSettings(data_dir=tmp_path))
    async with caller_for(app) as caller:
        await sign_in(caller)
        daemon_id = await a_rack_on(caller)
        rack = GatedRack(app, daemon_id)

        first = asyncio.create_task(caller.post(probe_of(daemon_id), json=WIRING))
        await asyncio.wait_for(rack.started.wait(), 5)
        second = await caller.post(probe_of(daemon_id), json=WIRING)

        assert second.status_code == 409
        detail = second.json()["detail"]
        assert str(daemon_id) in detail
        assert "already probing" in detail, "a 409 with no reason is an indistinguishable refusal"
        assert len(rack.questions("probe")) == 1, "the refused probe never reached the rack"

        rack.release.set()
        assert (await first).json() == {"ok": True, "error": None}
        # And the rack is probeable again the moment the first one is answered.
        third = await caller.post(probe_of(daemon_id), json=WIRING)
        assert third.status_code == 200
        assert len(rack.questions("probe")) == 2


async def test_one_rack_probing_does_not_refuse_a_probe_on_another(tmp_path):
    """Per rack, and it is a bus that is being held. A guard keyed by nothing at
    all would make one operator's probe refuse everybody else's."""
    app = create_app(AppSettings(data_dir=tmp_path))
    async with caller_for(app) as caller:
        await sign_in(caller)
        daemon_id = await a_rack_on(caller)
        other_id = (await caller.post("/api/daemons", json={"name": "far-rack"})).json()["id"]
        rack = GatedRack(app, daemon_id)
        Rack(app, other_id)

        first = asyncio.create_task(caller.post(probe_of(daemon_id), json=WIRING))
        await asyncio.wait_for(rack.started.wait(), 5)
        elsewhere = await caller.post(probe_of(other_id), json=WIRING)

        assert elsewhere.status_code == 200
        rack.release.set()
        assert (await first).status_code == 200


async def test_detection_is_not_rate_limited_the_way_a_probe_is(tmp_path):
    """A detect lists a directory and reads the running configuration. It touches
    no bus, so two at once is two directory listings and not an interleave."""
    app = create_app(AppSettings(data_dir=tmp_path))
    async with caller_for(app) as caller:
        await sign_in(caller)
        daemon_id = await a_rack_on(caller)
        rack = GatedRack(app, daemon_id)

        first = asyncio.create_task(caller.post(detect_of(daemon_id)))
        await asyncio.wait_for(rack.started.wait(), 5)
        rack.started.clear()
        second = asyncio.create_task(caller.post(detect_of(daemon_id)))
        await asyncio.wait_for(rack.started.wait(), 5)
        rack.release.set()

        assert [answer.status_code for answer in await asyncio.gather(first, second)] == [200, 200]
        assert len(rack.questions("detect")) == 2


async def test_a_probe_whose_caller_hung_up_leaves_the_rack_probeable(tmp_path):
    """The path a `finally` is for, and the only one where a plain release after
    the wait is not the same thing.

    A browser that closes the tab mid-probe cancels the handler where it is
    parked, and a `CancelledError` walks out through the guard without the route
    ever reaching its own next line. Released only on the way past, the rack
    would be marked as probing for the life of the process -- and every later
    probe on it answered 409 for a probe nobody is running.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    async with caller_for(app) as caller:
        await sign_in(caller)
        daemon_id = await a_rack_on(caller)
        rack = GatedRack(app, daemon_id)

        hung_up = asyncio.create_task(caller.post(probe_of(daemon_id), json=WIRING))
        await asyncio.wait_for(rack.started.wait(), 5)
        hung_up.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hung_up

        rack.release.set()
        again = await caller.post(probe_of(daemon_id), json=WIRING)

        assert again.status_code == 200, "the rack was left marked as probing"


def test_a_probe_nobody_answers_leaves_the_rack_probeable(client, daemon_id, monkeypatch):
    """The slot is released on the timeout path too. Held there, one silent probe
    would refuse every later one on that rack until the server restarts."""
    monkeypatch.setattr(daemons, "PROBE_TIMEOUT", 0.0)
    rack = Rack(client.app, daemon_id, reply=silent)

    assert client.post(probe_of(daemon_id), json=WIRING).status_code == 504

    monkeypatch.undo()
    rack.reply = answering()
    assert client.post(probe_of(daemon_id), json=WIRING).status_code == 200


def test_a_probe_that_was_refused_before_it_left_leaves_the_rack_probeable(client, daemon_id):
    """An offline rack takes no slot with it. 503 then a working probe, not 409."""
    assert client.post(probe_of(daemon_id), json=WIRING).status_code == 503

    Rack(client.app, daemon_id)
    assert client.post(probe_of(daemon_id), json=WIRING).status_code == 200


# --- what the wizard may ask for --------------------------------------------


@pytest.mark.parametrize("hold_s", [5.5, 10.0, 30.0])
def test_a_probe_may_not_ask_for_a_hold_the_rack_would_silently_cut(client, daemon_id, hold_s):
    """The wire allows thirty and the daemon cuts to five without saying so.

    `ors_daemon.supervisor.PROBE_HOLD_BUDGET` is 5.0 and `ProbeResult` carries no
    field reporting the cut, so a wizard counting down ten seconds has the panel
    go dark at five -- and an operator who looks up at second six answers "no,
    nothing lit" about wiring that is fine. Refused here, where there is still
    somebody to tell.
    """
    rack = Rack(client.app, daemon_id)

    answer = client.post(probe_of(daemon_id), json={**WIRING, "hold_s": hold_s})

    assert answer.status_code == 422
    assert rack.asked == [], "nothing was asked of the rack"


def test_the_longest_hold_this_route_takes_is_the_one_the_rack_will_really_give(client, daemon_id):
    """The bound is accepted, reaches the rack unchanged, and is **the same symbol
    the daemon cuts with**.

    `ors-server` may not import `ors-daemon`, so a number written down at both
    ends could only ever be restated here -- and nothing would have failed if
    `PROBE_HOLD_BUDGET` were lowered: the daemon would cut at the new value, this
    route would still accept five, and the operator would again be told a hold
    that is not the one honoured. That is the direction the bound exists to close,
    so the constant lives in `ors_schema.link`, which both ends depend on and
    neither is.

    Identity rather than equality, deliberately: `MAX_HOLD_S = 5.0` written here
    again would satisfy `==` and would drift the next time the number moves.
    `daemon/tests/test_hardware.py` asserts the other half of the pair.
    """
    rack = Rack(client.app, daemon_id)

    assert daemons.PROBE_HOLD_BUDGET is link.PROBE_HOLD_BUDGET
    assert daemons.PROBE_TIMEOUT == link.PROBE_HOLD_BUDGET + 2 * SEND_TIMEOUT

    answer = client.post(probe_of(daemon_id), json={**WIRING, "hold_s": link.PROBE_HOLD_BUDGET})

    assert answer.status_code == 200
    assert rack.questions("probe")[0]["hold_s"] == link.PROBE_HOLD_BUDGET


def test_the_wait_on_a_detect_is_a_round_trip_and_not_the_default_park():
    """`PROBE_TIMEOUT`'s pin, for the constant beside it that had none.

    Nothing in this file can catch a wrong `DETECT_TIMEOUT` by asking a rack:
    every rack here answers from *inside* the send, so the future `Hub.request`
    hands `asyncio.wait_for` is already done and even a zero timeout returns the
    answer through its fast path. `DETECT_TIMEOUT = 0.0` passes this suite whole
    -- and in front of an operator it is a rack that is merely slow, mid-apply
    and holding its bus guard, reported as one that "did not answer". The
    wizard's first step is then permanently unusable against that rack, and the
    504 says nothing that would explain it.

    Three facts, and none of them is the literal ten. It is a *round trip* --
    `SEND_TIMEOUT` for the question and `SEND_TIMEOUT` for the answer, which is
    the shape `REQUEST_TIMEOUT` itself is built from, so shortening one leg
    shortens this with it. It is longer than one leg, which a zero or a
    five-second wait is not. And it is shorter than the wait for a probe and
    shorter again than the default: a detect lists a directory, and it must not
    park a request handler for as long as a rack holding a panel lit, let alone
    for the forty seconds `Hub.request` gives a caller that names no timeout.
    That last inequality is the whole reason the parameter exists.
    """
    assert daemons.DETECT_TIMEOUT == 2 * SEND_TIMEOUT
    assert daemons.DETECT_TIMEOUT > SEND_TIMEOUT, "a wait that cannot cover the answer's own leg"
    assert daemons.DETECT_TIMEOUT < daemons.PROBE_TIMEOUT < REQUEST_TIMEOUT


async def test_a_rack_that_answers_a_moment_late_is_not_reported_as_silent(tmp_path):
    """And the same constant, asked of the route rather than of the file.

    A rack that has not answered by the time the wait is entered is the only kind
    this timeout is ever consulted for, and every other rack in this file has
    already answered by then. This one replies some turns of the loop later --
    turns rather than seconds, because nothing here spends wall clock, and the
    distinction that matters to `asyncio.wait_for` is only whether the future was
    done when it arrived.

    A rack the operator would describe as slow is a rack mid-apply: it is holding
    its own bus guard through a repaint and comes back to the link a moment
    after. Told "did not answer" about it, the wizard sends somebody to check a
    cable on a rack that is working.
    """
    app = create_app(AppSettings(data_dir=tmp_path))
    async with caller_for(app) as caller:
        await sign_in(caller)
        daemon_id = await a_rack_on(caller)
        rack = LateRack(app, daemon_id)

        detected = await caller.post(detect_of(daemon_id))

        assert detected.status_code == 200, "a rack that answered late was called silent"
        assert [(panel["bus"], panel["cs"]) for panel in detected.json()["panels"]] == [
            (bus, cs) for bus, cs, _ in FOUND
        ]
        assert len(rack.questions("detect")) == 1


@pytest.mark.parametrize("field", ["bus", "cs", "dc", "rst", "hz", "hold_s"])
def test_a_probe_whose_wiring_is_a_flag_is_refused_before_it_reaches_the_rack(
    client, daemon_id, field
):
    """This model is upstream of `ProbeRequest`, exactly as `CommandBody` is of
    `Command`: pydantic's lax mode takes `true` as 1 here first, and 1 is a
    plausible bus, chip select, pin and clock -- so the schema's own validator
    would see an honest integer and the probe would light some other device.

    The *reason* is asserted and not only the status, which is the half a 422
    alone cannot see. `not_a_flag`'s default sentence is "a screen id is a row
    id", and these fields are not screen ids: dropping the `what=` this model
    passes leaves a refusal that is still correct, still a 422, and carries a
    false reason -- the exact defect the parameter was added to prevent. The
    schema's own twins are asserted the same way in
    `packages/ors-schema/tests/test_link.py`, and for the same reason: the
    sentence is the whole of what the operator reads out of a 422, and a `hold_s`
    refused as a chip select sends them looking at their wiring.
    """
    rack = Rack(client.app, daemon_id)

    answer = client.post(probe_of(daemon_id), json={**WIRING, field: True})

    assert answer.status_code == 422
    assert rack.asked == [], "nothing was asked of the rack"
    refused = json.dumps(answer.json()["detail"])
    named = A_DURATION if field == "hold_s" else A_NUMBER
    other = A_NUMBER if field == "hold_s" else A_DURATION
    assert named in refused
    assert other not in refused, "the reason has to name the kind of field this one is"
    assert "screen id" not in refused, "and these are not screen ids -- `what=` says so"


def test_a_probe_that_names_a_field_nobody_defined_is_refused(client, daemon_id):
    """`extra="forbid"`, so a wizard sending `pattern` or `spi_bus` is told,
    rather than having it quietly ignored and the probe run on something else."""
    Rack(client.app, daemon_id)

    answer = client.post(probe_of(daemon_id), json={**WIRING, "pattern": "stripes"})

    assert answer.status_code == 422


def test_each_question_carries_an_id_no_other_rack_could_answer(client, daemon_id):
    """`deliver_reply` refuses an id that came back from the wrong rack, which
    makes a *collision across* racks harmless -- but `request`'s cleanup removes
    by identity to survive a reuse, so ids must still be unique within one rack.
    A per-rack counter is the shape that fails both halves at once."""
    other_id = client.post("/api/daemons", json={"name": "far-rack"}).json()["id"]
    rack = Rack(client.app, daemon_id)
    other = Rack(client.app, other_id)

    client.post(detect_of(daemon_id))
    client.post(probe_of(daemon_id), json=WIRING)
    client.post(detect_of(other_id))
    client.post(probe_of(other_id), json=WIRING)

    ids = [asked["request_id"] for asked in rack.asked + other.asked]
    assert len(ids) == 4
    assert len(set(ids)) == 4, "a per-rack counter answers one rack's question from another"
    assert all(1 <= len(one) <= MAX_REQUEST_ID for one in ids)


# --- the guard, and the rules the sweep in test_api_routes.py declares -------


def test_both_routes_refuse_an_unauthenticated_caller(tmp_path):
    """Named one by one as well as swept, and the rack is watched: a guard that
    ran after the handler would answer 401 having already held the bus."""
    anonymous = TestClient(create_app(AppSettings(data_dir=tmp_path)))
    rack = Rack(anonymous.app, 3)

    assert anonymous.post(detect_of(3)).status_code == 401
    assert anonymous.post(probe_of(3), json=WIRING).status_code == 401
    assert rack.asked == []


def test_both_routes_are_async_def(tmp_path):
    """`Hub` is event-loop-affine and FastAPI runs a `def` route in a threadpool,
    where `_pending` would be mutated from another thread while the daemon
    socket's reader iterates it -- a crossed answer rather than a dropped frame."""
    app = create_app(AppSettings(data_dir=tmp_path))

    for path in (DETECT_PATH, PROBE_PATH):
        assert inspect.iscoroutinefunction(endpoint(app, path)), path


def test_neither_route_opens_a_change(tmp_path):
    """They mutate nothing, which is what `MUTATES_NOTHING` in
    `test_api_routes.py` declares -- and an exemption from opening a `change` is
    held to writing nothing at all rather than being a licence.

    `push_now` is asserted on first, because a check that finds no `change`
    anywhere proves the routes are clean and the check is dead in exactly the
    same way.
    """
    app = create_app(AppSettings(data_dir=tmp_path))

    assert opens_a_change(daemons.push_now) is True, "the check is looking for the right thing"
    for path in (DETECT_PATH, PROBE_PATH):
        assert opens_a_change(endpoint(app, path)) is False, path
        assert writes(endpoint(app, path)) is False, path


def test_neither_question_bumps_a_version_or_pushes_a_snapshot(client, daemon_id):
    """The same rule as the sweep's, read off the effect rather than the source.

    Asking a rack what hardware it has changes no row, so bumping would mean
    pressing Detect tore down and repainted every panel on the rack -- which is
    the failure `send_command` refuses to be, one button along.
    """
    rack = Rack(client.app, daemon_id)
    before = version_of(client, daemon_id)
    with closing(client.app.state.database.connect()) as connection:
        events = connection.execute("SELECT COUNT(*) FROM daemon_event").fetchone()[0]

    client.post(detect_of(daemon_id))
    client.post(probe_of(daemon_id), json=WIRING)

    assert version_of(client, daemon_id) == before
    assert [asked["type"] for asked in rack.asked] == ["detect", "probe"], "a push went out"
    with closing(client.app.state.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daemon_event").fetchone()[0] == events


def version_of(client: TestClient, daemon_id: int) -> int:
    listed = client.get("/api/daemons").json()
    return next(rack["config_version"] for rack in listed if rack["id"] == daemon_id)


def endpoint(app: FastAPI, path: str):
    """The function a path really matches. `test_auth.py` says why not `app.routes`."""
    for context in iter_route_contexts(app.routes):
        if (context.path or context.original_route.path) == path:
            return context.original_route.endpoint
    raise AssertionError(f"no route matches {path}")
