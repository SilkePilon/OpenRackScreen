# OpenRackScreen

Four round 240×240 GC9A01 panels bolted into a server rack, driven from a
Raspberry Pi over SPI, drawing whatever a scene description says to draw. The
scenes are declarative JSON, the data comes from Prometheus, and the whole thing
is configured from a web interface rather than by editing YAML over SSH.

Three pieces, and they deploy differently on purpose:

| | what it is | where it runs |
| --- | --- | --- |
| **`daemon/`** | a thread per screen, driving the panels over SPI | a systemd unit on the Pi wired to the panels |
| **`server/`** | FastAPI + SQLite: owns the configuration, talks to each daemon over a WebSocket, serves the interface | a Docker container, on the Pi or anywhere on the LAN |
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

**Bring up the server with its interface.** Two environment variables, and both
of them matter more than they look:

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
ORS_DATA_DIR=/tmp/ors-data ORS_WEB_DIR=web/dist uv run ors-server
```

Then open <http://127.0.0.1:8080/> and set an admin password.

- **`ORS_DATA_DIR` defaults to `/var/lib/openrackscreen`**, which is the path
  inside the container. On a laptop the server dies at startup with
  `PermissionError: [Errno 13] Permission denied: '/var/lib/openrackscreen'`.
- **`ORS_WEB_DIR` defaults to `/app/web`**, which is also a container path.
  Leave it unset in a checkout and the server starts perfectly happily, answers
  `/api/health`, and returns **404 for every page** — because there is no
  interface where it looked. It says so once, at startup, and names the variable:

  ```
  no interface to serve: /app/web holds no index.html. Run `pnpm build` in web/, or point ORS_WEB_DIR at a build.
  ```

  This is the single most likely thing to go wrong on a first run from source.
  If the API works and the site is a 404, that line is in the log.

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

### Docker

One image, built from the repository root, serving the API and the interface on
one port from one origin:

```bash
docker build -f deploy/Dockerfile -t openrackscreen .
docker compose -f deploy/compose.pi.yaml up -d      # server on the Pi itself
docker compose -f deploy/compose.remote.yaml up -d  # server elsewhere on the LAN
```

Nothing needs installing or building first; the image builds the interface in a
Node stage and then discards it, so the ~288 MB that ships has no Node, no pnpm
and no `node_modules` in it. Both compose files publish `8080` and keep the
database in a named volume.

**Add `--build` when you are running from a checkout.** Both compose files name
`image: ghcr.io/silkepilon/openrackscreen:latest` as well as a `build:` block,
and compose prefers the image: if any copy of that tag is already on the machine
it is used as-is and your working tree is never built. On this machine a
two-day-old copy of that tag was picked up in exactly that way, and it predated
the interface being in the image — so the container came up, passed its
healthcheck, answered `/api/health`, and returned 404 for every page.
`docker compose -f deploy/compose.pi.yaml up -d --build` builds the checkout and
serves the interface.

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

The server is `uv run ors-server` with `ORS_DATA_DIR` and `ORS_WEB_DIR` set, as
above. The daemon is a systemd unit rather than a container — it needs four
`/dev/spidev*` nodes, `/dev/gpiochip*`, and the `spi` and `gpio` groups, and
containerising that buys nothing. `daemon/examples/openrackscreen.service` is the
unit; `daemon/README.md` owns the SPI wiring, enabling both buses in
`config.txt`, the `hardware` extra, and all five daemon commands.

---

## First run

1. **Set an admin password.** There is no default account. Until a password
   exists every configuration route answers `401`, including from `curl`, so a
   half-finished deployment cannot be paired against by accident.
2. **Pair the rack.** On the Daemons page, add a rack. The server mints a token
   and shows it **exactly once**, beside the line to paste on the Pi:

   ```bash
   sudo -u openrackscreen ors-daemon connect --server http://<host>:8080 --token <token>
   ```

   As the daemon's own user — `sudo ors-daemon connect` writes the pairing
   root-owned and the daemon then cannot read it. Losing a token costs a rotate,
   not a reinstall.
3. **Add screens.** The wizard runs **detect → wiring → probe → add**.

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
uv run pytest                                    # 2177 passed, 1 skipped
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
pnpm exec playwright test               # 8 specs, about three quarters of a minute
```

It boots a real server and two real daemons with virtual panels and drives the
whole story through a browser: set a password, watch a freshly installed rack
ask to join and approve it by its six-character code, pair a second rack with a
token, run the wizard, watch a panel render, edit it, kill the server mid-run
and bring it back. Two of the eight specs need the server *stopped in the
middle*, which is why the processes belong to `e2e/fixture.ts` and not to a
Playwright `webServer` block.

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
deploy/            Dockerfile and the two compose files
docs/superpowers/  the specs and the milestone plans
```

Tests live beside what they test — `packages/*/tests`, `daemon/tests`,
`server/tests` for Python; `web/tests` and `web/e2e` for the interface.

---

## What is not built yet

Milestone M3b — the interface — is complete. Stated plainly, because a README is
the easiest place in a repository to imply otherwise:

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
versions of the list above.
