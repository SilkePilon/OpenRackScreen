from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ors_server.app import AppSettings, create_app
from ors_server.logging import setup_logging

WS_MAX_MESSAGE_BYTES = 512 * 1024
"""The largest WebSocket message this server will read. Derived, not chosen.

uvicorn's `ws_max_size` defaults to 16,777,216, and inheriting that was the
whole problem: it is a number about websockets in general and has nothing to do
with a rack. Every message on this link is small and the largest by far is a
`Frame`, whose payload is bounded at `MAX_FRAME_BYTES` -- but base64 is 4/3 of
what it carries, so a 256 KiB panel is 349,528 bytes on the wire before the JSON
envelope around it. Half a mebibyte is a little over 1.5 times that, which
leaves room for the envelope, for the heartbeat's `status` object, and for a
field somebody adds later, while being a thirty-second of what uvicorn would
otherwise allow.

What it buys is the difference between a bound and a field validator. `Frame`'s
`max_length` counts the *decoded* payload, so it can only refuse a message the
server has already read and base64-decoded: at the default, any peer holding a
valid daemon key -- or reaching an unauthenticated `/ws/daemon` socket at all --
can make this process allocate 16 MiB per message, as fast as it can write them.
This bound is enforced by `websockets` before a byte reaches the application.

It is deliberately above `MAX_FRAME_BYTES` rather than equal to it: a limit set
to the schema's number would close the socket over a frame the schema accepts,
which is the reconnect loop `MAX_FRAME_BYTES` exists to prevent, arriving from
the transport instead of from the encoder.
"""


def packaged_web_dir() -> Path:
    """The built interface, as shipped inside the wheel.

    `Path(__file__).parent` and not `importlib.resources`: the directory is
    handed to starlette's `StaticFiles`, which wants a real path on a real
    filesystem, and this project is never installed from a zipimport.
    """
    return Path(__file__).resolve().parent / "web"


def resolve_web_dir() -> Path:
    """Where the built interface is. The environment first, the wheel second.

    In that order because the container sets `ORS_WEB_DIR` deliberately, and a
    resolution that preferred the packaged copy would make that setting dead
    everywhere it is used. A checkout serving its own build sets it to
    `web/dist`; `create_app` warns once and serves the API alone when the
    directory holds no build, which stays the ordinary developer state.
    """
    from_environment = os.environ.get("ORS_WEB_DIR")
    return Path(from_environment) if from_environment else packaged_web_dir()


def resolve_data_dir() -> Path:
    """The database, the secret key and the stored credentials.

    `~/.local/state/openrackscreen` and not `/var/lib/openrackscreen`, which
    needs root: the point of publishing to an index is that `uv tool install
    ors-server && ors-server` works, and a default only root can write makes
    the first boot a PermissionError for everyone who has not read the
    environment table. Both deployments that *should* use `/var/lib` -- the
    container and the generated unit -- set `ORS_DATA_DIR` explicitly, which
    keeps that path visible where it is chosen rather than implicit here.
    """
    from_environment = os.environ.get("ORS_DATA_DIR")
    if from_environment:
        return Path(from_environment)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "openrackscreen"


def main() -> int:
    # First, before anything that can log: `create_app` reports a rebuilt
    # schema, and until this runs the root logger has no handler and every
    # record the server writes is discarded where nobody can see it.
    setup_logging(os.environ.get("ORS_LOG_LEVEL", "INFO"))
    settings = AppSettings(
        data_dir=resolve_data_dir(),
        secret_key=os.environ.get("ORS_SECRET_KEY"),
        web_dir=resolve_web_dir(),
    )
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("ORS_HOST", "0.0.0.0"),
        port=int(os.environ.get("ORS_PORT", "8080")),
        # Both of these are settled here rather than left to a default, and for
        # opposite reasons.
        #
        # `ws_max_size` because the default has nothing to do with this
        # application: see `WS_MAX_MESSAGE_BYTES` for what 16 MiB per message
        # costs a server that anyone can open a socket to.
        ws_max_size=WS_MAX_MESSAGE_BYTES,
        # `proxy_headers` because it is *already* the default -- uvicorn 0.52's
        # signature is `proxy_headers: bool = True` -- and passing it makes that
        # legible instead of load-bearing and invisible. The deploy notes tell
        # an operator to set `FORWARDED_ALLOW_IPS` when the server sits behind a
        # reverse proxy, and that environment variable is read only while this
        # is on (uvicorn falls back to `127.0.0.1` when it is unset, so the
        # default is already the safe one). Written down because a future
        # uvicorn flipping it would silently turn every `X-Forwarded-For` this
        # deployment relies on back into the proxy's own address, and the only
        # symptom would be an audit log naming one machine for every request.
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
