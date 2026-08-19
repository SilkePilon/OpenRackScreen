"""What the server says about itself over mDNS, and when it stops saying it.

Every test here stubs `zeroconf` at the one seam `Announcer` takes for it -- a
factory returning the object `register_service` is called on -- so nothing in
this file opens a socket or sends a multicast packet. A test that really
announced would pass on a laptop running Avahi and hang on a CI runner with no
multicast, which is the worst of both answers.

The service type is asserted as a **literal** and never as
`announce.SERVICE_TYPE`: comparing the constant to itself passes under any
typo, and a typo here is a rack that never finds anything with no error
anywhere on either end.

**The other copy of that literal is in `daemon/tests/test_discovery.py`**, and
it is deliberately not shared with this one -- two independent transcriptions of
a wire constant is what makes either pin mean anything, so importing one into
the other would delete the check rather than tidy it. They are named in each
other so that a protocol change is one `grep` from both, which is the part that
was missing.
"""

from __future__ import annotations

from typing import Any

import ifaddr
import pytest
from fastapi.testclient import TestClient
from ors_server import announce
from ors_server.announce import Announcer
from ors_server.app import AppSettings, create_app
from zeroconf import Zeroconf


class StubZeroconf:
    """A `Zeroconf` that records instead of transmitting."""

    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []
        self.closed = 0

    def register_service(self, info: Any) -> None:
        self.registered.append(info)

    def unregister_service(self, info: Any) -> None:
        self.unregistered.append(info)

    def close(self) -> None:
        self.closed += 1


def announcer(port: int = 8080, version: str = "9.9.9", **extra: Any) -> tuple[Announcer, Any]:
    stub = StubZeroconf()
    return Announcer(port=port, version=version, zeroconf_factory=lambda: stub, **extra), stub


def test_the_announced_service_type_is_the_one_the_daemon_browses_for():
    """Pinned by literal, in the one place a typo would be silent.

    Nothing raises when a server announces `_openrackscreem._tcp.local.`: it
    registers, the daemon browses for the correct name, finds nothing, and
    reports "no server found" on a network with one.
    """
    it, stub = announcer()

    it.start()

    assert len(stub.registered) == 1
    info = stub.registered[0]
    assert info.type == "_openrackscreen._tcp.local."
    assert info.name.endswith("._openrackscreen._tcp.local.")


def test_the_service_is_announced_on_the_port_the_server_listens_on():
    it, stub = announcer(port=9123)

    it.start()

    assert stub.registered[0].port == 9123


def test_the_txt_record_carries_the_scheme_the_port_and_the_version():
    """§6.1: "The TXT record carries the scheme, the port, and the server's version."

    The port is in the SRV record too, and a daemon reads it from there -- it is
    repeated here because the spec says so and because a peer that has the TXT
    without having resolved the SRV can still build a URL from it.
    """
    it, stub = announcer(port=9123, version="0.2.0")

    it.start()

    assert stub.registered[0].decoded_properties == {
        "scheme": "http",
        "port": "9123",
        "version": "0.2.0",
    }


def test_the_instance_name_is_this_hosts_own_short_name(monkeypatch):
    """So two servers on one network are two entries and not a name collision."""
    monkeypatch.setattr(announce.socket, "gethostname", lambda: "rack-seven.lan")
    it, stub = announcer()

    it.start()

    assert stub.registered[0].name == "rack-seven._openrackscreen._tcp.local."


def test_a_host_with_no_name_still_announces_under_something(monkeypatch):
    """An empty instance name is a registration `zeroconf` refuses outright."""
    monkeypatch.setattr(announce.socket, "gethostname", lambda: "")
    it, stub = announcer()

    it.start()

    assert stub.registered[0].name == "openrackscreen._openrackscreen._tcp.local."


# --------------------------------------------------------------------------
# The address in the advertisement
# --------------------------------------------------------------------------


def interfaces(*addresses: Any) -> list[ifaddr.Adapter]:
    """An `ifaddr` interface table, in `ifaddr`'s own types.

    Its own types rather than a hand-made stand-in, so that these tests read
    `is_IPv4` and `.ip` off the class production code reads them off: a fake
    with the attributes remembered here would keep passing through a rename in
    `ifaddr` that stopped the server announcing any address at all.

    IPv6 addresses are the three-tuple `(address, flowinfo, scope_id)` `ifaddr`
    reports them as, which is also how `is_IPv4` tells the two apart.
    """
    return [
        ifaddr.Adapter(f"eth{index}", f"eth{index}", [ifaddr.IP(address, 24, f"eth{index}")])
        for index, address in enumerate(addresses)
    ]


def test_the_announcement_carries_this_hosts_own_lan_address(monkeypatch):
    """An announcement with no address is a SRV record naming nothing dialable.

    The registration succeeds, the rack resolves it, and what comes back has a
    port and no host -- so `discover` drops it and the network looks empty. That
    is the failure `local_addresses` exists to prevent, and until this test it
    was pinned by nothing: both `local_addresses() -> []` and dropping
    `parsed_addresses=` entirely passed the whole suite.
    """
    monkeypatch.setattr(
        announce.ifaddr, "get_adapters", lambda: interfaces("192.168.7.5", "10.0.0.9")
    )
    it, stub = announcer()

    it.start()

    assert stub.registered[0].parsed_addresses() == ["10.0.0.9", "192.168.7.5"]


def test_the_announcement_never_tells_a_rack_to_dial_itself(monkeypatch):
    """Loopback and link-local, dropped -- and dropped *first*, not just last.

    `sorted()` over every IPv4 an ordinary host has puts `127.0.0.1` in front of
    any `192.168.x` or `172.x` address, and the daemon takes the first IPv4 of
    what it resolves. So an announcement that merely *included* loopback was one
    a rack would dial itself over: a claim filed against port 8080 on the Pi,
    instead of the "no server found, pass --server" that names the fix.

    `169.254.x` goes with it -- it exists because DHCP did not answer. The
    Docker bridge address stays, because it is a real address on a real
    interface and nothing here can tell it apart from a second LAN; that one is
    answered by the image not announcing at all (see `deploy/Dockerfile`).
    """
    monkeypatch.setattr(
        announce.ifaddr,
        "get_adapters",
        lambda: interfaces("127.0.0.1", "169.254.11.2", "172.17.0.1", "192.168.0.145"),
    )
    it, stub = announcer()

    it.start()

    assert stub.registered[0].parsed_addresses() == ["172.17.0.1", "192.168.0.145"]


def test_a_host_with_nothing_but_loopback_announces_no_address_rather_than_a_wrong_one(monkeypatch):
    """The empty netns case, and the container-with-no-bridge case.

    Nothing to announce is not an error: the registration still happens, and a
    rack that resolves it drops an entry with no address. Announcing `127.0.0.1`
    instead would give that rack something to dial, and what it would reach is
    itself.
    """
    monkeypatch.setattr(announce.ifaddr, "get_adapters", lambda: interfaces("127.0.0.1"))
    it, stub = announcer()

    it.start()

    assert announce.local_addresses() == []
    assert stub.registered[0].parsed_addresses() == []


def test_an_ipv6_only_interface_is_not_announced_as_an_a_record(monkeypatch):
    """IPv4 only, deliberately -- and `ifaddr` reports v6 as a tuple, not a string."""
    monkeypatch.setattr(
        announce.ifaddr, "get_adapters", lambda: interfaces(("2001:db8::5", 0, 0), "192.168.7.5")
    )

    assert announce.local_addresses() == ["192.168.7.5"]


def test_stopping_unregisters_the_service_it_registered():
    """The whole point of `stop`.

    A server that restarted without unregistering leaves a record on the
    network naming a port nothing is listening on, and every rack that boots
    inside its TTL dials it.
    """
    it, stub = announcer()
    it.start()

    it.stop()

    assert stub.unregistered == stub.registered
    assert stub.closed == 1


def test_stopping_without_having_started_is_not_an_error():
    """`create_app` builds one and a process can end without ever serving."""
    it, stub = announcer()

    it.stop()

    assert stub.unregistered == []
    assert stub.closed == 0


def test_stopping_twice_unregisters_once():
    it, stub = announcer()
    it.start()

    it.stop()
    it.stop()

    assert len(stub.unregistered) == 1
    assert stub.closed == 1


def test_starting_twice_registers_once():
    """Two registrations of one name is a conflict the responder has to settle."""
    it, stub = announcer()

    it.start()
    it.start()

    assert len(stub.registered) == 1


def test_the_socket_is_closed_even_when_unregistering_fails():
    """Otherwise a failed shutdown leaks the responder's threads and sockets."""
    it, stub = announcer()
    it.start()
    stub.unregister_service = _raising

    with pytest.raises(OSError):
        it.stop()

    assert stub.closed == 1


def _raising(_info: Any) -> None:
    raise OSError("the interface went away")


# --------------------------------------------------------------------------
# Wired into the app
# --------------------------------------------------------------------------


class StubAnnouncer:
    """What `create_app` builds, when a test is watching for it being built."""

    built: list[StubAnnouncer] = []

    def __init__(self, port: int, version: str) -> None:
        self.port = port
        self.version = version
        self.starts = 0
        self.stops = 0
        StubAnnouncer.built.append(self)

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


@pytest.fixture
def announcers(monkeypatch) -> list[StubAnnouncer]:
    """Every `Announcer` the app under test constructed, in order."""
    monkeypatch.setattr(StubAnnouncer, "built", [])
    monkeypatch.setattr("ors_server.app.Announcer", StubAnnouncer)
    return StubAnnouncer.built


def test_the_app_announces_while_it_is_serving_and_stops_when_it_stops(
    monkeypatch, tmp_path, announcers
):
    monkeypatch.delenv("ORS_ANNOUNCE", raising=False)
    app = create_app(AppSettings(data_dir=tmp_path, port=9123))

    assert len(announcers) == 1, "the app built no announcer at all"
    assert (announcers[0].starts, announcers[0].stops) == (0, 0), "announced before serving"
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert (announcers[0].starts, announcers[0].stops) == (1, 0)

    assert (announcers[0].starts, announcers[0].stops) == (1, 1)


def test_the_app_announces_the_port_it_was_told_to_serve_on(monkeypatch, tmp_path, announcers):
    monkeypatch.delenv("ORS_ANNOUNCE", raising=False)
    create_app(AppSettings(data_dir=tmp_path, port=9123))

    assert announcers[0].port == 9123
    assert announcers[0].version, "a TXT record with no version is one no rack can read"


def test_announcing_on_leaves_a_real_announcer_on_the_app(monkeypatch, tmp_path):
    """The other half of `app.state.announcer is None`, which was all anything asserted.

    No stub of `Announcer` anywhere in this one: what `create_app` puts on the
    app has to be the real class, wired to the real `Zeroconf`, or the server
    starts a lifespan that announces to nothing. Constructing one binds nothing
    -- the factory is called in `start`, which this test never reaches -- so
    this opens no socket either.
    """
    monkeypatch.delenv("ORS_ANNOUNCE", raising=False)

    app = create_app(AppSettings(data_dir=tmp_path, port=9123))

    assert isinstance(app.state.announcer, Announcer)
    assert app.state.announcer.port == 9123
    assert app.state.announcer.version, "a TXT record with no version is one no rack can read"
    assert app.state.announcer.zeroconf_factory is Zeroconf, (
        "the app's announcer would announce over something that is not a responder"
    )


def test_ors_announce_set_to_zero_builds_no_announcer(monkeypatch, tmp_path, announcers):
    """The switch for every network where a multicast responder is unwanted.

    Containers most of all: a bridge network makes the announcement unreachable
    and the port in it wrong, so the image is better off silent.
    """
    monkeypatch.setenv("ORS_ANNOUNCE", "0")
    app = create_app(AppSettings(data_dir=tmp_path))

    assert announcers == []
    assert app.state.announcer is None


def test_the_app_starts_and_stops_cleanly_with_announcing_disabled(monkeypatch, tmp_path):
    """No stub anywhere: the real `Announcer` class, never constructed.

    Every `TestClient` test in this directory now runs a lifespan that has to
    decide whether to announce, so the disabled path is the one the whole suite
    depends on being uneventful.
    """
    monkeypatch.setenv("ORS_ANNOUNCE", "0")

    with TestClient(create_app(AppSettings(data_dir=tmp_path))) as client:
        assert client.get("/api/health").status_code == 200


def test_announcing_is_on_unless_it_is_switched_off(monkeypatch):
    monkeypatch.delenv("ORS_ANNOUNCE", raising=False)
    assert announce.announcing_is_enabled() is True

    monkeypatch.setenv("ORS_ANNOUNCE", "1")
    assert announce.announcing_is_enabled() is True

    monkeypatch.setenv("ORS_ANNOUNCE", "0")
    assert announce.announcing_is_enabled() is False


def test_an_announcement_that_fails_does_not_stop_the_server(monkeypatch, tmp_path, caplog):
    """A rack with no route to a multicast group is still a rack that serves.

    `zeroconf` binds sockets and joins a multicast group at `start`, and both
    fail on hosts that exist -- a container on a bridge network, a machine whose
    only interface is down. The server's own job does not depend on any of it.
    """
    monkeypatch.delenv("ORS_ANNOUNCE", raising=False)

    class Exploding(StubAnnouncer):
        def start(self) -> None:
            raise OSError("no route to 224.0.0.251")

        def stop(self) -> None:
            raise OSError("nothing to unregister")

    monkeypatch.setattr(StubAnnouncer, "built", [])
    monkeypatch.setattr("ors_server.app.Announcer", Exploding)

    with caplog.at_level("WARNING"):
        with TestClient(create_app(AppSettings(data_dir=tmp_path))) as client:
            assert client.get("/api/health").status_code == 200

    assert [record for record in caplog.records if "announce" in record.getMessage()]
