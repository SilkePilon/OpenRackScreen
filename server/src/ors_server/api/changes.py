"""What every mutation in this API does besides writing its row.

Three things have to happen on every edit, in this order, and the whole point of
this module is that no route has to remember any of them:

1. the affected daemons' generation counters are advanced,
2. a fresh snapshot is assembled *before the write is committed*, so a
   configuration no daemon could run is refused rather than saved, and
3. the snapshot is pushed to whichever of those racks is connected.

A rule that must be remembered at eleven call sites will be broken at the
twelfth, and the way it breaks is silent: the row saves, the request answers
200, and the rack goes on showing what it showed before. So the rule is not
written down for routes to follow -- it is the context manager a route uses in
order to write.

**Nothing in the code enforces that, and it is worth being exact about why.**
`Change.connection` is not the only writable connection a router holds: every
one of them opens `request.app.state.database.connect()` to read, that
connection is writable, and a route could commit through it with no version, no
snapshot and no push. SQLite has no read-only handle here to hand out instead.
What enforces the rule is `test_api_routes.unmanaged_mutations`, which parses
every mutating route -- and everything it hangs off `Depends(...)` -- and fails
the build for one that writes outside a `change`. Any claim stronger than that
would be a claim the next router could quietly falsify.

**The affected set defaults to every daemon.** A route that forgets to say what
it touched therefore pushes too much, which costs an unnecessary repaint of an
unrelated rack; the other default costs an edit nobody ever sees. Both are
wrong, one is visible, and the routes that do declare are pinned by tests that
assert a rack nobody edited was left alone.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response
from ors_schema.link import ConfigPush
from starlette.datastructures import State

from ors_server.snapshot import (
    SnapshotError,
    build_snapshot,
    build_snapshot_on,
    bump_config_version_on,
)

log = logging.getLogger(__name__)

MAX_EVENTS_PER_DAEMON = 200
"""How much of one rack's history the status panel keeps.

A ring buffer rather than a table, because the alternative is one row per action
forever on a rack that reconnects all day -- in a database whose whole design is
"one file you can re-enter by hand". Two hundred is a few days of a rack that is
behaving and about ten minutes of one that is not, which is the ratio that
matters: the interesting history is always the recent history.

Per daemon and not overall, so a rack in a reconnect loop cannot push every
other rack's history out of the window while it does it.
"""

SAVED_BUT_NOT_PUSHED = 202
"""The answer when an edit landed and **not one** of the racks it names got it.

Not 200, which would say the change is on the glass, and not an error, which
would say it was refused -- it is neither. The only case that reaches it is a
configuration that was *already* unservable before this edit (see `_assemble`),
so the honest statement is "accepted, not applied", which is what 202 is for.

Per edit and not per server, which it was not: the status was set if *any*
affected rack was unservable, and the rack-wide routes -- `PATCH /api/settings`
and every template edit -- affect every rack there is and so never call
`affects`. One rack with a hand-edited column therefore turned every rack-wide
edit into a 202 forever, while each of them went on pushing successfully to
every healthy rack. At that point 202 stops meaning "your edit was not applied"
and starts meaning "something somewhere is wrong", which is not something a
client can act on.

It is also only ever narrowed from the *default*. A route that declares its own
status keeps it: `201 Created` is a stronger and still-true claim -- the row
exists and its representation is in the body, which is what the interface routes
on -- and overwriting it would lose that to say something `UNSERVABLE_HEADER`
already says more precisely.
"""

UNSERVABLE_HEADER = "X-Unservable-Daemons"
"""Which racks an edit could not be given to, on every response that has any.

The status code cannot carry this. A 202 body is byte-identical to a 200 body
and names no rack, so a client reading one had to re-list every daemon and diff
`config_error` to find out which; and on a 201, or on an edit that reached three
racks out of four, there is no status code left to say it with at all.

Comma-separated daemon ids, ascending, and absent when every affected rack was
reached. The reason per rack stays in `config_error` on `GET /api/daemons`: it
is a sentence, it changes without any edit happening, and a header is not where
a paragraph belongs.
"""


def write_event(
    connection: sqlite3.Connection, daemon_id: int | None, level: str, kind: str, message: str
) -> None:
    """Append one line of a rack's history and prune the ring, on this connection.

    A function rather than a method, because there are two callers with nothing
    else in common and only one of them is an edit. `Change.record` writes on the
    transaction it is rolling back or committing; `ors_server.link.ws_daemon`
    writes on a connection of its own, for things that happen to a rack rather
    than to a row -- a connect, a disconnect, a snapshot the rack refused. None
    of those is a mutation, none of them may be rolled back by one, and all of
    them belong in the same list a person reads.

    The ring is what makes it one function rather than two similar ones. It is
    the same bound (`MAX_EVENTS_PER_DAEMON`), per rack, and a second copy of it
    is the copy that is forgotten -- a rack in a reconnect loop writes two of
    these a lap, which is exactly the traffic the ring exists for.

    `daemon_id` may be None for an event about a rack that has just been
    deleted. Nothing is pruned in that case: the rows share no rack, so there is
    no per-rack window to hold them to.
    """
    connection.execute(
        "INSERT INTO daemon_event (daemon_id, at, level, kind, message) VALUES (?, ?, ?, ?, ?)",
        (daemon_id, datetime.now(UTC).isoformat(), level, kind, message),
    )
    if daemon_id is None:
        return
    # The oldest go, and "oldest" is by row id rather than by `at`: the
    # timestamp is a string a clock change can move backwards, and the id is
    # the order they were really written in.
    connection.execute(
        "DELETE FROM daemon_event WHERE daemon_id = ? AND id NOT IN"
        " (SELECT id FROM daemon_event WHERE daemon_id = ? ORDER BY id DESC LIMIT ?)",
        (daemon_id, daemon_id, MAX_EVENTS_PER_DAEMON),
    )


@dataclass
class Change:
    """One edit in flight: a connection to write on, and who has to hear about it."""

    connection: sqlite3.Connection
    state: State
    _daemons: set[int] | None = None
    """The racks this edit changes, or None for "every one of them".

    None is the *default*, not an absence -- see the module docstring. `affects`
    is what narrows it, and once anything has narrowed it the set is exactly
    what was declared.
    """
    versions: dict[int, int] = field(default_factory=dict)
    """What each affected rack's counter was advanced to, filled in on the way out.

    Read by the route after its `async with` block, which is the only place the
    number is knowable: it is minted at the end of the transaction, and reading
    the column beforehand and adding one is an assumption about how many times
    the bump runs.
    """
    unservable: dict[int, str] = field(default_factory=dict)
    """Racks that were already unable to be given a configuration, and why.

    Filled in on the way out, so a route reads it after its `async with` block.
    Nothing here is a reason to refuse the edit; it is what makes the response
    honest about what did not happen.
    """
    delivered: dict[int, bool] = field(default_factory=dict)
    """Whether each pushed snapshot really left the server, per rack.

    Not "is there a socket": a rack with no servable configuration has no
    snapshot to send at all, so nothing is attempted and this holds no entry for
    it, and a send that timed out against a wedged Pi is dropped by the hub and
    holds False. Both of those are open sockets. `POST /api/daemons/{id}/push`
    is the one button that exists to dig a blank rack out of a stuck state, and
    it answered `delivered: true` with zero messages on the wire for the first of
    them -- so the question it asks has to be about the send.

    Filled in on the way out, like `versions`, and for the same reason: the
    pushes happen after the commit, which is after the route's block has run.
    """

    def affects(self, *daemon_ids: int) -> None:
        """Narrow this edit to the racks it really changes."""
        if self._daemons is None:
            self._daemons = set()
        self._daemons.update(daemon_ids)

    def affects_nobody(self) -> None:
        """For the edits with no rack to tell: creating one, and deleting one.

        Spelled out rather than left as `affects()` with no arguments, because
        "nothing to push" and "I forgot to say" must not look the same at the
        call site -- and the second of those is what the every-daemon default is
        there to catch.
        """
        if self._daemons is None:
            self._daemons = set()

    def record(self, daemon_id: int | None, level: str, kind: str, message: str) -> None:
        """Write one line of a rack's history, on this edit's transaction.

        On the transaction deliberately: an event describing an edit that was
        then refused is a history of something that never happened.
        """
        write_event(self.connection, daemon_id, level, kind, message)

    def daemons(self) -> list[int]:
        if self._daemons is not None:
            return sorted(self._daemons)
        return [
            int(row["id"]) for row in self.connection.execute("SELECT id FROM daemon ORDER BY id")
        ]


@asynccontextmanager
async def change(request: Request, response: Response) -> AsyncIterator[Change]:
    """A transaction that bumps and pushes on the way out, or writes nothing.

    `response` is a required argument and not a convenience: it is how a route
    that saved an edit nobody could be given still answers honestly, and taking
    it here means a route cannot use this machinery without being in a position
    to say so.

    The push is **awaited inside the handler**, deliberately, although the row is
    committed and the version minted before it is attempted, so the request does
    not need its result. Three reasons, in order of weight. There is no retry
    path in this milestone -- a push that is dropped is repaired only by the
    daemon reconnecting -- so a fire-and-forget send is a silently lost edit,
    which is the precise failure the "push configuration now" button exists to
    dig a rack out of. Two edits landing together would push in whatever order
    their tasks were scheduled, and the daemon applies what it is handed: the
    older snapshot arriving last leaves the rack running it, under a version
    that says otherwise. And it is already bounded -- `Hub.push_config` returns
    inside `SEND_TIMEOUT` against a wedged daemon and immediately against an
    offline one -- so what awaiting costs is five seconds in the worst case and
    a millisecond in every other. When there is a retry queue to hand a push to,
    this is the line to revisit.

    **Nothing inside the `async with` may await.** Every route in this package
    is `async def` and runs on the one event loop, so a body that awaited while
    holding the write lock would let another request's `BEGIN IMMEDIATE` run and
    block the loop itself on SQLite's busy timeout -- five seconds of every open
    socket, over one edit. The push is the first await here, and it is after the
    commit, deliberately.
    """
    state = request.app.state
    connection = state.database.connect()
    try:
        # IMMEDIATE rather than a deferred BEGIN: this transaction is going to
        # write, and taking the write lock at the start is what stops two edits
        # discovering each other halfway through as an upgrade that cannot be
        # granted.
        connection.execute("BEGIN IMMEDIATE")
        edit = Change(connection=connection, state=state)
        try:
            yield edit
            pushes = _assemble(edit)
        except BaseException:
            # Everything, including the 422 `_assemble` raises and the 404 a
            # route raises after writing nothing. A cancel lands here too, and a
            # half-applied edit is the one state nobody could describe.
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()

    if edit.unservable:
        response.headers[UNSERVABLE_HEADER] = ",".join(str(one) for one in sorted(edit.unservable))
    if edit.unservable and not pushes and _declares_no_status(request):
        response.status_code = SAVED_BUT_NOT_PUSHED
    for daemon_id, push in pushes:
        edit.delivered[daemon_id] = await state.hub.push_config(daemon_id, push)


def _declares_no_status(request: Request) -> bool:
    """Whether this route left its status code to the default, so 202 may take it.

    Asked of the route rather than of the injected `Response`, because the
    injected one is `None` on every route: FastAPI applies a declared
    `status_code=201` only *after* the handler returns, and lets anything written
    on the sub-response win over it. So "did the route declare one" is not
    readable from the response at all, and reading it from the response is how
    `POST /api/screens` answered 202 for a row it had just created.

    `scope["route"]` is set by FastAPI's own `APIRoute`, and a route reached by
    some other means is one whose declared status is not knowable here -- so it
    is left alone, `UNSERVABLE_HEADER` being the part of this contract that does
    not depend on a status code at all. Both directions are pinned by tests, so a
    framework that stopped setting `route` is a failure rather than a 202 that
    quietly stopped happening.
    """
    route = request.scope.get("route")
    return route is not None and getattr(route, "status_code", None) is None


def _assemble(edit: Change) -> list[tuple[int, ConfigPush]]:
    """Mint a version and build a snapshot for every rack this edit touched.

    The version first and the snapshot second, which is the order
    `bump_config_version_on` documents: the number is then never newer than the
    rows it names, and an edit that races this one mints a higher one and pushes
    its own.

    Both on the edit's own connection, so the snapshot describes the write that
    has not been committed yet -- which is the whole reason a refusal here can
    still undo it.
    """
    pushes: list[tuple[int, ConfigPush]] = []
    for daemon_id in edit.daemons():
        version = bump_config_version_on(edit.connection, daemon_id)
        edit.versions[daemon_id] = version
        try:
            snapshot = build_snapshot_on(edit.connection, edit.state.secrets, daemon_id)
        except SnapshotError as error:
            if _servable(edit.state, daemon_id):
                # This edit is what broke it, so this edit is what goes. The
                # message is the one `build_snapshot` wrote: it names the field
                # or the row, which is the only thing the person who just saved
                # a screen can act on.
                raise HTTPException(status_code=422, detail=str(error)) from error
            # It was already broken, and refusing here would make the API a trap
            # with no way out: every edit to this rack, including the one that
            # fixes it, would be refused for a reason that is not about the
            # change. Saved, recorded, not pushed, and said out loud in the
            # response's status code and in the rack's `config_error`.
            log.error(
                "an edit was saved for a rack that cannot be given a configuration",
                extra={"daemon": daemon_id, "error": str(error)},
            )
            edit.unservable[daemon_id] = str(error)
            edit.record(daemon_id, "error", "unservable", str(error))
            continue
        pushes.append((daemon_id, ConfigPush(version=version, snapshot=snapshot)))
    return pushes


def _servable(state: State, daemon_id: int) -> bool:
    """Whether this rack could have been given a configuration *before* this edit.

    On a connection of its own, which is exactly what makes it able to answer
    that: in WAL a second connection reads the last committed state, so it sees
    the rows as they were when the transaction above began, however much that
    transaction has written since.

    `RecursionError` is caught here as well as at `_json_column`, where it is
    turned into a message. This is the outer net: anything in the assembly that
    recurses over a document a hand-edited database can make arbitrarily deep
    lands here, and the honest answer to "could this rack have been given a
    configuration" is then no. Escaping instead makes the *edit* a 500, which
    would refuse the repair along with everything else.
    """
    try:
        build_snapshot(state.database, state.secrets, daemon_id)
    except (SnapshotError, KeyError, RecursionError):
        return False
    return True


def config_error(state: State, daemon_id: int) -> str | None:
    """Why this rack cannot be given a configuration, or None because it can.

    Read on every listing rather than stored, because there is no moment at
    which it could be written: the reasons are spread across the screen,
    integration, template and setting tables, and an edit to any of them can
    make a rack servable again without naming it.

    This is `GET /api/daemons`, per rack, and it is the only place a rack that
    cannot be given a configuration says so -- so it is the last route that may
    fail because one is broken. An exception escaping here answers 500 for every
    rack in the list, the healthy ones included, and takes away the page the
    reason would have been read on. `RecursionError` is the outer net; see
    `_servable`.
    """
    try:
        build_snapshot(state.database, state.secrets, daemon_id)
    except SnapshotError as error:
        return str(error)
    except KeyError:
        return None
    except RecursionError:
        return "this rack's configuration is nested too deeply to read"
    return None


def one_row(
    connection: sqlite3.Connection, statement: str, parameters: tuple, *, missing: str
) -> sqlite3.Row:
    """A row, or the 404 that says it is not there.

    Every route in this package reads its subject before it edits it, and the
    alternative to this is `if row is None: raise HTTPException(404)` written
    eleven times -- ten of which will say the same thing and one of which will
    hand `None` to the line below and answer 500 for a screen somebody deleted
    in another tab.
    """
    row = connection.execute(statement, parameters).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=missing)
    return row
