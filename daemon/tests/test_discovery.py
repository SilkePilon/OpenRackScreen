"""What an unpaired rack makes of what it hears on the network.

Every test stubs the browse at the one seam `discover` takes for it -- a
factory that answers with the services a real one would have resolved -- so
nothing here opens a socket or waits on a multicast response. A test that
really browsed would find whatever happened to be on the developer's LAN and
nothing at all in CI.

The service type is asserted as a **literal**, never as
`discovery.SERVICE_TYPE`: a constant compared to itself agrees with any typo,
and the symptom of this particular typo is a rack that finds nothing on a
network with a server on it and says so without an error anywhere.

**The other copy of that literal is in `server/tests/test_announce.py`**, which
pins the announcing end of the same wire constant. The two are deliberately not
shared: independently transcribed is the whole reason either one means anything,
and a common import would turn both into the constant agreeing with itself. They
name each other so a protocol change is one `grep` away from both.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from ors_daemon.discovery import Found, Heard, discover, listen_window, resolve


@dataclass
class StubService:
    """The shape `discover` reads off a resolved `zeroconf.ServiceInfo`."""

    addresses: list[str] = field(default_factory=lambda: ["192.0.2.10"])
    port: int | None = 8080
    properties: dict[bytes, bytes | None] = field(
        default_factory=lambda: {b"scheme": b"http", b"port": b"8080", b"version": b"0.2.0"}
    )
    server: str = "rack.local."

    def parsed_addresses(self) -> list[str]:
        return list(self.addresses)


def browsing(*services: Any, record: list[Any] | None = None):
    def factory(service_type: str, timeout: float) -> list[Any]:
        if record is not None:
            record.append((service_type, timeout))
        return list(services)

    return factory


def test_the_service_type_browsed_for_is_the_one_the_server_announces():
    asked: list[Any] = []

    discover(timeout=2.5, browser_factory=browsing(record=asked))

    assert asked == [("_openrackscreen._tcp.local.", 2.5)]


def test_one_server_is_one_entry_carrying_its_url_and_version():
    found = discover(timeout=0.1, browser_factory=browsing(StubService()))

    assert len(found) == 1
    assert found[0].host == "192.0.2.10"
    assert found[0].port == 8080
    assert found[0].version == "0.2.0"
    assert found[0].url == "http://192.0.2.10:8080"


def test_two_servers_are_both_reported_and_in_a_stable_order():
    """Both, and never one of them.

    A rack that picked a server would pick a different one on each boot, and
    two racks in one rack cabinet would pair with two different servers with
    nothing anywhere recording the choice. The daemon pairs with none of them
    and says so; `--server URL` is what settles it.
    """
    second = StubService(addresses=["198.51.100.4"], port=9000)
    third = StubService(addresses=["203.0.113.9"], port=8080)

    found = discover(timeout=0.1, browser_factory=browsing(third, second))

    assert [entry.url for entry in found] == [
        "http://198.51.100.4:9000",
        "http://203.0.113.9:8080",
    ]


def test_no_server_at_all_is_an_empty_list_and_not_an_error():
    """The ordinary case on a network that drops multicast. See `--server`."""
    assert discover(timeout=0.1, browser_factory=browsing()) == []


def test_a_txt_record_without_a_version_does_not_crash_the_browse():
    """Somebody else's implementation on the network is not ours to crash on.

    The entry is still usable -- it has a host and a port, which is what a claim
    is filed against -- so it is reported with an empty version rather than
    dropped.
    """
    nameless = StubService(properties={b"scheme": b"http"})

    found = discover(timeout=0.1, browser_factory=browsing(nameless))

    assert len(found) == 1
    assert found[0].version == ""
    assert found[0].url == "http://192.0.2.10:8080"


def test_a_txt_record_without_a_scheme_is_read_as_http():
    found = discover(timeout=0.1, browser_factory=browsing(StubService(properties={})))

    assert found[0].url == "http://192.0.2.10:8080"


def test_a_txt_record_naming_https_is_dialled_over_https():
    scheme = {b"scheme": b"https", b"version": b"0.2.0"}
    found = discover(timeout=0.1, browser_factory=browsing(StubService(properties=scheme)))

    assert found[0].url == "https://192.0.2.10:8080"


def test_an_undecodable_txt_record_is_read_as_far_as_it_goes():
    """`properties` is bytes on the wire and nothing promises it is UTF-8."""
    rubbish = {b"version": b"\xff\xfe", b"scheme": None}

    found = discover(timeout=0.1, browser_factory=browsing(StubService(properties=rubbish)))

    assert len(found) == 1
    assert found[0].url == "http://192.0.2.10:8080"


def test_an_ipv6_address_is_bracketed_so_the_url_can_be_dialled():
    """`http://fe80::1:8080` names a host, not a port. Brackets are not optional."""
    only_v6 = StubService(addresses=["2001:db8::5"])

    found = discover(timeout=0.1, browser_factory=browsing(only_v6))

    assert found[0].url == "http://[2001:db8::5]:8080"


def test_an_ipv4_address_is_preferred_when_a_service_publishes_both():
    both = StubService(addresses=["2001:db8::5", "192.0.2.10"])

    found = discover(timeout=0.1, browser_factory=browsing(both))

    assert found[0].host == "192.0.2.10"


def test_a_service_with_no_address_is_skipped_rather_than_reported():
    """A PTR that never resolved to an A record is nothing a rack can dial."""
    unresolved = StubService(addresses=[])

    assert discover(timeout=0.1, browser_factory=browsing(unresolved, StubService())) == [
        Found(host="192.0.2.10", port=8080, version="0.2.0")
    ]


def test_a_service_with_no_port_is_skipped_rather_than_reported():
    assert discover(timeout=0.1, browser_factory=browsing(StubService(port=None))) == []


def test_one_server_answering_twice_is_one_entry():
    """Two records for one host and port are one server, not a choice to report."""
    found = discover(timeout=0.1, browser_factory=browsing(StubService(), StubService()))

    assert len(found) == 1


# --------------------------------------------------------------------------
# Addresses that name this rack rather than a server
# --------------------------------------------------------------------------


def test_a_server_that_also_announces_loopback_is_dialled_at_its_lan_address():
    """`127.0.0.1` sorts first, and "the first IPv4" used to take it.

    An older `ors-server` announced every address its host had, in string order,
    which on any `192.168.x` or `172.x` LAN puts loopback in front. A rack that
    read the first IPv4 then dialled *itself* -- and because §6.1's flow is
    browse-then-claim, what an operator saw was a claim that failed against
    port 8080 on the Pi, rather than the "no server found, pass --server" that
    names the fix. The announcing end drops these now; this end drops them
    again, because another implementation's record is not ours to trust.
    """
    ordinary = StubService(addresses=["127.0.0.1", "192.0.2.10"])

    found = discover(timeout=0.1, browser_factory=browsing(ordinary))

    assert [entry.url for entry in found] == ["http://192.0.2.10:8080"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.0.1.1",
        "::1",
        "169.254.11.2",
        "fe80::1",
        "not-an-address",
    ],
)
def test_a_server_reachable_only_at_one_of_these_is_no_server_this_rack_found(address):
    """Loopback, link-local, and whatever else turns up in a `parsed_addresses`.

    An empty list is the right answer to all of them, and it is a *better* answer
    than one entry: the caller's "no server found, pass --server" is a sentence
    somebody can act on, and two unreachable entries would trip "more than one
    server, pair with none" on a LAN that has exactly one.
    """
    assert discover(timeout=0.1, browser_factory=browsing(StubService(addresses=[address]))) == []


def test_a_link_local_address_does_not_win_over_a_routable_one():
    """`169.254.x` means DHCP did not answer; a rack that got a lease cannot reach it."""
    partly = StubService(addresses=["169.254.11.2", "198.51.100.4"])

    found = discover(timeout=0.1, browser_factory=browsing(partly))

    assert found[0].host == "198.51.100.4"


def test_two_servers_that_both_announce_loopback_do_not_become_one_refusal():
    """The failure this whole filter is for, in the shape it actually arrives in.

    Two bridged containers on one LAN both announcing `127.0.0.1` used to be two
    entries with the same host and port -- deduplicated to one, which is a rack
    pairing with itself, or (with different ports) two, which is the "more than
    one server" refusal on a network with one real server. Both are dropped, and
    the real one is what is left.
    """
    bridged = StubService(addresses=["127.0.0.1"], port=8080)
    also_bridged = StubService(addresses=["127.0.0.1"], port=9000)
    real = StubService(addresses=["192.0.2.10"])

    found = discover(timeout=0.1, browser_factory=browsing(bridged, also_bridged, real))

    assert [entry.url for entry in found] == ["http://192.0.2.10:8080"]


def test_a_browse_that_fails_is_no_servers_rather_than_a_traceback():
    """`Zeroconf()` binds sockets, and a rack with its interface down is a rack.

    The caller's next line says "no server found, pass --server"; a traceback
    out of a freshly installed daemon says nothing anybody can act on.
    """

    def exploding(service_type: str, timeout: float) -> list[Any]:
        raise OSError("no route to 224.0.0.251")

    assert discover(timeout=0.1, browser_factory=exploding) == []


def test_a_browse_that_fails_with_zeroconfs_own_error_is_no_servers_either():
    """`except OSError` did not keep this module's promise, and this is why.

    zeroconf's own failures -- `NotRunningException`, `EventLoopBlocked`,
    `NonUniqueNameException`, `IncomingDecodeError` -- are `zeroconf.Error`,
    which is an `Exception` and not an `OSError`. Catching only the sockets'
    errors meant the browser's own errors came out of a first-boot join flow as
    a traceback, out of the one module whose docstring says nothing here raises
    at the caller.
    """

    class NotRunningException(Exception):
        """Shaped like zeroconf's: an `Exception`, and nothing to do with `OSError`."""

    def exploding(service_type: str, timeout: float) -> list[Any]:
        raise NotRunningException("the responder's event loop is not running")

    assert discover(timeout=0.1, browser_factory=exploding) == []


def test_zeroconfs_errors_are_still_not_oserrors():
    """The premise of the test above, read off the installed zeroconf rather than
    remembered. The day this fails, narrowing the catch is back on the table."""
    import zeroconf

    assert issubclass(zeroconf.Error, Exception)
    assert not issubclass(zeroconf.Error, OSError)
    for name in ("NotRunningException", "EventLoopBlocked", "NonUniqueNameException"):
        assert issubclass(getattr(zeroconf, name), zeroconf.Error), name


def test_a_caller_that_names_no_window_waits_the_documented_default():
    """`DEFAULT_TIMEOUT` is what the join flow gets, and every other test passes
    a timeout explicitly -- so without this one the number is pinned by nothing
    and can be edited to any value at all.

    Five seconds by literal: long enough for a responder on an ordinary LAN to
    answer twice, short enough that a rack whose network is simply empty is not
    held at a blank screen.
    """
    asked: list[Any] = []

    discover(browser_factory=browsing(record=asked))

    assert asked == [("_openrackscreen._tcp.local.", 5.0)]


# --------------------------------------------------------------------------
# The parts of `browse` that are not the network
# --------------------------------------------------------------------------

# Stand-ins for `zeroconf.ServiceStateChange`, compared by identity exactly as
# the real members are. Objects rather than strings so that the identity check
# in `Heard.note` is tested as an identity check and not as an accident of
# CPython interning short literals.
ADDED = object()
REMOVED = object()
UPDATED = object()

TYPE = "_openrackscreen._tcp.local."


def test_only_a_service_that_arrived_is_a_service_to_ask_about():
    """`Removed` and `Updated` are dropped, and that is a decision, not an oversight.

    What this browse answers is who is out there now. A name that announced
    itself and withdrew inside one five-second window is not something to hand a
    join flow, and an `Updated` for a name already heard adds nothing.
    """
    heard = Heard(added=ADDED)

    heard.note(zeroconf=None, service_type=TYPE, name=f"gone.{TYPE}", state_change=REMOVED)
    heard.note(zeroconf=None, service_type=TYPE, name=f"moved.{TYPE}", state_change=UPDATED)
    heard.note(zeroconf=None, service_type=TYPE, name=f"here.{TYPE}", state_change=ADDED)

    assert heard.names() == [f"here.{TYPE}"]


def test_the_names_heard_are_answered_in_a_stable_order():
    """Two racks browsing one network resolve the same names in the same order.

    Which matters for what it does to the *timeout*: names are resolved one at a
    time, so the order decides which server a rack has already found when a slow
    one runs the window out.
    """
    heard = Heard(added=ADDED)
    for name in (f"rack-c.{TYPE}", f"rack-a.{TYPE}", f"rack-b.{TYPE}", f"rack-a.{TYPE}"):
        heard.note(zeroconf=None, service_type=TYPE, name=name, state_change=ADDED)

    assert heard.names() == [f"rack-a.{TYPE}", f"rack-b.{TYPE}", f"rack-c.{TYPE}"]


def test_the_callback_takes_the_keywords_zeroconf_calls_it_with():
    """`ServiceBrowser` fires handlers with keywords, so these four names are wire.

    Every call above is keyword-only for this reason, and this test says so out
    loud: renaming a parameter of `Heard.note` is a `TypeError` raised on
    zeroconf's own thread, during a real browse, on a real network -- which is
    the one place nothing in this repository runs.
    """
    heard = Heard(added=ADDED)

    heard.note(zeroconf=object(), service_type=TYPE, name=f"rack.{TYPE}", state_change=ADDED)

    assert heard.names() == [f"rack.{TYPE}"]


class SetThatAnswersMidIteration(set):
    """A set that gives the browser thread its turn in the middle of a copy.

    The race, made to happen on demand. Left to real timing it is unreproducible
    on a GIL build -- `sorted()` over a set of strings is one uninterrupted C
    loop -- and perfectly ordinary on a free-threaded one, which is the worst
    possible combination: a defect that cannot be written a failing test for
    today and starts biting the day somebody runs the daemon on 3.14t. So the
    window is opened explicitly, at the one place it exists: iterating the set.

    `interrupt` is called once, after the first element, and is expected to
    *fail* to complete against correct code -- it is another thread reaching for
    the same names, and the lock is what holds it off until the copy is done.
    """

    def __init__(self, names: Any, interrupt: Any) -> None:
        super().__init__(names)
        self.interrupt = interrupt

    def __iter__(self) -> Any:
        iterator = super().__iter__()
        for index, name in enumerate(iterator):
            if index == 0:
                self.interrupt()
            yield name


def test_a_name_that_arrives_while_the_list_is_taken_does_not_break_the_browse():
    """zeroconf answers on its thread while `browse` reads the names on the caller's.

    Without the lock this is `RuntimeError: Set changed size during iteration`,
    raised in a first-boot join flow, and only when a second server answers
    late -- which is exactly the case discovery exists to report. The thread
    started mid-copy here is that second server: against correct code it is
    still parked on the lock when the copy finishes, and it gets its name in
    immediately afterwards.
    """
    heard = Heard(added=ADDED)
    answering = []

    def a_second_server_answers() -> None:
        thread = threading.Thread(
            target=heard.note,
            kwargs={
                "zeroconf": None,
                "service_type": TYPE,
                "name": f"late.{TYPE}",
                "state_change": ADDED,
            },
        )
        answering.append(thread)
        thread.start()
        thread.join(timeout=0.2)

    heard._names = SetThatAnswersMidIteration(
        {f"rack-{index}.{TYPE}" for index in range(8)}, a_second_server_answers
    )

    taken = heard.names()

    assert answering and answering[0].is_alive(), (
        "the second server got its name in during the copy, so the copy was not"
        " holding the lock -- on a free-threaded build that is the RuntimeError"
    )
    answering[0].join(timeout=5)
    assert f"late.{TYPE}" not in taken, "a copy is a copy; it cannot grow after being taken"
    assert f"late.{TYPE}" in heard.names(), "the late answer was lost rather than delayed"


def test_a_browse_window_that_has_already_passed_is_not_a_negative_sleep():
    """`time.sleep` raises `ValueError` on a negative, and a caller computing a
    window from a deadline that is already gone means "do not wait"."""
    assert listen_window(2.5) == 2.5
    assert listen_window(0.0) == 0.0
    assert listen_window(-1.0) == 0.0


def test_each_name_is_asked_about_once_within_the_resolve_timeout():
    """The literal `3000`, which nothing else in this tree spends.

    Written out rather than compared to `RESOLVE_TIMEOUT_MS`, for the same
    reason the service type is: a constant agrees with itself under any edit,
    and this one is a wait a rack sits through per name it heard.
    """
    asked: list[Any] = []

    def requester(zeroconf: Any, service_type: str, name: str, timeout_ms: int) -> Any:
        asked.append((service_type, name, timeout_ms))
        return StubService()

    resolve("a-responder", TYPE, [f"b.{TYPE}", f"a.{TYPE}"], requester=requester)

    assert asked == [(TYPE, f"b.{TYPE}", 3000), (TYPE, f"a.{TYPE}", 3000)]


def test_a_name_that_does_not_resolve_is_dropped_rather_than_reported():
    """A PTR with no SRV behind it is a name, not a server: there is nothing to dial."""
    answered = StubService()

    def requester(zeroconf: Any, service_type: str, name: str, timeout_ms: int) -> Any:
        return None if name.startswith("ghost") else answered

    resolved = resolve(None, TYPE, [f"ghost.{TYPE}", f"real.{TYPE}"], requester=requester)

    assert resolved == [answered]


def test_the_resolver_is_handed_the_responder_that_did_the_browsing():
    """One `Zeroconf`, not a second one: a second would bind its own sockets."""
    seen: list[Any] = []
    responder = object()

    def requester(zeroconf: Any, service_type: str, name: str, timeout_ms: int) -> Any:
        seen.append(zeroconf)
        return StubService()

    resolve(responder, TYPE, [f"rack.{TYPE}"], requester=requester)

    assert seen == [responder]
