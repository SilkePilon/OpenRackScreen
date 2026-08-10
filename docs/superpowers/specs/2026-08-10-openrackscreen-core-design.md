# OpenRackScreen — Core (Phase 1) Design

**Date:** 2026-08-10
**Status:** Approved for planning
**Scope:** Phase 1 of 4. Later phases have their own specs.

---

## 0. Working rules for whoever implements this

**Research before writing code. Assume nothing.**

Every external interface named in this document must be verified against current
upstream documentation before it is implemented. This document records intent and
architecture; it is not a source of truth for anyone else's API. Where this
document states an endpoint, register value, CLI flag, or library signature, treat
it as a starting hypothesis to confirm, not a fact.

Specifically, look up and confirm (non-exhaustive):

- **shadcn/ui** — current CLI. Use the official commands (`npx shadcn@latest init`,
  `npx shadcn@latest add <component>`) to install and add every component. Do not
  hand-write component source that the registry provides. Confirm the current
  registry name for the sidebar block (`sidebar-04` was chosen as the base) and
  confirm it still ships the floating + collapsible-to-icon variant.
- **Vite + React** — current recommended scaffold, Tailwind version and its
  configuration style (Tailwind v4 config differs substantially from v3).
- **GC9A01** — init sequence, MADCTL (0x36) bit meanings, sleep-in/out (0x10/0x11)
  and display-off/on (0x28/0x29) timing requirements, and whether any read command
  can identify the panel. The existing script's init sequence is known-working on
  this hardware and is the reference; changes to it must be justified.
- **luma.lcd / luma.core** — current `spi()` signature, framebuffer API, and
  whether a maintained GC9A01 device class now exists upstream (the current script
  subclasses `luma.core.device.device` manually).
- **Raspberry Pi SPI** — how SPI1 and its chip-selects are enabled (`dtoverlay`),
  max reliable clock per bus, and GPIO conflicts. The existing wiring
  (SPI0.0/SPI0.1 at 40 MHz, SPI1.0/SPI1.1 at 16 MHz) is known-working.
- **Prometheus HTTP API** — `/api/v1/query` request/response shape, error shapes,
  and the correct handling of `scalar` vs `vector` vs `matrix` result types.
- **qBittorrent Web API v2** — login/session semantics, CSRF/Referer requirements,
  torrent state strings, and the fields returned by `/torrents/info`. State strings
  in particular change between qBittorrent versions — verify against current docs
  rather than copying the hardcoded set from the existing script.
- **kubectl port-forward** — behaviour on connection loss, and whether a
  library-based alternative (e.g. the Kubernetes Python client's portforward) is a
  better fit than shelling out.
- **FastAPI / Starlette** — WebSocket lifecycle, backpressure behaviour, and
  correct patterns for broadcasting to many clients.
- **Cryptography** — current recommended password hashing (argon2id parameters)
  and symmetric encryption (Fernet or equivalent) library versions and defaults.
- **WebP encoding via Pillow** — quality/speed tradeoffs at 240×240, and whether
  it is cheap enough to run on a Pi 3B+ at 2 fps for four screens.

When research contradicts this document, the research wins — raise it, update the
spec, then implement.

**Also:**

- Test-driven. Write the failing test first. See §9.
- Do not assume the reader's hardware matches the author's. Everything except the
  final on-Pi checklist must run in CI with no hardware attached.
- No silent scope growth. §11 lists what Core explicitly does not include.

---

## 1. Problem

A single 500-line Python script (`k8s_monitor.py`) drives four GC9A01 240×240 round
SPI displays mounted horizontally in a server rack. It renders four fixed screens
(CPU, MEM, PODS, HEALTH-or-TORRENT) from Prometheus and qBittorrent, reached
through `kubectl port-forward`, with a hardcoded night-mode window.

Changing anything means editing Python on the Pi over SSH. Screen layout, data
sources, thresholds, colours, sleep schedule and wiring are all constants in the
file. Adding a fifth screen or a second data source means more constants.

## 2. Goal

Turn it into a configurable product: a web interface that configures screens,
templates, schedules and data sources, and a daemon that drives the hardware —
with the daemon optionally on a different machine from the web interface.

Core (this spec) must fully replace the current script. If the rack cannot run
Core in place of `k8s_monitor.py` with the same four screens and the same
behaviour, Core is not done.

## 3. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Project split | 4 phases, Core first | One spec covering daemon + designer + extensions + workflows would rot before it shipped |
| Where work happens | **Fat daemon** — daemon polls sources and renders | Screens keep working when the server is down or being rebuilt; no per-frame network dependency |
| Renderer | Shared Python package used by both daemon and server | Editor previews are pixel-identical to the glass; one engine to maintain |
| Web stack | **Vite + React + shadcn/ui**, static build | No Node process in production; server is a single Python container |
| App shell | shadcn `sidebar-04` — floating, collapsible to icons | Chosen from mockups; rack canvas + right-hand inspector (layout "B") |
| Screen definition | **Declarative scene JSON** | Phase 2's designer and phase 4's workflows both build on it; Python-only templates would dead-end both |
| Link direction | **Daemon dials server** over one WebSocket | Survives DHCP, wifi, NAT, subnet changes; server is the thing with a stable address |
| Pairing | Key generated by daemon installer, pasted into the UI | No account system, no discovery protocol dependency |
| Hardware detection | Probe what's knowable + identify wizard | GC9A01 has no reliable ID read-back; brute-forcing GPIO on a Pi with other HATs is unsafe |
| Config ownership | Server SQLite is authoritative, daemon caches to disk | One thing to back up; daemon still boots and runs alone |
| Live preview | Daemon streams real frames upstream, throttled | Browser shows what is actually on the glass, including bugs |
| UI auth | Single admin password + session cookie | Enough to protect stored cluster and qBittorrent credentials; no user table |
| Schedules | Global night window + per-screen override | Matches current behaviour plus the obvious next need; a rules engine is phase 4 |
| Multi-daemon | Supported from day one | Retrofitting it later touches config, transport, UI routing and workflow scoping |
| K8s access | Direct URL by default, optional kubeconfig tunnel | Works with the author's current cluster and with a normal setup |
| Core data sources | Prometheus + qBittorrent | Two genuinely different shapes (query language vs REST + session auth) force an honest integration interface |
| Deployment | Docker image + compose for server; shell installer + systemd for daemon | Daemon needs bare-metal SPI/GPIO |

## 4. Architecture

Two deployables, three shared packages.

```
┌─ Server host (Docker; Pi, NUC, or cluster) ──────────────────┐
│  SPA (Vite/React/shadcn, static)                             │
│  API server (FastAPI + uvicorn)  ── SQLite                   │
│    imports ors-render for editor previews                    │
└───────────────▲──────────────────────────────────────────────┘
                │  one outbound WebSocket (daemon → server)
                │  down: config snapshot, commands
                │  up:   hello, heartbeat, frames, source status
┌───────────────┴─ Raspberry Pi (systemd) ─────────────────────┐
│  Link client  → persists last config to disk                 │
│  Poller       → per-integration workers                      │
│  Screen workers → resolve bindings → ors-render → SPI        │
│    imports ors-render, ors-schema                            │
└───────────────┬──────────────────────────────────────────────┘
                │ direct HTTP (server never touches the cluster)
        Prometheus            qBittorrent
```

### 4.1 Repository layout

Monorepo.

```
server/                  FastAPI app, migrations, API, static-file serving
daemon/                  link client, poller, screen workers, display drivers
packages/ors-render/     scene engine, built-in templates, golden tests
packages/ors-schema/     pydantic models: scene, config, protocol (shared)
web/                     Vite + React + shadcn SPA
docs/                    this spec, phase specs, hardware notes
deploy/                  Dockerfile, compose.yaml, install.sh
```

`ors-schema` is the contract. Server, daemon and the generated TypeScript types
all derive from it — the SPA's types are generated from the OpenAPI schema, not
hand-maintained.

Python tooling: `uv` for dependency management, `ruff` for lint/format, `pytest`.
Python version pinned to what current Raspberry Pi OS ships — verify before
pinning; do not assume.

### 4.2 Component responsibilities

**`ors-render`** — pure function: `render(scene, context, size) -> PIL.Image`. No
I/O, no network, no hardware, no clock reads except through an injected context.
This makes it fully golden-testable. Supersamples ×2 then downsamples, matching
the current script's quality.

**Daemon — link client.** Holds one WebSocket to the server. Reconnects with
exponential backoff (1s → 30s, jittered). Owns the on-disk config cache. Never
blocks rendering: if the server is unreachable at boot, load cache and run.

**Daemon — poller.** One worker per enabled integration, each on its own interval.
Writes resolved values into a snapshot store (lock-protected dict, same pattern as
the current `Cache`). Marks its integration healthy/unhealthy with a reason.

**Daemon — screen workers.** One thread per screen. Each tick: pick the first scene
whose `when` passes, resolve bindings against the snapshot, render, apply device
rotation/flip, push to the display backend. Skips entirely during night mode.

**Daemon — display backend.** Interface with two implementations: `GC9A01SPI` (the
existing driver, extracted) and `VirtualDisplay` (writes PNGs to a directory).
Backend selection is config, so the whole daemon runs on a laptop.

**Server.** REST for config CRUD, two WebSocket endpoints (`/ws/daemon`,
`/ws/ui`), session auth, static SPA serving, server-side preview rendering for the
template editor, secret encryption.

## 5. Scene model

A **screen** has an ordered list of **scenes**. Each scene may carry a `when`
expression. The first scene whose `when` passes is rendered; a scene with no
`when` is the fallback and must be last.

This is how the current script's "HEALTH becomes TORRENT when the cluster is
healthy and downloads are active" behaviour becomes data instead of code. It is
also the hook phase 4's workflows use to force a screen into a specific scene.

### 5.1 Elements

v1 element types: `ring`, `arc`, `text`, `image`, `rect`, `line`, `sparkline`,
`group`.

Common fields: `cx`, `cy` (normalized 0–1, default 0.5), `when` (optional),
`opacity`, `rotate`.

- **ring** — `r`, `thickness` (both normalized), `value`, `min`, `max`, `palette`,
  `track`, `cap` (`none` | `dot`), `start_angle`, `direction`.
- **arc** — ring without value semantics; explicit `from`/`to` angles.
- **text** — `text` (binding-capable), `size` (px at a 240 baseline), `font`
  (`regular` | `bold`), `color`, `align`, `max_width`, `ellipsis`.
- **image** — `src` (uploaded asset id or binding), `fit`, `w`, `h`.
- **rect** / **line** — primitives for dividers, bars, frames.
- **sparkline** — `values` (binding to a list), `w`, `h`, `palette`, `fill`.
- **group** — `elements[]`, optional `when`, optional `repeat`.

### 5.2 Repeat

`group.repeat` draws the same sub-elements once per item in a list:

```json
{ "type": "group",
  "repeat": { "over": "{{qbit.active}}", "as": "t", "limit": 3 },
  "step":   { "r": -0.125, "thickness": -0.017 },
  "palettes": ["blue", "amber", "violet"],
  "elements": [
    { "type": "ring", "r": 0.858, "thickness": 0.083, "value": "{{t.progress}}" }
  ] }
```

`step` applies a per-iteration delta to numeric fields of the child elements;
`palettes` cycles per iteration. `index` and the alias (`t`) are available to
bindings inside the group. This reproduces the three concentric torrent rings.

### 5.3 Coordinates and scaling

Positions and radii are normalized 0–1 of the panel. Font sizes are px at a 240 px
baseline and are scaled by `size/240` at render. A different panel size therefore
needs no scene edits.

Rotation and horizontal flip are **device** properties on the screen record,
applied by the daemon after rendering. They never appear in a scene, so one scene
renders correctly on any screen regardless of how it is mounted. (The rack's four
panels are mounted horizontally; three of the four currently need `hflip`.)

### 5.4 Bindings

Syntax: `{{namespace.field}}` with an optional filter pipeline,
e.g. `{{prom.cpu | round:0}}%`.

Filters v1: `round:n` · `bytes` · `duration` · `pct` · `trunc:n` · `default:x` ·
`upper` · `lower`.

Namespaces come from integration instances. An integration named `prom` of type
`prometheus` exposes exactly the fields configured on it:

```yaml
cpu:      100 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100
cpu_hot:  topk(1, ...)            # → { node, value }
nodes_ready: sum(kube_node_status_condition{condition="Ready",status="true"})
```

A qBittorrent integration named `qbit` exposes a fixed, documented field set:
`active[]` (each with `name`, `progress`, `eta`, `speed`, `state`), `min_eta`,
`total_speed`, `count`.

Fields are the "placeholder values from external sources" a user picks from when
building a custom screen; the UI shows the live field tree with current values.

An unresolved binding renders its `default` (empty string if none) and flags the
screen amber in the UI. It never raises.

### 5.5 Palettes

Either a named gradient (`cyan`, `green`, `lime`, `amber`, `red`, `violet`,
`blue`, `mono`) with two or more stops, an inline stop list, or threshold-driven:

```json
{ "thresholds": [ {"at": 0, "palette": "green"},
                  {"at": 70, "palette": "amber"},
                  {"at": 90, "palette": "red"} ] }
```

Thresholds let a scene change colour without a conditional element. The token
`@palette` used as a `color` resolves to the current palette's accent, so titles
track the gauge.

### 5.6 `when` expressions

A deliberately small language: comparisons (`== != < <= > >=`), boolean
(`and or not`), `in`, arithmetic (`+ - * /`), literals, dotted field access,
and the functions `len()`, `abs()`, `min()`, `max()`, `round()`.

Implemented as a whitelisted-AST evaluator over Python's `ast` module — parse,
walk, reject any node type not on the allow-list, then evaluate. **No `eval` of
arbitrary source, no attribute access on Python objects, no imports, no calls to
anything not on the function list.** This is a security boundary and is tested as
one (§9).

### 5.7 System scenes

Reserved scene names every screen inherits unless overridden: `stale` (data source
unhealthy — the current script's NO DATA), `connecting` (integration never
succeeded), `error` (render failure, shows a short message), `identify` (giant
ordinal digit used by the setup wizard).

### 5.8 Built-in templates shipped in Core

`ring-gauge`, `big-number`, `multi-ring`, `node-health`, `torrent`, `text-only`,
plus the system scenes. Together these reproduce all four current screens.

Templates declare `params` (e.g. `title`, `value`, `subtitle`, `palette`), and a
screen supplies values for them. Editing a built-in template updates every screen
using it; "Detach" copies it into a user-owned template first.

## 6. Data model (SQLite)

| Table | Columns (beyond id/timestamps) |
|---|---|
| `daemon` | `name`, `key_hash`, `version`, `capabilities` (json), `last_seen`, `status`, `config_version` |
| `screen` | `daemon_id`, `position`, `name`, `spi_bus`, `spi_cs`, `dc_pin`, `rst_pin`, `bus_speed_hz`, `rotation`, `hflip`, `brightness`, `enabled`, `template_id`, `params` (json), `sleep_override` (json, nullable) |

A screen never stores its own scene list. It always points at a template and
supplies that template's params; "Detach" copies the built-in template into a new
user-owned `template` row and repoints the screen at it. There is exactly one
place a scene can live.
| `template` | `name`, `builtin` (bool), `category`, `scenes` (json), `params_schema` (json) |
| `integration` | `daemon_id`, `type`, `name`, `config` (json), `secret_id`, `poll_interval`, `enabled` |
| `secret` | `ciphertext` |
| `setting` | `key`, `value` — admin password hash, timezone, global night window, render interval |
| `daemon_event` | `daemon_id`, `level`, `kind`, `message` — capped ring buffer feeding the UI status panel |
| `asset` | uploaded images/fonts: `filename`, `mime`, `bytes`, `sha256` |

**Secrets.** Encrypted at rest with a key from `ORS_SECRET_KEY`, or generated on
first boot into a `0600` file inside the data volume. Secrets are write-only over
the API: responses redact them, and the SPA never receives a plaintext credential.
They are transmitted to the daemon over the link (which is expected to be a LAN;
TLS is the reverse proxy's job) and stored in the daemon's `0600` config cache.

**Migrations** are versioned and run on server start. The daemon's disk cache
carries a schema version and is discarded (falling back to "wait for server") if
it is from a newer/unknown version.

## 7. Link protocol

One WebSocket per daemon, initiated by the daemon. JSON control messages; frames
sent as binary (msgpack envelope + WebP payload).

**Handshake.** Daemon connects to `/ws/daemon` and sends `hello` containing its
pairing key, hostname, daemon version, protocol version, and capabilities (SPI
buses and chip-selects present, GPIO pins in use/free, display backends available,
platform info). Server verifies the key against `key_hash` (constant-time),
binds the daemon record, and replies with the current config snapshot. A bad key
gets a close frame and a rate-limited retry budget.

**Daemon → server:** `hello` · `heartbeat` (uptime, load, CPU temp, per-screen
last-render time) · `ack{config_version}` · `source_status{integration_id, ok,
error, latency_ms, last_success}` · `frame{screen_id, seq, webp}` · `log{level,
message}`

**Server → daemon:** `config{version, screens[], integrations[], templates[],
settings}` — **always a full snapshot, never a patch**; the daemon applies it
idempotently and acks the version. `command{identify|sleep|wake|reload|probe}` ·
`frames{enabled, screen_ids[], fps}`

**Frame streaming** is off by default. `/ws/ui` clients subscribing to a screen
cause the server to send `frames{enabled:true}`; the last UI unsubscribing turns
it off. Default 2 fps, WebP, ~2–6 KB per frame. Frames are dropped rather than
queued if the link is congested — the newest frame always wins.

**Config apply is atomic on the daemon:** validate the whole snapshot against
`ors-schema`, write to a temp file, `fsync`, `rename` over the cache, then swap
the in-memory config and restart affected workers. A snapshot that fails
validation is nacked with the error and the daemon keeps running the previous one.
**A bad config can never take the rack down.**

## 8. HTTP API and UI

### 8.1 Endpoints

```
POST   /api/auth/login              password → session cookie (HttpOnly, SameSite=Strict)
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/daemons                 list + live status
POST   /api/daemons                 create record, returns nothing secret
POST   /api/daemons/{id}/pair       accept pasted key
POST   /api/daemons/{id}/rotate-key
DELETE /api/daemons/{id}
POST   /api/daemons/{id}/command    identify | sleep | wake | reload | probe

GET/POST/PATCH/DELETE  /api/screens
POST   /api/screens/reorder
GET    /api/screens/{id}/preview    server-side render → PNG

GET/POST/PATCH/DELETE  /api/templates
POST   /api/templates/{id}/preview  render arbitrary scene json + sample context

GET/POST/PATCH/DELETE  /api/integrations
POST   /api/integrations/{id}/test  dry-run: resolves every field, returns live values or errors

GET    /api/settings   PATCH /api/settings
GET    /api/events     daemon event log
GET    /api/backup     export config json      POST /api/restore

WS     /ws/daemon      daemon link
WS     /ws/ui          browser: frames, daemon status, live field values
```

### 8.2 SPA

Vite + React + TypeScript + Tailwind + shadcn/ui. Installed and extended **only**
through the official CLI (`npx shadcn@latest init`, `npx shadcn@latest add ...`).
Base shell from the `sidebar-04` block: floating sidebar, collapsible to icons.

Layout (approved mockup "B"): the horizontal rack of round screens is the canvas;
selecting a disc opens a tabbed inspector on the right (Config / Data / Sleep).
Round screens render as circular live frames, in rack order, matching the physical
horizontal mount.

Pages: **Screens** (rack canvas + inspector, setup wizard) · **Templates** (list,
assign, detach; the visual editor itself is phase 2) · **Integrations** (add,
configure fields, Test) · **Daemons** (pair, status, events, key rotation) ·
**Settings** (password, timezone, night window, backup/restore).

Types for the SPA are generated from the server's OpenAPI schema. State/data
fetching via TanStack Query; live updates via a single `/ws/ui` connection.

### 8.3 Setup flow

1. `install.sh` on the Pi installs the daemon and a systemd unit, generates a
   pairing key, prints it, and asks for the server URL.
2. UI → Daemons → Pair → paste key. The daemon appears online, reporting its SPI
   buses and free GPIO.
3. Add screen: choose bus + chip-select, DC and RST pins. **Identify** renders a
   giant ordinal on that panel immediately, before any other config exists.
4. Repeat per screen, then drag the discs into physical rack order.
5. Integrations → add Prometheus (direct URL, or kubeconfig tunnel) → define
   fields → **Test** shows live values.
6. Assign a template to each screen and bind its params.

## 9. Failure handling

- **Config**: atomic apply, validation before swap, nack on failure, previous
  config retained. (§7)
- **Poller**: failure is scoped to one integration. Exponential backoff, health
  flag with a human-readable reason, surfaced in the UI and in `source_status`.
  Screens depending on an unhealthy source fall to their `stale` scene after a
  configurable number of missed cycles (default 3).
- **Render**: an exception renders the `error` scene on that panel with a short
  message; the worker survives and the error is logged upstream once per distinct
  message (not per frame).
- **SPI**: a write failure re-inits that device up to 3× with backoff; then the
  screen is marked faulted and skipped. Sibling screens are unaffected.
- **Watchdog**: each screen worker publishes a heartbeat; a wedged worker is
  restarted by the daemon supervisor.
- **Link loss**: daemon keeps rendering from cache indefinitely. Server marks the
  daemon offline; edits are still saved and pushed on reconnect.
- **Night mode**: computed in the daemon from the configured timezone (not the
  host's, which is a known foot-gun). Panels get display-off then sleep-in;
  rendering is skipped entirely while asleep.
- **Auth**: argon2id password hash, rate-limited login, constant-time key
  comparison for daemon pairing.

## 10. Testing

Test-driven throughout: failing test first.

- **`ors-render` golden images** — render committed scene JSON against a fixed
  context, compare to committed reference PNGs with a small per-pixel tolerance.
  This is the safety net for the entire project; every element type, palette and
  filter gets a case.
- **Expression evaluator** — unit tests for correctness and for hostile input.
  Must prove that `__import__`, attribute access, subscript-based escapes,
  comprehension abuse and calls to non-whitelisted functions are all rejected at
  parse time.
- **Bindings and filters** — table-driven tests including missing fields, wrong
  types, and `default` behaviour.
- **Protocol** — `ors-schema` round-trip tests, plus compatibility tests for an
  older daemon talking to a newer server (protocol version negotiation).
- **Daemon integration** — `VirtualDisplay` backend + mocked HTTP sources: feed a
  config snapshot, assert the emitted PNGs and the health transitions. Covers
  night mode, stale fallback, scene switching, reconnect, atomic config apply.
  Runs in CI with no hardware.
- **Server** — FastAPI `TestClient` for REST and auth; WebSocket tests for
  pairing (good key, bad key, replayed key), config push idempotency, and frame
  subscribe/unsubscribe lifecycle.
- **Web** — vitest for logic and binding editors; Playwright smoke test covering
  login → pair → add screen → identify.
- **Hardware checklist (manual, on the Pi)** — all four panels init; correct
  rotation/flip per panel; sleep and wake at the night boundary; SPI1 at 16 MHz
  stable; 24-hour soak with no memory growth and no wedged worker; behaviour when
  the server is stopped mid-run.

CI runs everything except the hardware checklist, on every push.

## 11. Non-goals for Core

Named explicitly so they do not leak into the implementation:

- Visual scene designer (phase 2) — Core ships built-in templates and a JSON view,
  not a drag-and-drop editor.
- Extension framework and the Jellyfin, *arr, Grafana and Kubernetes-API
  connectors (phase 3). Core has exactly two integration types, but the
  integration interface is designed for phase 3 to build on.
- Workflow engine (phase 4).
- Multi-user accounts, roles, invites, password reset.
- TLS termination, reverse proxying, external exposure.
- Panels other than GC9A01 240×240. The display-backend interface exists; one
  hardware implementation ships.
- Automatic daemon updates.

## 12. Milestones

| M | Delivers | Done when |
|---|---|---|
| M1 | Monorepo, `ors-schema`, `ors-render` (ring, text, group, repeat, palettes, filters, expressions) | Golden tests pass; virtual backend renders the CPU scene to a PNG in CI |
| M2 | Standalone daemon: local config file, Prometheus poller, screen workers, GC9A01 backend, night mode | Real rack runs from a hand-written config file, no server involved |
| M3 | Server + link: FastAPI, SQLite, migrations, pairing, config push, daemon disk cache | Config edited via API reaches the glass; daemon survives server restart |
| M4 | SPA: shadcn shell, pairing, screen wizard + identify, live frames, rack canvas + inspector | A screen can be added and configured start to finish in the browser |
| M5 | Integrations UI: Prometheus + qBittorrent, field editor, Test, multi-ring torrent template, scene switching | All four original screens reproduced through the UI, including HEALTH↔TORRENT |
| M6 | Docker image, `install.sh`, schedules UI, backup/restore, docs | `k8s_monitor.py` is deleted from the Pi |

**Core is complete when M6's last row is literally true.**

## 13. Open questions for later phases

Recorded here so they are not re-litigated during Core:

- Does the phase 2 designer edit scene JSON directly with a live preview, or a
  constrained widget-based editor? Affects how much the scene schema needs to
  stay human-writable.
- Do phase 3 extensions run in the daemon (consistent with the fat-daemon
  decision) or the server? Third-party code on the Pi has different security
  implications.
- Phase 4 workflows: do they execute in the server (central, needs to reach
  sources) or the daemon (already reaching sources, but N daemons means N
  engines)?
