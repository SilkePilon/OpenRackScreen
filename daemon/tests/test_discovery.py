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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ors_daemon.discovery import Found, discover


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


def test_a_browse_that_fails_is_no_servers_rather_than_a_traceback():
    """`Zeroconf()` binds sockets, and a rack with its interface down is a rack.

    The caller's next line says "no server found, pass --server"; a traceback
    out of a freshly installed daemon says nothing anybody can act on.
    """

    def exploding(service_type: str, timeout: float) -> list[Any]:
        raise OSError("no route to 224.0.0.251")

    assert discover(timeout=0.1, browser_factory=exploding) == []
