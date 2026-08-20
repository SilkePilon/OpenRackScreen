"""Saying where this server is, so a freshly installed rack can find it.

The rack asks to join; nobody pastes a token. That flow needs the two ends to
find each other first, and this is that half: an mDNS service registration
under `_openrackscreen._tcp.local.`, carrying the port to dial and the version
that is answering, for as long as the server is serving and not one moment
longer.

**`SERVICE_TYPE` is the whole protocol.** It is pinned by literal here and by
literal in `daemon/tests/test_discovery.py`, in both trees, because it is the
one string whose corruption produces no error on either side: a server
announcing a misspelt type registers cleanly, a rack browsing for the correct
one waits out its timeout, and what an operator sees is "no server found" on a
network with a server on it.

**Nothing here is required for the server to work.** `zeroconf` binds sockets
and joins a multicast group, and there are ordinary hosts where that fails --
a container on a bridge network, a machine whose only interface is down, a
network where multicast is dropped. So `app.py` treats a failed announcement as
a warning, `--server URL` exists on the daemon for exactly those networks, and
`ORS_ANNOUNCE=0` turns this off for deployments that would rather be silent.

**A bridged container is the case where it fails without failing.** mDNS is a
link-layer protocol and a Docker bridge is a NAT, so the multicast never
reaches the LAN at all -- while the addresses this host has *inside* the bridge
are the bridge's and mean nothing outside it. The announcement therefore
succeeds, and nothing hears it. That is why `deploy/Dockerfile` sets
`ORS_ANNOUNCE=0` and `deploy/compose.mdns.yaml` -- host networking -- is the
supported way for a container to be discoverable. No advertised-address setting
would help: the packet does not leave the bridge whatever address is in it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from collections.abc import Callable
from typing import Any

import ifaddr
from zeroconf import ServiceInfo, Zeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_openrackscreen._tcp.local."
"""The name a rack browses for. Changing it is a protocol break, not a rename.

`_tcp` because the link it advertises is an HTTP server and a WebSocket on top
of one. The trailing dot is not decoration -- `zeroconf` compares fully
qualified names, and a type without it is refused at registration.
"""

FALLBACK_INSTANCE = "openrackscreen"
"""What this announces itself as when the host has no name to give.

An empty instance name is a registration `zeroconf` rejects outright, so a
machine with an empty `gethostname` would otherwise turn "announce yourself"
into a startup exception.
"""

SCHEME = "http"
"""What the TXT record says to dial this server over.

A constant and not a setting because this server speaks plain HTTP and TLS is
somebody's reverse proxy in front of it -- and a proxy is a different host and
port, which is a different announcement than this one. A daemon reads the field
rather than assuming it, so the day the answer is `https` the rack needs no
change.
"""

ANNOUNCE_ENV = "ORS_ANNOUNCE"
"""`ORS_ANNOUNCE=0` turns announcing off. Anything else, or nothing, leaves it on.

On by default because the milestone this belongs to exists to make a rack
pairable without anybody typing anything, and a discovery mechanism that has to
be switched on first is one more thing to have not done.

Exactly `"0"` switches it off, so that `ORS_ANNOUNCE=false` -- which is not a
value this documents anywhere -- leaves announcing on rather than quietly
disabling it. The README names the one value that works.
"""


def announcing_is_enabled(environment: Any | None = None) -> bool:
    """Whether this deployment wants an mDNS responder at all.

    Read from the environment rather than carried on `AppSettings`, which is the
    one exception in this server to that rule and is deliberate: the whole test
    suite builds apps, `create_app` builds one of these, and a *default* that
    every existing test would have to have been updated to pass is a default
    that gets passed wrongly once and multicasts out of a CI runner.
    """
    values = os.environ if environment is None else environment
    return values.get(ANNOUNCE_ENV, "1").strip() != "0"


def reaches_the_lan(address: str) -> bool:
    """Whether an address of this host is one another machine could dial.

    Loopback and link-local are not, and dropping them is the entire job here.
    `127.0.0.1` in an announcement is not merely useless: it *sorts first* among
    the addresses of any ordinary `192.168.x` or `172.x` host, the daemon takes
    the first IPv4 of what it resolves, and what the rack then dials is itself
    -- a claim filed against port 8080 on the Pi, rather than the "no server
    found, pass --server" that names the fix. Link-local goes with it: `169.254`
    exists because DHCP did not answer, and a server that only has one is not
    reachable from a rack that got a lease.

    An address that will not parse is dropped too. `ifaddr` reads the interface
    table and has no reason to hand back anything else, and an entry this cannot
    make sense of is not an entry to publish as somewhere to dial.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - not something the interface table produces
        return False
    return not (parsed.is_loopback or parsed.is_link_local)


def local_addresses() -> list[str]:
    """Every IPv4 address of this host that a rack on the LAN could dial.

    Without them the registration publishes a SRV record naming a host nothing
    answers for, and a rack that resolves the service gets an entry it cannot
    dial -- so this is not decoration, it is the address in the advertisement.
    `test_the_announcement_carries_this_hosts_own_lan_address` is what fails if
    this ever answers with nothing on a host that has an address.

    `ifaddr` and not `zeroconf.get_all_addresses`, which is the same call and is
    deprecated in 0.150 with a removal notice saying to use `ifaddr` directly;
    the wrapper would have made a future zeroconf an ImportError at server
    start. It reads the interface table and opens no socket, which is why every
    test in this file can call it.

    IPv4 only, deliberately: the daemon dials one URL, a link-local IPv6
    address needs its scope to be dialled at all, and a rack and its server on
    one LAN have IPv4 between them or they have nothing. And never loopback or
    link-local -- see `reaches_the_lan`, where the whole argument is.
    """
    try:
        adapters = ifaddr.get_adapters()
    except OSError:  # pragma: no cover - an interface table that cannot be read
        log.warning("could not enumerate this host's addresses to announce", exc_info=True)
        return []
    return sorted(
        {
            address.ip
            for adapter in adapters
            for address in adapter.ips
            if address.is_IPv4 and reaches_the_lan(address.ip)
        }
    )


def instance_name() -> str:
    """This host's short name, as the instance half of the service name.

    The first label only: a machine whose `gethostname` answers an FQDN would
    otherwise put a dot inside the instance label, which is a different name
    than intended once `zeroconf` escapes it.
    """
    return socket.gethostname().split(".")[0].strip() or FALLBACK_INSTANCE


class Announcer:
    """One mDNS registration, for as long as `start` and `stop` bracket.

    `zeroconf_factory` is the one seam the tests take, and there is exactly one
    so that there is exactly one thing to stub: it is called at `start`, never
    at construction, so building an `Announcer` that never serves -- which is
    what `create_app` does in every test in this repository -- binds no socket
    and sends no packet.
    """

    def __init__(
        self,
        port: int,
        version: str,
        zeroconf_factory: Callable[[], Any] = Zeroconf,
        *,
        instance: str | None = None,
    ) -> None:
        self.port = port
        self.version = version
        self.zeroconf_factory = zeroconf_factory
        self.instance = instance
        self._zeroconf: Any | None = None
        self._info: ServiceInfo | None = None

    def service_info(self) -> ServiceInfo:
        """The record this publishes: where to dial, and what is answering.

        The TXT carries the scheme, the port and the version, which is what the
        design says it carries. The port is in the SRV record as well and a
        daemon reads it from there -- it is repeated because a peer holding the
        TXT alone can still build a URL, and because a field the design names is
        a field somebody will read.
        """
        instance = self.instance or instance_name()
        return ServiceInfo(
            SERVICE_TYPE,
            f"{instance}.{SERVICE_TYPE}",
            port=self.port,
            properties={
                "scheme": SCHEME,
                "port": str(self.port),
                "version": self.version,
            },
            parsed_addresses=local_addresses(),
        )

    def start(self) -> None:
        """Register, unless this is already registered.

        Idempotent because the alternative is two registrations of one name on
        one network, which is a conflict the responder settles by renaming one
        of them -- and the rename lands on whichever this process registered
        second, so a rack sees two servers where there is one.
        """
        if self._zeroconf is not None:
            return
        zeroconf = self.zeroconf_factory()
        info = self.service_info()
        zeroconf.register_service(info)
        self._zeroconf, self._info = zeroconf, info
        log.info("announcing %s on port %s", info.name, self.port)

    def stop(self) -> None:
        """Unregister and close. Both, and in that order.

        Unregistering is what keeps a restart from leaving a record on the
        network naming a port nothing is listening on -- every rack that boots
        inside its TTL dials it and reports a server that is not there. Closing
        is in a `finally` because a responder left open holds threads and
        sockets for the life of the process, which is exactly as long as a
        failed shutdown is going to be looked at.
        """
        zeroconf, info = self._zeroconf, self._info
        if zeroconf is None:
            return
        self._zeroconf, self._info = None, None
        try:
            zeroconf.unregister_service(info)
        finally:
            zeroconf.close()
