# OpenRackScreen

Four round 240×240 GC9A01 panels bolted into a server rack, driven from a
Raspberry Pi over SPI, drawing whatever a scene description says to draw. The
scenes are declarative JSON, the data comes from Prometheus, and the whole thing
is configured from a web interface rather than by editing YAML over SSH.

Three pieces, and they deploy differently on purpose:

| | what it is | where it runs |
| --- | --- | --- |
| **`daemon/`** | a thread per screen, driving the panels over SPI | a systemd unit on the Pi wired to the panels |
| **`server/`** | FastAPI + SQLite: owns the configuration, talks to each daemon over a WebSocket, serves the interface | a Docker container, a `uv tool install`, or its own systemd unit — on the Pi or anywhere on the LAN |
| **`web/`** | Vite + React + TypeScript, shadcn/ui on the Radix base | inside the server's image; there is no second container |

Plus two shared Python packages: `packages/ors-schema` is the wire contract both
ends validate against, and `packages/ors-render` is the renderer they share, so
a preview in the browser and a frame on the glass come out of the same code.

The daemon has a `virtual` backend that writes PNGs instead of driving a bus,
which is what makes everything below runnable on a laptop.

---

## Try it without hardware

You need [uv](https://docs.astral.sh/uv/), plus Node and pnpm for the interface
(CI uses Node 24; pnpm's version is pinned by the repo). Nothing else — no Pi,
no panels, no Prometheus.

```bash
git clone https://github.com/SilkePilon/OpenRackScreen.git
cd OpenRackScreen
uv sync --all-packages
```

**Draw the rack's four panels to PNG.** No server and no backend involved — this
runs the real renderer against the real GC9A01 config the author's rack runs:

```bash
uv run ors-daemon render --config daemon/examples/rack.yaml --out /tmp/ors-preview
```

That writes `CPU.png`, `MEM.png`, `PODS.png` and `HEALTH.png`. Without `--data`
every screen draws `connecting`, which is exactly what a cold rack shows.

**Bring up the server with its interface.** Three environment variables, and
each of them is there for a reason:

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
ORS_DATA_DIR=/tmp/ors-data ORS_WEB_DIR=web/dist ORS_ANNOUNCE=0 uv run ors-server
```

Then open <http://127.0.0.1:8080/> and set an admin password.

- **`ORS_WEB_DIR` is the one that decides whether you get a website.** Its
  default as of M3c is the interface *inside the installed wheel* — which a
  checkout does not have, because `server/src/ors_server/web/` is built by the
  release workflow and is gitignored here. Leave it unset in a checkout and the
  server starts perfectly happily, answers `/api/health`, and returns **404 for
  every page**. It says so once, at startup, and names the variable:

  ```
  no interface to serve: /…/server/src/ors_server/web holds no index.html. Run `pnpm build` in web/, or point ORS_WEB_DIR at a build.
  ```

  This is the single most likely thing to go wrong on a first run from source.
  If the API works and the site is a 404, that line is in the log.
- **`ORS_DATA_DIR` is optional now, and pointing it at `/tmp` is the point.**
  Its default also moved in M3c: it is `$XDG_STATE_HOME/openrackscreen`, or
  `~/.local/state/openrackscreen`, so an installed server needs no root and no
  variable at all. Setting it here keeps a scratch database out of the one a
  `uv tool install` would use.
- **`ORS_ANNOUNCE=0` keeps your laptop off the LAN's service list.** Announcing
  is *on* by default — that is what lets a rack find a server with nobody
  typing a URL — so a checkout run without this really does advertise
  `_openrackscreen._tcp.local.` to every machine on your network, and a real
  rack that hears it will file a claim against it. Verified in both directions
  on this machine: the server logs `announcing <host>._openrackscreen._tcp.
  local. on port 8080`, and `ors_daemon.discovery.discover()` finds it. Exactly
  the string `0` turns it off; `false` does not.

**Run a rack with no panels.** Copy `daemon/examples/rack.yaml`, switch each
screen's `display:` to `{ backend: virtual, out_dir: /tmp/ors-panels }`, and:

```bash
uv run ors-daemon validate --config /tmp/virtual-rack.yaml
uv run ors-daemon run --config /tmp/virtual-rack.yaml
```

Every frame lands in `/tmp/ors-panels` as a PNG. The shipped config polls a
Prometheus through a `kubectl port-forward` to the author's cluster, so on your
machine the tunnel fails, the log says so, and the panels sit on `connecting` —
a source that has never answered is still connecting, and only one that answered
and then went quiet shows `NO DATA`. That is the honest behaviour, not a broken
one. Point the integration at a Prometheus you do have, or delete it.

`--status /tmp/ors-status.json` writes every screen and every integration to one
file, atomically, which is the thing to `cat` when a panel is wrong.

**What a laptop cannot show you.** The setup wizard in the interface creates
`gc9a01` screens and only `gc9a01` screens, because that is the only panel this
project drives — so pairing a virtual rack and then running the wizard against
it fails where the backend is opened, with `DisplayError: the gc9a01 backend
needs luma, which ships in the hardware extra`. The end-to-end suite gets around
that by substituting the display factory and nothing else; `web/e2e/virtual_rack.py`
is candid about what that does and does not prove. Anything below the backend —
a real `/dev` enumerating, a GC9A01 accepting its init sequence, 40 MHz surviving
your wiring — needs the rack, and `daemon/README.md` ends with the hardware
checklist for exactly that reason.

---

## Run it for real

Three ways to run the server and one way to run the rack. Read
[Nothing is published yet](#nothing-is-published-yet) at the end of this section
before you type any of them: **no version of anything here is on PyPI, and no
image has ever been pushed to ghcr.io**, so today the two paths that work from a
clean machine are Docker built from a checkout, and from source.

### Install

The shortest path there will be, once there is an index to install from:

```bash
uv tool install ors-server
ors-server
```

`ors-server` with no subcommand runs the server — there is no `serve` word to
say — and it needs nothing set. It creates `$XDG_STATE_HOME/openrackscreen`
(`~/.local/state/openrackscreen` when that is unset), generates `secret.key`
into it at mode `0600` on first boot, and **serves the interface out of the
wheel**: no `pnpm`, no `ORS_WEB_DIR`, no second process. Open
<http://127.0.0.1:8080/> and set an admin password.

To survive a reboot without Docker, the same program installs itself as a
service:

```bash
sudo ors-server install
```

Six steps, in this order, and it is safe to re-run — re-running *is* the upgrade
path. It refuses an impossible `--port` before touching anything; creates
`/var/lib/ors-server` at `0700`; creates the `openrackscreen` system user
(shared with the daemon, so either half may be installed first); builds a venv
at `/opt/ors-server`; writes `/etc/systemd/system/ors-server.service`; and runs
`systemctl daemon-reload`, `enable --now` and `try-restart`. It prints the unit
path, whether the user was created, the data directory, and one line to check it
with:

```
unit: /etc/systemd/system/ors-server.service
service user: created
data directory: /var/lib/ors-server
health: curl -fsS http://127.0.0.1:8080/api/health
```

`--prefix` moves the venv and `--port` moves the port — and `--port` reaches
both `ORS_PORT` and the mDNS announcement from one read, so the port racks are
told to dial cannot drift from the one uvicorn binds. Two things this unit does
differently from the container, both deliberate: it sets
`Environment=ORS_DATA_DIR=/var/lib/ors-server` explicitly, because the code
default is a *user's* state directory and a service account's home is not a
place a database should silently appear; and it sets `ORS_ANNOUNCE=1`, because
unlike the container it is on the host's own link and being findable is the
whole point.

`sudo ors-server uninstall` stops the service, disables it and removes the unit,
and **leaves the database alone**. `--purge` deletes `/var/lib/ors-server` —
with it the admin password, `secret.key`, and therefore the only thing that can
decrypt the integration credentials in any backup of `ors.db` you already took.

### Docker

One image, built from the repository root, serving the API and the interface on
one port from one origin:

```bash
docker build -f deploy/Dockerfile -t openrackscreen .
docker compose -f deploy/compose.pi.yaml up -d      # server on the Pi itself
docker compose -f deploy/compose.remote.yaml up -d  # server elsewhere on the LAN
```

Nothing needs installing or building first; the image builds the interface in a
Node stage and then discards it, so the ~297 MB that ships has no Node, no pnpm
and no `node_modules` in it. Both compose files publish `8080` and keep the
database in a named volume — **`deploy_ors-data`, not `ors-data`**: compose
prefixes volume names with the project name, and the project name defaults to
the directory holding the compose file. `docker volume ls` is the authority, and
`server/README.md`'s backup recipe uses the prefixed name for exactly this
reason.

**Both compose files carry a `build:` block and no `image:`, on purpose.**
Compose prefers `image:` over `build:` when a service names both, so a file
carrying both never builds your working tree — and the symptom is a container
that starts, goes healthy, answers `/api/health` and 404s every page, which is
what a stale tag on this machine actually did once.

**Add `--build` when the checkout has changed since the last one.** Removing the
`image:` line did not remove the problem, it moved it: `docker compose up -d`
builds only when the service has *no* image at all, and after that it reuses
`<project>-server` no matter what the working tree says. Verified while checking
this file — a compose file with `build:` and nothing else brought up a container
running code from before the previous commit, and only `up -d --build` picked up
the change. `down` does not help; it removes containers, not images.

`deploy/compose.image.yaml` is the opt-in overlay for the published image, kept
separate so that opting in is a thing you type:

```bash
docker compose -f deploy/compose.pi.yaml -f deploy/compose.image.yaml up -d
```

That names `ghcr.io/silkepilon/openrackscreen:latest`, which **does not exist
yet** — nothing in `.github/workflows/` builds or pushes an image, and a `pull`
against it answers `error from registry: denied`. Until something publishes it,
the overlay is a documented shape and not a working command.

**mDNS does not reach the LAN from either compose file above.** Both use
`ports:`, which is a bridge, and a bridge is a NAT: mDNS is link-layer multicast
to `224.0.0.251` with a TTL of 1 and the packet does not cross. So the image
sets `ORS_ANNOUNCE=0` — an announcement nobody hears is useless, and one
carrying a bridge address is worse than none, because a rack files a claim
against an address that does not answer instead of printing "no server found,
pass `--server`". `deploy/compose.mdns.yaml` is the way to get discovery from a
container, and it works by giving up the network namespace:

```bash
docker compose -f deploy/compose.pi.yaml -f deploy/compose.mdns.yaml up -d
```

`network_mode: host`, `ports: !reset null` and `ORS_ANNOUNCE=1`. Needs Compose
2.24 or newer for the `!reset`. On a network that drops multicast — most managed
switches with IGMP snooping, and every setup where rack and server are on
different VLANs — none of this helps and `--server URL` is the answer instead.

**64-bit Raspberry Pi OS is required.** A `linux/arm/v7` build fails on the
Dockerfile's first line — `ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim`
publishes `linux/amd64` and `linux/arm64` and never published a 32-bit ARM
variant — with `no match for platform in manifest: not found`, before any Python
runs. **`linux/arm64` itself has never actually been built by anyone**, on this
machine or in CI; it is the supported target on paper and an untested path in
practice. Everything stated about Docker above was verified on `linux/amd64`
only.

`server/README.md` owns the rest: the volume layout, `ORS_SECRET_KEY` and why
losing it is unrecoverable, reverse proxies and `ORS_TRUSTED_PROXIES`, backups,
health checks, and the full environment table.

### From source

The server is `uv run ors-server` with `ORS_WEB_DIR` pointed at a `pnpm build`,
as above — and this is the only one of the three paths that runs on a clean
machine today. The daemon is a systemd unit rather than a container: it needs
four `/dev/spidev*` nodes, `/dev/gpiochip*`, and the `spi` and `gpio` groups,
and containerising that buys nothing. `daemon/README.md` owns the SPI wiring,
the `hardware` extra, `ors-daemon install`, and every command.

### The rack

```bash
uv tool install "ors-daemon[hardware]"
sudo ors-daemon install
```

`install` does the eight things a rack needs and prints a **six-character short
code** that `First run` below is about: directories, the `openrackscreen` system
user, membership of `spi` and `gpio`, a venv at `/opt/openrackscreen`, the unit,
`enable --now`, both SPI buses in `/boot/firmware/config.txt` behind a
timestamped backup, and this rack's install identity. **A reboot is required**
afterwards — the SPI change is read by the firmware at boot and nothing else can
apply it — and the command says `reboot needed: yes` when it made one.

`--no-spi` leaves `config.txt` alone, `--prefix` moves the venv,
`--use-current-interpreter` points the unit at an `ors-daemon` you already have
instead of building one, and `--upgrade` changes nothing at all: `install` is
idempotent and a plain re-run is always the upgrade path, so the flag exists to
say that out loud. `sudo ors-daemon uninstall` leaves `/var/lib/openrackscreen`
— and therefore the pairing and the identity — alone; `--purge` removes it, and
that costs a fresh approval in the interface.

### Nothing is published yet

Every `uv tool install` line above resolves against PyPI, and **none of the five
distributions has ever been uploaded**: `ors-schema`, `ors-render`,
`ors-daemon`, `ors-server` and the `openrackscreen` meta-package are built in
lockstep at one version by `.github/workflows/release.yml`, which fires on a
`v*` tag and publishes over Trusted Publishing — and Trusted Publishing has to
be configured by a human, once per project on PyPI, **before** the first tag can
publish anything. Nobody has done that yet. Today:

```
$ uv tool install ors-server
  × No solution found when resolving dependencies:
  ╰─▶ Because ors-server was not found in the package registry and you require
      ors-server, we can conclude that your requirements are unsatisfiable.
```

This reaches further than the two `uv tool install` lines. Both installers build
their venv with `uv pip install <name>==<version>` against the same index, so
`sudo ors-server install` and `sudo ors-daemon install` cannot complete on a
machine with no checkout either. Both handle it the same way and neither leaves
a rack quietly dark: the unit file is written, and then **deliberately not
enabled or started**, with a warning naming the failed install and telling you
to re-run. `ors-daemon install --use-current-interpreter` is the way past it
today, because it skips the venv step entirely and points the unit at an
`ors-daemon` that is already on the machine. `ors-server install` has no
equivalent flag.

---

## First run

1. **Set an admin password.** There is no default account. Until a password
   exists every configuration route answers `401`, including from `curl`, so a
   half-finished deployment cannot be paired against by accident.
2. **Install the daemon on the Pi**, `sudo ors-daemon install`, and write down
   the short code it prints:

   ```
   short code: K7QF2M
   unit: /etc/systemd/system/openrackscreen.service
   service user: created
   SPI: enabled, reboot needed
   reboot needed: yes
   ```

   Then reboot, if it asked. The unit starts a rack that is paired with nothing,
   which is not an error — it is the ordinary state of a rack that has just been
   installed. It browses for `_openrackscreen._tcp.local.`, finds the server,
   files a claim, and waits. The same short code goes into its journal every
   time it starts unpaired, so losing the terminal does not lose the code:
   `journalctl -u openrackscreen | grep 'short code'`.
3. **Approve it.** The rack appears under **"Waiting to join"** on the Daemons
   page, above the rack list. Compare the six characters there against the ones
   the Pi printed, and click Approve.

   **What that code proves, exactly.** It binds against *confusion*, not against
   an attacker: with two racks installed the same afternoon, it is what stops
   you approving the wrong one, and with one rack it is what stops you approving
   a stranger's. It is derived from this rack's identity and is shown by both
   ends — so anyone who has already read the Pi's console, or its journal, can
   read it too, and could file a claim carrying it. It is a check against a
   mistake, and it is not a secret.

   Denying is not visible to the rack. `deny` deletes the claim and a poll
   afterwards answers `404`, byte-identical to a claim id nobody ever filed, so
   that a prober cannot confirm a denial — which means a denied rack keeps
   trying, with a 24-hour server-side suppression to stop it retraining you to
   click Approve.
4. **Or pair it with a token**, which still exists and is a first-class path
   rather than a fallback. A rack that cannot use discovery — multicast dropped,
   a different VLAN, a server in a bridged container — is paired the old way:
   on the Daemons page, add a rack; the server mints a token and shows it
   **exactly once**, beside the line to paste on the Pi:

   ```bash
   sudo -u openrackscreen ors-daemon connect --server http://<host>:8080 --token <token>
   ```

   As the daemon's own user — `sudo ors-daemon connect` writes the pairing
   root-owned and the daemon then cannot read it. Losing a token costs a rotate,
   not a reinstall.

   Between the two there is a third shape: `ors-daemon run --server URL` skips
   the browse and files a claim against one named server, so the approval
   gesture is unchanged on a network where only *discovery* is broken. The unit
   `install` writes does not carry `--server`, so using it means a systemd
   drop-in — or the token above, which needs nothing.
5. **`--config` is optional now.** A paired rack runs from what the server
   pushed and needs no local YAML at all; a rack with neither a pairing nor a
   `--config` is the one that goes and asks to join. Standalone file-driven
   racks are unchanged — pass `--config` and nothing dials anywhere.
   `daemon/README.md` has the five-row table of what `run` boots from.
6. **Add screens.** The wizard runs **detect → wiring → probe → add**.

   The probe step exists because **a panel cannot introduce itself.** A GC9A01
   has no ID register readable over 4-wire SPI, and DC and RST are plain GPIO
   pins that nothing on the bus reports. Detection can enumerate `/dev/spidev*`
   and no more; which physical panel is on which chip select, and which pins
   drive it, is only knowable by lighting one and looking at the rack. So the
   daemon holds a single panel lit for five seconds and you confirm with your
   eyes before the screen is created.

---

## Development

Two toolchains, two independent gates. CI runs them as separate jobs so a red
interface reads as a red interface.

**Python** — one `uv` workspace over `packages/*`, `daemon` and `server`:

```bash
uv sync --all-packages
uv run pytest                                    # 2597 passed, 1 skipped
uv run ruff check . && uv run ruff format --check .
```

Run `pytest` unpiped and without `-q`. `-q` is already in `addopts`, and a
second one suppresses the count — so `uv run pytest -q` is a green run that
never tells you how much it ran.

**Interface** — pnpm 11.1.2, pinned by `packageManager` in `web/package.json`,
so CI and your shell resolve the same one:

```bash
cd web
pnpm install --frozen-lockfile
pnpm test        # 173 tests, 15 files
pnpm typecheck
pnpm lint
pnpm build
```

`web/pnpm-workspace.yaml` carries `allowBuilds: {msw: false}` and it is a gate
answer, not a preference. pnpm 11 refuses to run a dependency's install scripts
until someone has said so in writing, and until then *every* pnpm command exits
1 with `ERR_PNPM_IGNORED_BUILDS` — `pnpm test` included. Delete those two lines
and the whole interface toolchain stops.

**End to end**, and this one is not in CI:

```bash
pnpm exec playwright install chromium   # once per machine
pnpm exec playwright test               # 8 tests in one serial spec, about fifty seconds
```

It boots a real server and two real daemons with virtual panels and drives the
whole story through a browser: set a password, watch a freshly installed rack
ask to join and approve it by its six-character code, pair a second rack with a
token, run the wizard, watch a panel render, edit it, kill the server mid-run
and bring it back. Two of the eight need the server *stopped in the middle*,
which is why the processes belong to `e2e/fixture.ts` and not to a Playwright
`webServer` block.

**The dev loop.** `pnpm dev` in `web/` proxies `/api` and `/ws` to
`127.0.0.1:8080`, so the interface runs hot-reloaded against a real server on
its real socket rather than against mocks.

**The types are generated, never written.** `pnpm generate:types` reads
`/api/openapi.json` from a running server into `src/api/schema.d.ts`, and CI
regenerates and diffs it — so a server change that alters the API fails in the
pipeline instead of in somebody's browser.

---

## Repository layout

```
daemon/            ors-daemon: supervisor, SPI displays, integrations, the link
  examples/        rack.yaml (the author's real rack) and the systemd unit
server/            ors-server: FastAPI app, SQLite store, /ws/daemon and /ws/ui
web/               the SPA; tests/ is Vitest, e2e/ is Playwright + a real stack
packages/
  ors-schema/      the wire contract: scenes, configs, link messages
  ors-render/      the shared renderer: elements, templates, palettes, fonts
  openrackscreen/  the meta-package: no modules, pins ors-daemon and ors-server
deploy/            Dockerfile, two compose files, two overlays (image, mdns)
tools/             version.py: one version across all five distributions
docs/superpowers/  the specs and the milestone plans
```

Tests live beside what they test — `packages/*/tests`, `daemon/tests`,
`server/tests` for Python; `web/tests` and `web/e2e` for the interface.

---

## What is not built yet

Milestone M3c — install and pairing — is complete. Stated plainly, because a
README is the easiest place in a repository to imply otherwise.

**Four things above this line have never been run at all.** They are written as
they are designed to work, and every one of them is untested outside this
repository's own suites:

- **Nothing is on PyPI.** Trusted Publishing is unconfigured, so the first `v*`
  tag cannot publish, so every `uv tool install` line in this file — and the
  `uv pip install` step inside both `install` commands — resolves against an
  index that has never heard of these names. See
  [Nothing is published yet](#nothing-is-published-yet).
- **No image has ever been pushed**, to ghcr.io or anywhere else. No workflow
  builds one, and `deploy/compose.image.yaml` names a tag that answers `denied`.
- **`linux/arm64` has never been built by anyone**, on this machine or in CI.
  Everything this README claims about Docker was verified on `linux/amd64`.
- **No real Raspberry Pi has run `sudo ors-daemon install`.** No system user has
  been created on real hardware, no real `/boot/firmware/config.txt` has been
  edited behind its backup, and nothing has rebooted into an SPI change this
  project made. The installer's whole surface is exercised against injected
  filesystem roots and a fake command runner, which is what makes it testable
  and is exactly not the same as having been run.
- **mDNS has never crossed two machines.** Announce and browse were verified
  against real sockets for the first time while this README was being checked —
  a real server announcing, and `discovery.discover()` returning it — but both
  ends were the same host, and no rack has ever been *paired* over a browse.
  Multicast between two machines, through a switch that may or may not be
  snooping IGMP, is the part still untested, and it is why `--server URL` is
  documented as a first-class path rather than as a workaround.

  That check is also what found the bug that made it not work at all: the
  lifespan called python-zeroconf's synchronous `register_service` from the
  event loop thread, where it deadlocks itself and gives up after ten seconds.
  Every test passed throughout, because every test substitutes the responder.

And the scope that was never in M3c to begin with:

- **Integrations: Prometheus only.** Jellyfin, the \*arr applications,
  qBittorrent and Grafana are M4, each of them a poller in the daemon and a form
  in the interface. `ors-render` ships a `torrent` template with nothing to feed
  it yet.
- **No visual template editor**, and no n8n-style workflow builder. Templates are
  JSON.
- **`sleep`, `wake` and `reload` are not commands.** The server answers `501` and
  names the mechanism that does work instead — a screen sleeps because of the
  rack's night window or its own override, and a paired rack's configuration
  comes from the server, so there is nothing local to re-read. The interface
  deliberately offers no buttons for them; a button that lies is worse than no
  button.
- **`frames_dropped`** reaches `status.json` and stops. There is no exporter.
- **One admin password.** Multi-user accounts, roles and audit trails are out of
  scope, not pending.

The design specs and the per-milestone plans are in `docs/superpowers/`; the M3b
plan's "What M4 picks up" and the M3b spec's "Non-goals" are the authoritative
versions of the scope list above.
