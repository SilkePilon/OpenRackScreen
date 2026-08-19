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
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from ors_server import announce
from ors_server.announce import Announcer
from ors_server.app import AppSettings, create_app


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
    it, stub = announcer(port=8080, version="0.2.0")

    it.start()

    assert stub.registered[0].decoded_properties == {
        "scheme": "http",
        "port": "8080",
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
