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

import ipaddress
import logging
import threading
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


class Heard:
    """The service names one browse has heard, across the two threads that touch them.

    `zeroconf` answers on a thread of its own while `browse` waits on the
    caller's, so this set is written from one and read from the other. A bare
    set is not enough for that: a copy taken while the browser is still adding
    to it is `RuntimeError: Set changed size during iteration`, raised on the
    caller's thread, out of a first-boot join flow, and only when a second
    server answers late -- which is the moment this feature exists for. `names`
    takes a sorted copy under the lock, and the caller never sees the set.
    """

    def __init__(self, added: Any) -> None:
        self.added = added
        self._lock = threading.Lock()
        self._names: set[str] = set()

    def note(self, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        """`zeroconf`'s callback. **These four parameter names are a contract.**

        `ServiceBrowser` fires its handlers with keywords -- `zeroconf=`,
        `service_type=`, `name=`, `state_change=` -- so renaming any of them is
        a `TypeError` raised on zeroconf's own thread, during a real browse on a
        real network, which is the one place nothing in this repository runs.

        `Added` only. `Removed` and `Updated` are dropped, because the answer
        this browse gives is who is out there now, and a name that arrived and
        withdrew inside one five-second window is not one to hand a join flow.
        """
        if state_change is self.added:
            with self._lock:
                self._names.add(name)

    def names(self) -> list[str]:
        """Everything heard so far, sorted, as a list nothing else is mutating.

        Sorted here rather than at the caller so that the copy and the order are
        one operation, taken under one lock.
        """
        with self._lock:
            return sorted(self._names)


def listen_window(timeout: float) -> float:
    """The browse window in seconds, never negative.

    `time.sleep` raises `ValueError` on a negative number, and a caller that
    computed its window from a deadline already passed is asking for "do not
    wait", not for a traceback.
    """
    return max(timeout, 0.0)


def request(  # pragma: no cover - opens sockets
    zeroconf: Any, service_type: str, name: str, timeout_ms: int
) -> Any | None:
    """One browsed name's SRV, TXT and A records, or None if it did not answer.

    The second of this module's seams, and the reason the first can be thin:
    with this injectable, everything `browse` decides -- which names it asks
    about, in what order, and what becomes of one that does not resolve -- is
    tested without a socket.
    """
    from zeroconf import ServiceInfo

    info = ServiceInfo(service_type, name)
    return info if info.request(zeroconf, timeout_ms) else None


def resolve(
    zeroconf: Any,
    service_type: str,
    names: Iterable[str],
    requester: Callable[[Any, str, str, int], Any | None] = request,
) -> list[Any]:
    """Each browsed name, resolved; the ones that did not answer are dropped.

    Dropped rather than reported without an address, because a name with no SRV
    behind it is nothing a claim can be filed against. `RESOLVE_TIMEOUT_MS` is
    spent per name, and the responder that did the browsing is reused rather
    than a second one built -- a second `Zeroconf` binds its own sockets.
    """
    resolved = []
    for name in names:
        info = requester(zeroconf, service_type, name, RESOLVE_TIMEOUT_MS)
        if info is not None:
            resolved.append(info)
    return resolved


def browse(service_type: str, timeout: float) -> list[Any]:  # pragma: no cover - opens sockets
    """Listen for `timeout` seconds and resolve everything that answered.

    The one function in this module that touches the network, and now only
    that: a `Zeroconf`, a `ServiceBrowser`, and a sleep. Every decision it used
    to hold -- which state changes count, what order the names are asked about
    in, what happens to one that does not resolve, what a negative timeout means
    -- is in `Heard`, `listen_window` and `resolve` above, where tests reach it.
    """
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

    heard = Heard(added=ServiceStateChange.Added)
    zeroconf = Zeroconf()
    browser = ServiceBrowser(zeroconf, service_type, handlers=[heard.note])
    try:
        time.sleep(listen_window(timeout))
        return resolve(zeroconf, service_type, heard.names())
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

    `except Exception` and not `except OSError`, measured against the installed
    zeroconf 0.150: `NotRunningException`, `EventLoopBlocked`,
    `NonUniqueNameException` and `IncomingDecodeError` all subclass
    `zeroconf.Error(Exception)`, and not one of them is an `OSError`. Catching
    only the sockets' own errors kept the letter of "a browse that fails is no
    servers" while letting the browser's own failures out of a first-boot join
    flow as a traceback -- out of the one module whose docstring promises the
    opposite.
    """
    try:
        services = list(browser_factory(SERVICE_TYPE, timeout))
    except Exception:  # noqa: BLE001 - the promise above: nothing here raises at the caller
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
    """The address to dial: IPv4 first, and never one that names this rack.

    IPv4 first because a link-local IPv6 address needs its scope to be dialled
    at all and the server announces IPv4 anyway; the v6 address is taken only
    when it is the only one offered, which is better than reporting nothing.

    **Loopback and link-local are dropped here as well as at the announcing
    end**, and the repetition is deliberate: another implementation's
    announcement -- or an older `ors-server`'s, which advertised every address
    its host had -- is not ours to trust. `127.0.0.1` sorts ahead of any
    `192.168.x` or `172.x` address, so a rack that took the first IPv4 dialled
    *itself*: a claim filed against port 8080 on the Pi, rather than the "no
    server found, pass --server" that names the fix. A service offering nothing
    else is dropped by `_read`, which is what puts the caller back on that
    message; two of them would trip "more than one server, pair with none" on a
    LAN with exactly one.
    """
    try:
        addresses = [str(address) for address in service.parsed_addresses()]
    except Exception:  # noqa: BLE001 - anything on the LAN may have answered
        log.debug("ignoring an announcement whose addresses could not be read", exc_info=True)
        return ""
    dialable = [address for address in addresses if _reaches_another_machine(address)]
    for address in dialable:
        if ":" not in address:
            return address
    return dialable[0] if dialable else ""


def _reaches_another_machine(address: str) -> bool:
    """Whether an announced address is one this rack could dial a server at.

    Anything that will not parse as an address goes too: `parsed_addresses` is
    documented to answer addresses, and a peer that puts something else there
    has given this rack nothing to dial.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        log.debug("ignoring an announced address that is not an address: %r", address)
        return False
    return not (parsed.is_loopback or parsed.is_link_local)


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
