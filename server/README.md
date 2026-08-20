# OpenRackScreen server

Owns the rack's configuration, hands it to each daemon over a WebSocket, and
serves the API the interface is built on. One process, one SQLite file, no
external services.

The two halves of OpenRackScreen deploy differently on purpose:

| | runs as | where |
| --- | --- | --- |
| **server** (this) | a Docker container, a `uv tool install`, or its own systemd unit | the Pi, or any machine on the LAN |
| **daemon** | a systemd unit on the host | the Pi wired to the panels |

The daemon is **not** part of either compose file, and that is a decision rather
than an omission — see [The daemon is not in compose](#the-daemon-is-not-in-compose).

## Bringing it up

Three ways, and the first two of them depend on something nobody has published
yet — read [Nothing here is published](#nothing-here-is-published) before typing
any of them.

### Docker

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

Both publish `8080`, build the checkout they are run from, and keep their
database in a named Docker volume. Neither needs anything else set to start.

**The volume is called `deploy_ors-data`, not `ors-data`.** Compose prefixes
volume names with the project name, and the project name defaults to the
directory holding the compose file — which is `deploy/`. This matters exactly
once, and it matters a lot: a `docker run -v ors-data:/data` typed from the
declaration in the YAML silently creates a *new, empty* volume of that name and
backs up nothing. `docker volume ls` is the authority. `docker compose -p NAME`
sets the prefix if you want a different one.

Neither file names an `image:`, and that is deliberate rather than an oversight:
compose prefers `image:` over `build:` when a service has both, so a file
carrying both never builds your working tree.

**Pass `--build` whenever the checkout has moved.** `docker compose up -d`
builds only when the service has no image yet; after the first one it reuses
`<project>-server` regardless of what has changed on disk, and `down` removes
containers rather than images. This was reproduced while checking this file: a
container came up running code from before the previous commit, and behaved
exactly as that code did.

`deploy/compose.image.yaml` is the opt-in overlay for the published image, kept
separate so that opting in is a thing you type:

```bash
docker compose -f deploy/compose.pi.yaml -f deploy/compose.image.yaml up -d
```

### `uv tool install`

```bash
uv tool install ors-server
ors-server
```

`ors-server` with no subcommand runs the server — there is no `serve` word, and
`install`/`uninstall` are the only two words this program takes. It needs
nothing set: it creates `$XDG_STATE_HOME/openrackscreen` (or
`~/.local/state/openrackscreen`), generates `secret.key` into it at `0600` on
first boot, and **serves the interface out of the wheel**, so there is no
`pnpm build` to do and no `ORS_WEB_DIR` to point anywhere.

Unlike the container, this announces itself over mDNS by default. That is the
point — it is how a rack that has never been paired finds a server to ask to
join — but on a laptop it is usually not what you want; `ORS_ANNOUNCE=0` turns
it off, and only exactly that value does.

### `ors-server install`

A server that survives a reboot without Docker:

```bash
sudo ors-server install
```

Six steps, and it is safe to re-run — re-running **is** the upgrade path:

1. Refuse an impossible `--port` (outside 1–65535) **before touching
   anything**, so a typo leaves the machine exactly as it was rather than
   enabled into a unit that dies at every boot on `Invalid argument`.
2. `/var/lib/ors-server`, at `0700`, re-asserted on every run — it holds
   `secret.key`, and the server refuses to start if that file is readable by
   anyone but its owner.
3. The `openrackscreen` system user. The **same account the daemon's install
   creates**, so a Pi running both halves has one service account — but a
   different state directory, because `/var/lib/openrackscreen` holds the rack's
   pairing and `ors.db` has no business landing on top of it. A `useradd` exit
   of 9 means it already exists and is success, which is what makes either
   install order work.
4. A venv at `/opt/ors-server` (`--prefix` moves it) with `ors-server` installed
   into it. **Not** `/opt/openrackscreen`, which is the daemon's: `uv venv` on an
   existing prefix rebuilds it, so one shared prefix would have each half
   quietly replacing the other's interpreter and packages.
5. `/etc/systemd/system/ors-server.service`.
6. `systemctl daemon-reload`, `enable --now`, `try-restart`.

It prints where everything went and one line to check it with:

```
unit: /etc/systemd/system/ors-server.service
service user: created
data directory: /var/lib/ors-server
health: curl -fsS http://127.0.0.1:8080/api/health
```

The unit differs from `daemon/examples/openrackscreen.service` in every line
that carries weight, which is why the two installers share no code. It sets
`PrivateDevices=yes`, which the daemon's **must not** (it hides `/dev/spidev*`
and takes the rack dark); it has no `SupplementaryGroups=spi gpio`, because
handing a network-facing service the rack's hardware buys nothing; it has no
`TimeoutStopSec=30`, because nothing here holds a panel that has to be slept
before it is killed; and it sets two `Environment=` lines the daemon has no use
for:

- **`ORS_DATA_DIR=/var/lib/ors-server`**, explicit and load-bearing. The code
  default is a *user's* state directory, resolved against this unit's
  `--system --no-create-home` account, whose home is not a place a database
  should be and may not be writable at all. The dangerous half is not the crash
  — it is the case where it works: a server pointed at a fresh directory comes
  up healthy and asks for a new admin password because it is a new database,
  with the real one still on disk somewhere else, and nothing logs that.
- **`ORS_ANNOUNCE=1`**, stated rather than left to the default because the image
  sets the opposite. This one is on the host's own link, where announcing is the
  entire point.

`--port` reaches both `ORS_PORT` and the mDNS announcement from a single read,
so the port racks are told to dial cannot drift from the one uvicorn binds.

`sudo ors-server uninstall` stops the service, disables it, removes the unit and
**leaves the database alone**. `--purge` deletes `/var/lib/ors-server` — the
admin password, `secret.key`, and with it the only thing that could ever decrypt
the integration credentials in a backup of `ors.db` you already took. Neither
form removes the venv at `--prefix`.

Both subcommands must run as root; as anyone else they print
`ors-server install has to run as root.` and exit `2` without touching the
machine.

Then, whichever way you brought it up, open `http://<host>:8080/`.

## Nothing here is published

**No version of `ors-server` has ever been uploaded to PyPI, and no image has
ever been pushed to ghcr.io.** Both are designed, both are wired up, and neither
has happened:

- Publishing is `.github/workflows/release.yml`, which fires on a `v*` tag and
  uploads all five distributions over **Trusted Publishing**. Trusted Publishing
  has to be configured by a human on PyPI, once per project, *before* the first
  tag — and nobody has. Until then `uv tool install ors-server` answers
  `Because ors-server was not found in the package registry …`.
- That reaches `ors-server install` too, whose venv step is
  `uv pip install ors-server==<version>` against the same index. It handles the
  failure rather than hiding it: the unit is written and then **deliberately not
  enabled or started**, with a warning naming the reason — because a
  `Type=simple` unit whose `ExecStart` does not exist fails asynchronously,
  `enable --now` exits `0` anyway, and `StartLimitIntervalSec=0` means it never
  latches into `failed`, so it would re-exec every five seconds forever while
  `systemctl is-failed` answered no. Unlike `ors-daemon install`, there is no
  `--use-current-interpreter` escape hatch here.
- No workflow in this repository builds or pushes a container image at all, so
  `deploy/compose.image.yaml`'s `ghcr.io/silkepilon/openrackscreen:latest`
  answers `error from registry: denied` on a `pull`.

Building the image from a checkout — the two compose files above, or
`docker build -f deploy/Dockerfile` — needs none of that and works today.

## First run

The first request to the API is what sets the admin password: there is no
default password and no account to guess, because a monitoring display that
ships with `admin/admin` on a LAN is a monitoring display anyone on that LAN
can rewrite. Open the server in a browser and it will ask you to choose one.

Until a password is set, every configuration route answers `401` — including,
usefully, from `curl`, so a half-finished deployment cannot be paired against
by accident.

### Approving a rack

The ordinary way a rack joins, and nobody types a token. `sudo ors-daemon
install` on the Pi prints a **six-character short code**; the rack finds this
server over mDNS (or is told where it is with `--server URL`) and files a claim.
It then appears under **"Waiting to join"** on the Daemons page, above the rack
list. Compare the six characters against the ones the Pi printed, and click
Approve.

Be honest about what that comparison proves. **It binds against confusion, not
against somebody who has already seen it** — it is what stops you approving the
wrong rack when two were installed the same afternoon, and a stranger's rack
when you installed one. Both ends display it, so it is not a secret and anyone
with the Pi's console or journal could file a claim carrying it. It is a check
against a mistake.

Denying deletes the claim rather than recording it. A poll afterwards answers
`404`, byte-identical to a claim id nobody ever filed, so a prober cannot
confirm a denial — which means a denied rack cannot tell and files again, and a
24-hour server-side suppression is what stops it reappearing every few seconds
and training you to click Approve.

### Pairing with a token

Still supported, still first-class, and the answer for any rack that cannot use
discovery — multicast dropped, a different VLAN, this server in a bridged
container. Mint a token for it in the interface, then, **as the daemon's own
user**, on the Pi:

```bash
sudo -u openrackscreen ors-daemon connect --server http://<host>:8080 --token <token>
```

As that user and not under a bare `sudo`, which writes the pairing root-owned
and `0600` — the daemon then gets `PermissionError` on every read of it, runs
unpaired, and says so in a log while the interface shows nothing at all.

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

**Turn that proxy's access log off for `/api/racks/claims/`, or keep it
somewhere you would keep a password.** This server does not write an access log
— uvicorn's is off, deliberately, because the claim id in
`GET /api/racks/claims/{id}` *is* the credential a rack's poll authenticates
with, and a URL in a log is that credential written down for everyone who can
read the log. Nothing here can reach a proxy's own log, so it is the one place
that decision has to be made again by hand.

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
docker run --rm -v deploy_ors-data:/data -v "$PWD:/backup" alpine \
    tar czf /backup/ors-backup.tar.gz -C /data .
docker compose -f deploy/compose.pi.yaml start
```

Stopped rather than running, because SQLite in WAL mode has sidecar files and a
copy taken mid-write is a copy of a database mid-write.

**`deploy_ors-data`, with the prefix.** The compose files declare the volume as
`ors-data`; compose creates it as `<project>_ors-data`, and the project name
defaults to the directory holding the compose file. Getting this wrong has no
error at all — `docker run -v ors-data:/data` *creates* an empty volume of that
name and tars up nothing, leaving you with an 87-byte archive containing one
`./` entry and a backup you will discover is empty on the day you need it.
Confirm the name before you trust the archive:

```bash
docker volume ls                                   # the authority
tar tzf ors-backup.tar.gz                          # must list ors.db and secret.key
```

If you bring the stack up with `docker compose -p NAME`, substitute
`NAME_ors-data`.

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

Install it with `sudo ors-daemon install`, which writes that unit for you (the
hand-authored copy is `daemon/examples/openrackscreen.service`, and the two
differ only in `ExecStart`); `daemon/README.md` has the rest.

One collision to know about on the Pi, where both halves share a machine: the
daemon's `StateDirectory=openrackscreen` **is** `/var/lib/openrackscreen` on the
host, created `0700` and owned by the `openrackscreen` user, and it holds the
pairing. That is the same path the server uses *inside its container*. They do
not collide, because the compose files mount a named Docker volume rather than
the host directory — but replacing that named volume with a bind mount of
`/var/lib/openrackscreen` would drop the server's `ors.db` into a directory its
uid cannot enter, on top of the one file the rack cannot be given again.

## The interface ships with the server

There is no second container and no separate web server, and there is no second
*package* either. The interface reaches a deployment two ways, and both of them
are "it is already there":

- **In the image.** The Dockerfile builds the SPA in a Node stage, copies the
  built `web/dist` to `/app/web`, and sets `ORS_WEB_DIR=/app/web`. The Node
  stage is discarded: the shipped image has no Node, no pnpm and no
  `node_modules`; `/app/web` is static HTML, JS and CSS.
- **In the wheel.** `.github/workflows/release.yml` builds the interface and
  copies it to `server/src/ors_server/web/` before `uv build`, and the wheel's
  own configuration ships everything under that package — so a
  `uv tool install ors-server` gets the interface with the server, with no
  `pnpm` anywhere and nothing to set. The workflow **refuses to upload a server
  wheel that has no `ors_server/web/index.html` in it**, before anything is
  published, because a wheel without the interface installs cleanly, serves no
  pages, and cannot be un-installed by yanking.

Either way the server mounts that directory under everything it already routes,
so the interface and the API answer on one port, from one origin, and no CORS or
proxy configuration exists to get wrong.

`ORS_WEB_DIR` is where the server looks, and **its default as of M3c is the copy
inside the wheel** — `<site-packages>/ors_server/web`, not the `/app/web` it used
to be, because a pip-installed server has no `/app`. The image therefore sets
`ORS_WEB_DIR` itself rather than relying on the default. Two things follow:

- **A missing build is not an error.** The server logs
  `no interface to serve: … holds no index.html` once at startup and serves the
  API alone. That is the right behaviour for a checkout that has never run
  `pnpm build` — and it is also what a wrong `COPY` in the Dockerfile looks
  like: a container that starts, passes its healthcheck, answers
  `/api/health`, and returns 404 for every page. **If every page is a 404 and
  the API works, that warning is in the log and it names the directory.**
- **Working on the interface locally** means running the server against your own
  build instead of the image's:

  ```bash
  ORS_WEB_DIR=web/dist ORS_ANNOUNCE=0 uv run ors-server
  ```

  `ORS_ANNOUNCE=0` because announcing is on by default: without it a checkout
  really does advertise itself to every machine on your network as a server
  racks may ask to join.

  Or don't — `pnpm dev` in `web/` proxies `/api` and `/ws` to `127.0.0.1:8080`,
  which is the better loop while editing.

The API keeps its paths. Everything this server answers for itself is under
`/api` or `/ws` — the docs are at `/api/docs`, the schema at
`/api/openapi.json` — and the mount declines both prefixes rather than
shadowing them. So an unknown `/api/...` is still a JSON `404` and not the index
page, which matters most for the requests that *do not* exist: a client
misspelling a route gets an error it can read instead of a page.

## mDNS discovery does not cross a Docker bridge

A rack that has never been paired can find its server by itself: the server
announces `_openrackscreen._tcp.local.` over mDNS, and `ors-daemon` browses for
it. **That does not work from a container on a bridge network, and no setting
inside the container can make it.** mDNS is a link-layer protocol — multicast to
`224.0.0.251`, TTL 1 — and a Docker bridge is a NAT, so the announcement never
reaches the LAN at all. The addresses the container would announce are the
bridge's, which name nothing reachable from outside it either.

Both compose files above use `ports:`, which is a bridge. So the image sets
`ORS_ANNOUNCE=0`: an announcement nobody hears is merely useless, but one
carrying an unreachable address is worse than none — a rack finds it, files a
claim against an address that does not answer, and never prints the
"no server found, pass `--server`" line that names the fix.

Three ways to get discovery, and a rack only needs one of them:

- **Host networking**, which puts the server on the LAN's own link:

  ```bash
  docker compose -f deploy/compose.pi.yaml -f deploy/compose.mdns.yaml up -d
  ```

  That overlay sets `network_mode: host`, drops the published port with
  `ports: !reset null`, and turns `ORS_ANNOUNCE` back on. All three are needed
  and none is enough alone: host networking without the variable is a silent
  server, and the variable without host networking is the useless announcement
  the image is avoiding. It costs the container's network namespace: `8080` is
  now a port on the host, a conflict there is a startup failure rather than a
  rebind, and the server can reach every service on the host. Compose 2.24 or
  newer, for the `!reset`.

- **Don't use a container.** `ors-server install` writes a unit with
  `Environment=ORS_ANNOUNCE=1` and runs on the host's own link, where none of
  the above applies. Same for a plain `uv tool install ors-server`, which
  announces by default.

- **Or name the server**, which needs nothing from this side at all — either
  with a token:

  ```bash
  sudo -u openrackscreen ors-daemon connect --server http://<host>:8080 --token <token>
  ```

  or by pointing the join flow itself at one server instead of browsing, which
  keeps the approve-by-short-code gesture intact:

  ```bash
  ors-daemon run --server http://<host>:8080
  ```

  This is also the answer on any network that drops multicast between the rack
  and the server — plenty do, including most managed switches with IGMP snooping
  and every setup where the two are on different VLANs.

Verified on this machine, after the fix below: a container brought up with
`-f deploy/compose.mdns.yaml up -d --build` logs
`announcing <host>._openrackscreen._tcp.local. on port 8080` and
`ors_daemon.discovery.discover()` returns it. **But none of this has ever
crossed two machines.** Announcing and browsing were
put against real sockets for the first time while this file was being verified
— a server logging `announcing <host>._openrackscreen._tcp.local. on port 8080`,
and `ors_daemon.discovery.discover()` returning it — but both ends were the same
host. No rack has ever been *paired* over a browse, and multicast between two
machines through a switch that may be snooping IGMP is exactly the part still
untested. That is why `--server` is documented above as a first-class path and
not as a workaround: it is the one of the two that does not depend on multicast
behaving.

It is also what found the reason none of it worked. The lifespan called
python-zeroconf's *synchronous* `register_service` from the event loop thread,
where zeroconf adopts that loop and then blocks it waiting on a coroutine it
scheduled onto it — a deadlock by construction, `EventLoopBlocked` ten seconds
later, caught and logged as a warning while the server came up silent. Every
deployment that set `ORS_ANNOUNCE=1` announced nothing, and the whole test suite
stayed green because it substitutes the responder.

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

Two of these defaults **moved in M3c**, both so that `uv tool install ors-server
&& ors-server` needs no root and no configuration. Neither the container nor the
generated unit relies on the new defaults: both set the two variables
explicitly, which keeps the chosen path visible where it is chosen.

| variable | default | what it does |
| --- | --- | --- |
| `ORS_DATA_DIR` | `$XDG_STATE_HOME/openrackscreen`, else `~/.local/state/openrackscreen` | where `ors.db` and `secret.key` live. **Moved in M3c** — it used to be `/var/lib/openrackscreen`, which needs root and made a first boot a `PermissionError` for anyone who had not read this table. The image sets `/var/lib/openrackscreen` and `ors-server install` sets `/var/lib/ors-server`; in both, change it and you must move the volume mount or `StateDirectory=` with it. |
| `ORS_HOST` | `0.0.0.0` | in code as well as in the image, which sets it anyway because `127.0.0.1` *inside a container* is the container and nothing else and the mistake is worth naming where it bites. |
| `ORS_PORT` | `8080` | must match the container side of the published port. It is read once and used twice — uvicorn binds it and the mDNS announcement tells racks to dial it — so the two cannot disagree. `ors-server install --port` writes it into the unit. |
| `ORS_SECRET_KEY` | generated | see above. Unrecoverable. An empty string is refused at startup rather than read as "unset". |
| `ORS_WEB_DIR` | the interface **inside the wheel**, `<site-packages>/ors_server/web` | where the built interface is. **Moved in M3c** — it used to be `/app/web`, which is a container path a pip-installed server does not have, so the image now sets that itself. A checkout has no copy inside the package and needs `ORS_WEB_DIR=web/dist`. See [The interface ships with the server](#the-interface-ships-with-the-server). |
| `ORS_LOG_LEVEL` | `INFO` | one JSON object per line, on stdout. There is no per-request access log at any level: uvicorn's is off, because `GET /api/racks/claims/{id}` carries a bearer credential in its path. See [Behind a reverse proxy](#behind-a-reverse-proxy). |
| `ORS_ANNOUNCE` | on in the code, **`0` in the image**, `1` in the unit `ors-server install` writes | `ORS_ANNOUNCE=0`, exactly that value, stops the server announcing itself over mDNS as `_openrackscreen._tcp.local.` — which is how a freshly installed rack finds a server to ask to join. Anything else, including `false`, leaves it on. The image sets it off because the announcement does not reach the LAN from a bridge network at all, and the address in it would be the bridge's; see [mDNS discovery does not cross a Docker bridge](#mdns-discovery-does-not-cross-a-docker-bridge) for the host networking that turns it back on. Worth setting to `0` in a checkout too, so a development server does not advertise itself to every rack on your LAN. A failure to announce is a warning rather than a refusal to start in any case. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | which proxies' `X-Forwarded-For` to believe. Read only while uvicorn's proxy headers are on, which they are — passed explicitly rather than inherited. |

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
`ors-schema` and `ors-render` packages. It also needs `web/`, because the image
builds the interface: three stages, `uv` → `node` → `python:3.12-slim-trixie`,
of which only the last one ships.

Nothing has to be installed or built beforehand. `web/node_modules` and
`web/dist` are excluded from the context in `.dockerignore` on purpose, so the
image is the same whether or not you have ever run `pnpm install` — and a
cross-build for the Pi cannot pick up a tree of x86 `rollup` and `esbuild`
binaries from the host.

For a 64-bit Pi from another machine:

```bash
docker buildx build --platform linux/arm64 -f deploy/Dockerfile -t openrackscreen .
```

**That command has never been run by anyone**, on this machine or in CI. `arm64`
is the supported target on paper and an untested path in practice; everything
this file states about the image was verified on `linux/amd64`, where the build
takes about sixteen seconds warm and produces roughly 297 MB.

Only the Python half of that is emulated. The Node stage is pinned to
`--platform=$BUILDPLATFORM`, so `pnpm install`, `tsc -b` and `vite build` run
natively on the machine you are building on: `dist` is HTML, JS and CSS, and it
is the same bytes whatever CPU emitted them. The uv builder is not pinned that
way on purpose — it installs wheels *for* the target.

`arm64` is the supported target, and it is the **only** ARM target. It has been
since this image first existed, which is older than the interface being in it:
the first `FROM` in `deploy/Dockerfile` is
`ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim`, and that tag publishes
`linux/amd64` and `linux/arm64` and no `linux/arm/v7`. A
`--platform linux/arm/v7` build therefore fails on that line — before any
Python, and before the Node stage is even reached. `node:24` publishes no
`linux/arm/v7` either, so the interface stage is a second wall behind the first
one rather than the thing that closed 32-bit ARM. Use the 64-bit Raspberry Pi
OS.

The builder stage still installs `gcc` and `libc6-dev` for a 32-bit reason —
`argon2-cffi-bindings` publishes no `armv7l` wheel, so Argon2 would compile from
C there, while `cryptography` does ship one. That layer has been dead in
practice since the uv tag was pinned, and is kept because it is cheap (one apt
layer in a stage nothing ships), because the wheel situation is what would have
to change first, and because the runtime stage must still not have a compiler in
it either way. Anyone reopening 32-bit ARM starts at the uv base image, not at
Node.
