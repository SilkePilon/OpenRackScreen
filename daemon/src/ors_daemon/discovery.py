"""Finding a server to ask to join, without anybody typing a URL.

The other half of `ors_server.announce`: a rack that is neither paired nor
handed a config browses for `_openrackscreen._tcp.local.` and reports every
server that answered. Filing a claim against one of them is Task 15's job and
`ors_daemon.join`'s -- this module only listens and reports.

**Every answer is a list, including the empty one and including the one with
two entries in it.** Discovery finding more than one server is reported as a
list and paired with none of them, because a rack that picked one would pick a
different one the next time it booted and nothing anywhere would record the
choice; `--server URL` is what settles it. And `--server URL` is not a nicety
either: plenty of networks drop multicast, so the empty list is an ordinary
answer on a network with a perfectly good server on it, not a failure to
report as one.

**Nothing here raises at the caller.** A browse binds sockets and joins a
multicast group, and a freshly installed rack with its interface down is
exactly the machine most likely to be running this -- so a browse that fails is
no servers, logged, and the caller's own "no server found, pass --server"
message. A traceback out of first boot says nothing anybody can act on.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

SERVICE_TYPE = "_openrackscreen._tcp.local."
"""What the server announces itself as. `ors_server.announce.SERVICE_TYPE`.

Written out here rather than imported, because the daemon does not depend on
the server package and never will: this is a wire protocol between two
separately installed programs, and the two copies are pinned by literal in each
tree's tests so that a change to one without the other fails a test rather than
producing a rack that finds nothing and says so calmly.
"""

DEFAULT_TIMEOUT = 5.0
"""How long to listen before answering, in seconds.

Long enough for a responder on an ordinary LAN to answer twice over, short
enough that a rack whose network is simply empty is not held at a blank screen
for a noticeable time. It is the caller's to override, and the caller is a
join flow that is about to wait for a human to click approve.
"""

RESOLVE_TIMEOUT_MS = 3000
"""How long to wait for the SRV, TXT and A records of one browsed name.

Separate from the browse window because it is a different question: the browse
asks who is out there, and this asks a host that has already answered for the
details. A name that does not resolve inside it is dropped rather than reported
without an address.
"""

DEFAULT_SCHEME = "http"
"""What to dial a server over when its TXT record does not say.

`http`, because that is what this server speaks and what its announcement says
when it is ours. An announcement without the field is another implementation's,
or an older one's, and refusing to talk to it would be a worse answer than
trying the scheme every OpenRackScreen server has ever used.
"""


@dataclass(frozen=True, order=True)
class Found:
    """One server that answered, in the form a claim can be filed against."""

    host: str
    port: int
    version: str = ""
    scheme: str = DEFAULT_SCHEME

    @property
    def url(self) -> str:
        """What to dial. Bracketed when the host is IPv6, which is not optional.

        `http://2001:db8::5:8080` names no port and no host anybody can parse --
        the colons of the address and the colon of the port are the same
        character, and every URL parser reads the last one as the port.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def browse(service_type: str, timeout: float) -> list[Any]:  # pragma: no cover - opens sockets
    """Listen for `timeout` seconds and resolve everything that answered.

    The one function in this module that touches the network, and the one thing
    `discover`'s `browser_factory` replaces: everything a test has an opinion
    about -- what is browsed for, what is made of the answers, what happens when
    there are none or two -- is on the other side of this seam.
    """
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

    names: set[str] = set()

    def note(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if state_change is ServiceStateChange.Added:
            names.add(name)

    zeroconf = Zeroconf()
    browser = ServiceBrowser(zeroconf, service_type, handlers=[note])
    try:
        time.sleep(max(timeout, 0.0))
        resolved = []
        for name in sorted(names):
            info = ServiceInfo(service_type, name)
            if info.request(zeroconf, RESOLVE_TIMEOUT_MS):
                resolved.append(info)
        return resolved
    finally:
        browser.cancel()
        zeroconf.close()


def discover(
    timeout: float = DEFAULT_TIMEOUT,
    browser_factory: Callable[[str, float], Iterable[Any]] = browse,
) -> list[Found]:
    """Every OpenRackScreen server that answered, sorted, deduplicated.

    Sorted by host and port so that two racks browsing the same network print
    the same list in the same order, and so that a person reading two logs is
    comparing the same thing twice. Deduplicated on the same pair, because one
    server answering on two interfaces is one server -- what must never be
    collapsed is two *different* servers, which is the case the caller has to
    see in order to refuse to choose between them.
    """
    try:
        services = list(browser_factory(SERVICE_TYPE, timeout))
    except OSError:
        log.warning("could not browse for a server on this network", exc_info=True)
        return []

    servers: dict[tuple[str, int], Found] = {}
    for service in services:
        entry = _read(service)
        if entry is None:
            continue
        servers.setdefault((entry.host, entry.port), entry)
    return sorted(servers.values())


def _read(service: Any) -> Found | None:
    """One resolved service, as a `Found` -- or None if it is not dialable.

    Defensive about every field, because none of them are this project's to
    guarantee: anything on the LAN may answer to this service type, and an
    announcement from another implementation, or from a version that predates a
    field, is not a reason for a rack to crash on first boot. Missing an address
    or a port is the one thing that disqualifies an answer, because without
    both there is nothing to dial.
    """
    host = _address(service)
    port = getattr(service, "port", None)
    if not host or not port:
        log.debug("ignoring an announcement with no address or port: %r", service)
        return None
    text = _properties(service)
    return Found(
        host=host,
        port=int(port),
        version=text.get("version", ""),
        scheme=text.get("scheme") or DEFAULT_SCHEME,
    )


def _address(service: Any) -> str:
    """The address to dial, IPv4 first.

    IPv4 first because a link-local IPv6 address needs its scope to be dialled
    at all and the server announces IPv4 anyway; the v6 address is taken only
    when it is the only one offered, which is better than reporting nothing.
    """
    try:
        addresses = [str(address) for address in service.parsed_addresses()]
    except Exception:  # noqa: BLE001 - anything on the LAN may have answered
        log.debug("ignoring an announcement whose addresses could not be read", exc_info=True)
        return ""
    for address in addresses:
        if ":" not in address:
            return address
    return addresses[0] if addresses else ""


def _properties(service: Any) -> dict[str, str]:
    """The TXT record as text, dropping whatever will not decode.

    `properties` is bytes on both sides of every pair and nothing promises
    either is UTF-8, so a key that will not decode is skipped and a value that
    will not decode leaves its key absent -- which is the same state as a peer
    that never sent it, and that state already has an answer above.
    """
    raw = getattr(service, "properties", None) or {}
    text: dict[str, str] = {}
    for key, value in raw.items():
        name = _text(key)
        decoded = _text(value)
        if name is not None and decoded is not None:
            text[name] = decoded
    return text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode()
        except UnicodeDecodeError:
            return None
    return str(value)
