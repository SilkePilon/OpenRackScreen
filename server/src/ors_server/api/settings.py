"""The rack-wide settings: the timezone and the night window.

Two keys of a table that also holds the admin password hash, which is why this
module names the keys it answers with rather than selecting the table. A
`SELECT * FROM setting` in a list comprehension is one line and it puts an argon2
hash of the admin's password into a JSON response.

**Both settings affect every rack.** `build_snapshot` reads them for whichever
daemon it is assembling, so a timezone change is a change to every snapshot --
the routes below never call `affects`, which `changes.Change` reads as "every one
of them".

The admin password is `POST /api/auth/setup`'s and stays there: it is a
credential, it is rate-limited, and it has nothing to do with what a rack
displays.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request, Response
from ors_schema.daemon import DaemonConfig, NightWindow
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ors_server.api.changes import change

log = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

MAX_TIMEZONE = 64

READABLE = ("timezone", "night")
"""The only keys this API answers with, named rather than selected.

`setting` is a key-value table, so what is in it is whatever anybody has put
there -- today `admin_password_hash` and these two. An allow-list is the only
shape of this route that a new key cannot leak by being added.
"""


class SettingsView(BaseModel):
    timezone: str
    night: NightWindow


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(default=None, min_length=1, max_length=MAX_TIMEZONE)
    night: NightWindow | None = None

    @field_validator("timezone")
    @classmethod
    def _resolvable(cls, timezone: str | None) -> str | None:
        """Refuse a zone this host cannot resolve, in the request that set it.

        `DaemonConfig.timezone` is a free string and the daemon's `system_clock`
        is what turns it into a `ZoneInfo` -- so an unresolvable one is not a
        snapshot the server can refuse, it is a rack that will not start. The
        check is here because this is the last place a person is looking.

        It is a check against *this* host's tzdata and the rack has its own,
        which makes it necessary rather than sufficient: a server that knows
        "Europe/Amsterdam" and a Pi that does not is still possible, and shows
        up as a nack. Catching the typos is worth having anyway, and both ends
        of a normal deployment carry the same tzdata.
        """
        if timezone is None:
            return None
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"no such timezone: {error}") from error
        return timezone


@router.get("/settings")
async def read_settings(request: Request) -> SettingsView:
    with closing(request.app.state.database.connect()) as connection:
        return _view(connection)


@router.patch("/settings")
async def patch_settings(request: Request, response: Response, body: SettingsBody) -> SettingsView:
    async with change(request, response) as edit:
        named = body.model_dump(exclude_unset=True)
        if "timezone" in named and body.timezone is not None:
            _write(edit.connection, "timezone", body.timezone)
        if "night" in named and body.night is not None:
            _write(edit.connection, "night", body.night.model_dump_json())
        for daemon_id in edit.daemons():
            edit.record(daemon_id, "info", "settings", "the rack-wide settings changed")
        view = _view(edit.connection)
    return view


def _write(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _view(connection: sqlite3.Connection) -> SettingsView:
    """What is stored, with the defaults `DaemonConfig` would have used.

    The defaults come from the schema's own models rather than from constants
    here, for the reason `build_snapshot` leaves absent keys out of its payload:
    one place writes a default, and a second copy would be an interface showing
    a night window the rack is not keeping.
    """
    stored = {
        row["key"]: row["value"]
        for row in connection.execute(
            f"SELECT key, value FROM setting WHERE key IN ({','.join('?' * len(READABLE))})",  # noqa: S608 - names from READABLE
            READABLE,
        )
    }
    night = NightWindow.model_validate(json.loads(stored["night"])) if "night" in stored else None
    return SettingsView(
        # The schema's default, read off the field rather than repeated: an
        # interface showing "UTC" while the rack keeps something else is a
        # disagreement nobody would look for.
        timezone=stored.get("timezone", DaemonConfig.model_fields["timezone"].default),
        night=night or NightWindow(),
    )
