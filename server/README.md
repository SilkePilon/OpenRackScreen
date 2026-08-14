# OpenRackScreen server

Owns the rack's configuration, hands it to each daemon over a WebSocket, and
serves the API the interface is built on. One process, one SQLite file, no
external services.

The two halves of OpenRackScreen deploy differently on purpose:

| | runs as | where |
| --- | --- | --- |
| **server** (this) | a Docker container | the Pi, or any machine on the LAN |
| **daemon** | a systemd unit on the host | the Pi wired to the panels |

The daemon is **not** part of either compose file, and that is a decision rather
than an omission — see [The daemon is not in compose](#the-daemon-is-not-in-compose).

## Bringing it up

Two compose files, differing only in whether the server shares a machine with
the panels.

On the same Raspberry Pi that drives the panels:

```bash
docker compose -f deploy/compose.pi.yaml up -d
```

On another machine on the LAN:

```bash
docker compose -f deploy/compose.remote.yaml up -d
```

Both publish `8080` and keep their database in a named Docker volume called
`ors-data`. Neither needs anything else set to start.

Then open `http://<host>:8080/`.

### First run

The first request to the API is what sets the admin password: there is no
default password and no account to guess, because a monitoring display that
ships with `admin/admin` on a LAN is a monitoring display anyone on that LAN
can rewrite. Open the server in a browser and it will ask you to choose one.

Until a password is set, every configuration route answers `401` — including,
usefully, from `curl`, so a half-finished deployment cannot be paired against
by accident.

Pairing a rack afterwards is two steps: mint a token for it in the interface,
then, **as the daemon's own user**, on the Pi:

```bash
sudo -u openrackscreen ors-daemon connect --server http://<host>:8080 --token <token>
```

A token is shown exactly once and cannot be shown again. Losing one costs a
rotate, not a reinstall.

### Behind a reverse proxy

`compose.remote.yaml` reads `ORS_TRUSTED_PROXIES` and passes it to uvicorn as
`FORWARDED_ALLOW_IPS`:

```bash
ORS_TRUSTED_PROXIES=10.0.0.5 docker compose -f deploy/compose.remote.yaml up -d
```

Set it whenever anything sits in front of this server. Left at its default,
uvicorn trusts `X-Forwarded-For` only from `127.0.0.1`, so every login appears
to arrive from the proxy — and the per-IP login limiter then throttles
*everybody* the first time anyone mistypes a password.

## Where the key and the database live

Both are inside the volume, at `/var/lib/openrackscreen` in the container:

| file | what it is |
| --- | --- |
| `ors.db` | every daemon, screen, template, integration and setting |
| `secret.key` | the Fernet key the integration credentials are encrypted under |

The container runs as uid `10001`, and the image chowns that directory to it —
which is what makes an empty named volume come out writable, because a volume
inherits the ownership of the image path it shadows.

To back the deployment up, stop the container and copy the volume:

```bash
docker compose -f deploy/compose.pi.yaml stop
docker run --rm -v ors-data:/data -v "$PWD:/backup" alpine \
    tar czf /backup/ors-backup.tar.gz -C /data .
docker compose -f deploy/compose.pi.yaml start
```

Stopped rather than running, because SQLite in WAL mode has sidecar files and a
copy taken mid-write is a copy of a database mid-write.

### `ORS_SECRET_KEY` is unrecoverable

By default the server generates `secret.key` into the volume on first boot and
keeps it at mode `0600`. It refuses to start if that file is ever readable by
anyone but its owner — a key everyone can read is not a key, and the ciphertext
it protects is in `ors.db` right beside it. The fix is `chmod 600`.

You can supply the key instead, to keep it with your other secrets:

```bash
ORS_SECRET_KEY="$(docker compose -f deploy/compose.pi.yaml run --rm --no-deps \
    server python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

**Once any integration credential has been stored, that key is the only thing
that can read it back, and nothing anywhere keeps a second copy.** Change it, or
lose the volume that holds the generated one, and every stored credential is
permanently undecryptable — not degraded, not recoverable from a backup of the
database, gone. The recovery is to re-enter each credential by hand.

Two consequences worth knowing before it matters:

- Set `ORS_SECRET_KEY` **before** creating any integration, or not at all. Doing
  it afterwards silently replaces the key that the existing ciphertext was
  written under.
- The failure does not appear at boot. Nothing decrypts a credential until a
  daemon is given a snapshot that needs one, so a wrong key looks like a
  perfectly healthy server for as long as it takes something to poll.

Setting `ORS_SECRET_KEY` to an empty string is refused at startup rather than
treated as "unset", for exactly this reason. This is also why both compose files
list the variable by bare name instead of `ORS_SECRET_KEY: ${ORS_SECRET_KEY:-}`,
which would substitute to an empty string and crash a first boot that set
nothing.

## The daemon is not in compose

The daemon needs `/dev/spidev0.0`, `/dev/spidev0.1`, `/dev/spidev1.0`,
`/dev/spidev1.1` and `/dev/gpiochip*`, membership of the `spi` and `gpio`
groups, and a state directory that survives a reboot. Containerising that means
`--privileged`, or a device list that has to be corrected every time a rack is
rewired, and buys nothing: it is one Python process, and systemd manages it
well.

Install it from `daemon/examples/openrackscreen.service`; `daemon/README.md` has
the rest.

One collision to know about on the Pi, where both halves share a machine: the
daemon's `StateDirectory=openrackscreen` **is** `/var/lib/openrackscreen` on the
host, created `0700` and owned by the `openrackscreen` user, and it holds the
pairing. That is the same path the server uses *inside its container*. They do
not collide, because the compose files mount a named Docker volume rather than
the host directory — but replacing that named volume with a bind mount of
`/var/lib/openrackscreen` would drop the server's `ors.db` into a directory its
uid cannot enter, on top of the one file the rack cannot be given again.

## Outbound network

The server makes exactly one outbound HTTP call of its own: the **Test** button
on an integration fetches the Prometheus URL you gave it, from inside the
container, to check that it answers. Nothing else in this image dials out —
daemons connect *to* the server, not the other way round.

On a LAN neither compose file needs to do anything about that. Behind an egress
policy, allow it, or the Test button fails with a connection error that reads
exactly like a wrong URL. The regular polling that drives the panels is done by
the daemon on the rack, not here, so blocking this affects the button and
nothing else.

## Environment

| variable | default | what it does |
| --- | --- | --- |
| `ORS_DATA_DIR` | `/var/lib/openrackscreen` | where `ors.db` and `secret.key` live. Set in the image; change it and you must move the volume mount with it. |
| `ORS_HOST` | `0.0.0.0` in the image | `127.0.0.1` inside a container is the container and nothing else. |
| `ORS_PORT` | `8080` | must match the container side of the published port. |
| `ORS_SECRET_KEY` | generated | see above. Unrecoverable. |
| `ORS_LOG_LEVEL` | `INFO` | one JSON object per line, on stdout. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | which proxies' `X-Forwarded-For` to believe. |

## Health

`GET /api/health` answers `{"status": "ok", "version": ...}` without a session
and without reading a single row, which is why it is what the image's
`HEALTHCHECK` asks for.

Do not point a healthcheck at `GET /api/daemons`. It looks like the more
informative probe and it assembles a snapshot per rack to fill `config_error` —
fine once, a standing per-interval cost forever.

```bash
docker compose -f deploy/compose.pi.yaml ps      # health from the container
curl -fsS http://localhost:8080/api/health       # or directly
docker compose -f deploy/compose.pi.yaml logs -f
```

## Building the image

```bash
docker build -f deploy/Dockerfile -t openrackscreen .
```

The build context is the repository root, not `server/` — the server is one
member of a uv workspace and the build needs the lock file and the shared
`ors-schema` and `ors-render` packages.

For a 64-bit Pi from another machine:

```bash
docker buildx build --platform linux/arm64 -f deploy/Dockerfile -t openrackscreen .
```

`arm64` is the supported target. 32-bit builds work but are slow: `argon2-cffi-bindings`
publishes no `armv7l` wheel, so Argon2 compiles from C — which is why the builder
stage installs `gcc`, and why the runtime stage must not.
