# Core M3 — Server, Link and Web Interface Design

**Date:** 2026-08-12
**Status:** Approved for planning
**Scope:** Milestone M3 of Core (phase 1). Extends `docs/superpowers/specs/2026-08-10-openrackscreen-core-design.md`, which remains the authority for anything this document does not restate.

**Renumbering.** The Core spec's M3 (server and link) and M4 (web interface) are merged here into one milestone. Pairing a daemon is a browser flow — the seam between them cut through a single user-facing action, and building the pairing API in one milestone to be exercised only in the next meant designing a UX against nothing. What follows shifts: integrations beyond Prometheus become M4, packaging and schedules M5.

---

## 0. Working rules for whoever implements this

**Research before writing code. Assume nothing.** Every external interface named here is a starting hypothesis to confirm against current upstream documentation. Where research contradicts this document, the research wins — raise it, update the spec, then implement.

Confirm before implementing:

- **shadcn/ui** — the current CLI. Install and add every component through it (`npx shadcn@latest init`, `npx shadcn@latest add …`); do not hand-write source the registry provides. Confirm `sidebar-04` still ships the floating, collapsible-to-icon variant chosen in the Core spec's mockups.
- **Vite + React + Tailwind** — the current scaffold and Tailwind's configuration style; v4 differs substantially from v3.
- **FastAPI / Starlette** — WebSocket lifecycle, backpressure, and the correct pattern for broadcasting to many clients. Confirm how a disconnect surfaces, since both sockets must survive one.
- **argon2id** — current recommended parameters, and the library that implements them.
- **Symmetric encryption at rest** — Fernet or its current equivalent, and its key-management guidance.
- **SQLite** — the correct `PRAGMA` settings for a file written by one process and read while a browser polls (WAL in particular), and what `sqlite3` in the shipped Python does with concurrent access from threads.
- **WebP encoding via Pillow** — quality and speed at 240×240, and whether a Pi 3B+ can encode four panels at 2 fps alongside four render threads. Measure before assuming; M2 measured RGB565 packing at 13.9 ms in pure Python and 0.27 ms with numpy, and the same gap may exist here.
- **Docker on ARM** — multi-arch build, and whether the SPA is built in the image or in CI.

**Also:** test-driven, failing test first. No test may sleep to wait for time to pass — the clock is injected on both ends, as it is in M2. Everything runs in CI on x86 with no Pi. §10 lists what M3 excludes.

---

## 1. Problem

M2 delivered a daemon that drives the rack from a hand-written YAML file. Changing anything — a template, a param, a night window, which metric is on which panel — means editing that file over SSH and restarting. Adding a screen means knowing the GPIO pinout. Nothing about the rack is visible except by `cat`-ing a status file.

## 2. Goal

A server and a browser interface that own the configuration, and a link that carries it to the daemon — so the rack is set up and changed from a web page, and the YAML file stops being the source of truth.

M3 is complete when you pair a daemon in the browser, add a screen through the wizard, change its template, and watch the glass change.

## 3. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Milestone shape | **Server, link and interface as one milestone** | Pairing is a browser flow; splitting it meant building an API in one milestone to be used in the next. |
| Frames | **In scope**, streamed on demand | The rack canvas is the interface's centre; a mockup of it would be a guess. |
| Config source | **Server wins; the file is the fallback** | An unpaired daemon runs from its file as it does today; a paired one boots from its cache when the server is down. One code path, and a server outage never darkens the rack. |
| Pairing | **Browser only** — the server mints a token, the daemon claims it | No CLI pairing command ships. The UI is the mechanism, not a wrapper over one. |
| Schema changes | **Export, then rebuild** | No migration framework. On a version bump the server writes config and daemons to JSON beside the database and starts fresh: you re-pair, but nothing you tuned is lost. |
| UI auth | **Single admin password + session cookie** | Enough to stop a device on the LAN owning the rack and reading stored credentials. No user table, no reset flow. |
| Server deployment | **Docker image** | One image: FastAPI, SQLite, prebuilt SPA. Compose files for "all on the Pi" and "server elsewhere". |
| Daemon deployment | **Host systemd, not a container** | It needs `/dev/spidev*` and GPIO. Containerising means four device mappings, `/dev/gpiomem`, and matching numeric GIDs inside the image — a class of failure that starts cleanly and cannot open a panel. An optional container recipe is documented as the harder path. |
| Frame transport | **The existing `/ws/ui` socket, on demand** | One socket for status, health and frames. Nothing is encoded while nobody is watching. |

## 4. Architecture

Three deployables where M2 had one.

```
┌─ Browser ─────────────────────────────────────────────────┐
│  SPA: Vite + React + shadcn, sidebar-04 floating shell    │
│  REST for config · one WebSocket for status and frames    │
└───────────────▲───────────────────────────────────────────┘
                │  /api/*  ·  /ws/ui        (session cookie)
┌───────────────┴─ ors-server (Docker) ─────────────────────┐
│  FastAPI · SQLite · argon2id · secrets encrypted at rest  │
│  link/hub: connected daemons, config push, ack, frames    │
└───────────────▲───────────────────────────────────────────┘
                │  /ws/daemon               (pairing token)
                │  down: config snapshot, commands
                │  up:   hello, heartbeat, ack, status, frames
┌───────────────┴─ ors-daemon (systemd, on the Pi) ─────────┐
│  link client → applies snapshots → the M2 daemon, intact  │
└───────────────────────────────────────────────────────────┘
```

### 4.1 The wire format is the config model

The server assembles a `DaemonConfig` from its rows and pushes exactly that — the same pydantic model the daemon loads from YAML today. The daemon's "apply a snapshot" path and its "load a file" path therefore converge on one validated object.

This is why the config models were put in `ors-schema` in M2 rather than in the daemon, and why hand-authoring that YAML was worth doing: it was the test of whether the shape is usable by a human, before a server had to generate it.

### 4.2 Package layout

```
server/
  pyproject.toml                 ors-server
  src/ors_server/
    __main__.py                  uvicorn entry point
    app.py                       FastAPI app, API routes, SPA static mount
    db.py                        SQLite: schema, version check, export-and-rebuild
    auth.py                      argon2id, session cookie, login rate limit
    secrets.py                   encryption at rest
    api/                         daemons, screens, templates, integrations, settings, auth
    link/
      hub.py                     connected daemons, snapshot push, ack tracking, frame relay
      ws_daemon.py               /ws/daemon
      ws_ui.py                   /ws/ui
  tests/
web/                             Vite + React + shadcn; built into the image
deploy/
  Dockerfile                     multi-stage: SPA build, then the Python image
  compose.pi.yaml                server beside the daemon on the Pi
  compose.remote.yaml            server on another box
packages/ors-schema/             gains link/protocol models
daemon/src/ors_daemon/link.py    the client
```

### 4.3 What changes in the daemon

Three additions and nothing moved:

- `link.py` — a reconnecting WebSocket client, on the same thread discipline as every other worker: an injected clock, a `_stop_event` (never `_stop`, which shadows `threading.Thread._stop`), and a `run` that cannot die.
- An **apply-snapshot** path beside `load_config`, sharing its validation and its atomic write to the on-disk cache.
- A **frame encoder** that runs only while the server has asked for frames.

The supervisor gains one thread. Nothing in the render path, the poller, the tunnel or the screen worker changes.

## 5. The link

### 5.1 Protocol

Defined in `ors-schema` so both ends import one definition. JSON control messages; frames as binary.

**daemon → server:** `hello{token_or_key, hostname, daemon_version, protocol_version, capabilities}` · `heartbeat{uptime, load, cpu_temp, per-screen last render}` · `ack{config_version}` · `nack{config_version, reason}` · `source_status{integration, state, reason, latency_ms}` · `frame{screen_id, seq, webp}` · `log{level, message}`

**server → daemon:** `config{version, snapshot}` — **always whole, never a patch** · `command{identify|sleep|wake|reload}` · `frames{enabled, screen_ids, fps}`

**Three different versions travel here and must not be confused.** `config.version` in a snapshot is the server's *generation counter*, bumped on every change it pushes — this is what an `ack` refers to and what answers "did the Pi apply what I sent?". `DaemonConfig.version` is the config *schema* version, a constant. The daemon's status file also reports a `config_fingerprint`, a content hash, which is what a human compares when they suspect two ends disagree. M2's status file already separates the first two by name; the link must not reintroduce the collision.

A snapshot is applied atomically: validate the whole thing, write the cache, swap, restart affected workers, ack the version. A snapshot that fails validation is nacked with the reason and the previous config keeps running.

### 5.2 Pairing

1. In **Daemons → Add**, the server mints a token, stores only its hash, and shows the token once with the command to run.
2. On the Pi, one command writes that token and the server URL into the daemon's local state and exits — `ors-daemon connect --server <url> --token <token>`, which the browser shows ready to copy.

   Being precise, because this is the one place the "pair in the browser" decision meets reality: the Pi has to learn two facts, and nothing can carry them there but a shell. What is *not* in the terminal is the pairing flow — no prompts, no key generated on the Pi and typed back, no state to reconcile between two places. The server decides who is paired; that command is a one-time bootstrap that writes a file, and it is the only daemon-side step in the whole milestone.
3. The daemon connects, presents the token in `hello`. The server compares in constant time, binds the daemon record, marks the token spent, and replies with the current snapshot.
4. A bad token gets a close frame and a rate-limited retry budget.

A token is single-use: once claimed, that daemon has an identity of its own. Rotating a key from the UI disconnects that daemon until it is re-paired — which is what a rotate button should do.

### 5.3 Frames

Off by default. A browser subscribing to a screen makes the server send `frames{enabled:true}` to that daemon; the last subscriber leaving turns it off. Default 2 fps, WebP, a few KB per frame. Frames are dropped rather than queued when a link is congested — the newest frame always wins, because a stale panel image is worse than a skipped one.

## 6. Data model

SQLite, one file, written only by the server.

| Table | Columns beyond id/timestamps |
|---|---|
| `daemon` | `name`, `token_hash`, `paired_at`, `version`, `capabilities` (json), `last_seen`, `status`, `config_version` |
| `screen` | `daemon_id`, `position`, `name`, `display` (json), `rotation`, `hflip`, `enabled`, `template` (its name), `params` (json), `sleep_override` (json) |
| `template` | `name`, `builtin`, `category`, `scenes` (json), `params_schema` (json) |
| `integration` | `daemon_id`, `type`, `name`, `config` (json), `secret_id`, `poll_interval`, `enabled` |
| `secret` | `ciphertext` |
| `setting` | `key`, `value` — admin password hash, timezone, night window |
| `daemon_event` | `daemon_id`, `level`, `kind`, `message` — capped ring buffer behind the UI's status panel |

**Schema changes.** The database carries a schema version. On a bump the server writes `export-<timestamp>.json` beside it — every table's contents, secrets redacted — then rebuilds empty. You re-pair each daemon; the config you tuned is recoverable from the export rather than retyped. That export is also the beginning of the backup/restore the Core spec wants in M5.

**Secrets** are encrypted at rest with a key from `ORS_SECRET_KEY` or generated on first boot into a `0600` file in the data volume. They are write-only over the API: responses redact them, and the SPA never receives a plaintext credential. They reach the daemon inside the snapshot, over a LAN link; TLS is a reverse proxy's job.

`integration.config` is the one column that carries operator-authored JSON and is exported verbatim, so a credential inline in it — a URL of the form `https://user:pass@host` most plausibly — ends up in a plaintext file beside the database. The integrations API enforces the invariant: credentials become `secret` rows, and a URL carrying userinfo is refused.

## 7. HTTP API and the interface

### 7.1 Endpoints

```
POST   /api/auth/login | /api/auth/logout        GET /api/auth/me
GET    /api/daemons                              POST /api/daemons          (mint a token)
POST   /api/daemons/{id}/rotate-key              DELETE /api/daemons/{id}
POST   /api/daemons/{id}/command                 identify | sleep | wake | reload
GET/POST/PATCH/DELETE  /api/screens              POST /api/screens/reorder
GET    /api/screens/{id}/preview                 server-side render, PNG
GET/POST/PATCH/DELETE  /api/templates
GET/POST/PATCH/DELETE  /api/integrations         POST /api/integrations/{id}/test
GET/PATCH /api/settings                          GET /api/events
WS     /ws/daemon                                WS  /ws/ui
```

Every route and both sockets require the session cookie. A `/ws/ui` that skipped the check would be a live view of the rack for anyone on the LAN. `/ws/ui` is refused at the handshake rather than after a first message: unlike `/ws/daemon`, there is no credential on the wire to fall back on.

#### `/ws/ui`, the browser protocol

Not the link protocol. `ors-schema` is the contract between the server and a daemon, and both ends of that one are pydantic; this one is between the server and a browser, so it is written for what a browser has.

**Browser → server:** `{"action": "subscribe"|"unsubscribe", "screen_id": int}`. Anything else is skipped and logged; the socket stays open. Unknown fields are refused rather than ignored, so an SPA older than the server is told which field it invented.

**Server → browser:**

- `{"type": "daemons", "online": [int, ...]}` — every rack the server is holding a socket for, sorted. Sent once on connect and **pushed again on every change**, so the interface never polls for it. It is also the signal that a panel has stopped for a reason: a rebooted Pi, a pulled cable or a wifi blip all arrive here, and no per-frame event covers those.
- `{"type": "frame", "screen_id": int, "seq": int, "webp": "<base64>"}`.

**The base64 is the standard alphabet, with padding — decode with `atob(message.webp)` and no substitution.** This is deliberately *not* what pydantic emits. `Frame` on the link serialises `bytes` with the **URL-safe** alphabet (`-` and `_`), and `atob` throws `InvalidCharacterError` on both of those characters, so `model_dump_json()` onto this socket produces a payload the interface cannot read. The failure is silent at the Python end — `base64.b64decode` accepts either alphabet unless asked not to — so `ws_ui.py` assembles the message rather than dumping the model, and pins it with a payload whose two encodings differ. If that ever changes back, the SPA needs `.replace(/-/g, '+').replace(/_/g, '/')` first.

**A stalled stream has no event of its own, by decision.** A frame a rack encoded too large is refused by the schema and skipped by `/ws/daemon`; the watcher's queue simply stops and the page keeps its last image. M3a does not add a `stalled` event for it, because an oversized frame is the rarest of the ways a panel goes quiet and an event covering only it would teach the interface that silence otherwise means healthy. The panel component owns staleness: time since the last `frame` for that screen, combined with `daemons`, which covers every case where the rack itself has gone.

**A subscription is not a per-screen toggle at the far end.** `frames{enabled, screen_ids[]}` is whole-daemon state — the daemon *replaces* its streaming set from it — so the server recomputes that daemon's complete watched set, across every open tab, on every subscribe and unsubscribe, and sends it whole. `enabled: false` only when the set is empty. Four tabs on four panels of one rack is the case this exists for.

### 7.2 Pages

Vite + React + TypeScript + Tailwind + shadcn/ui, installed and extended **only** through the official CLI. The shell is the `sidebar-04` block: floating sidebar, collapsible to icons. Types are generated from the server's OpenAPI schema, not hand-maintained. Data via TanStack Query; live updates over one `/ws/ui` connection.

- **Daemons** — pair, status, per-daemon events, key rotation.
- **Screens** — the approved layout: a horizontal rack of round live panels as the canvas, a tabbed inspector on the right (Config / Data / Sleep), and the add-screen wizard with **identify**, which paints an ordinal on the glass so a physical panel can be mapped to a config line. Live panels arrive **before** the mount correction — the daemon streams the rendered frame, not the copy it transposes by `rotation` and `hflip` — so the browser shows a panel the same way up as a person standing at the rack sees it, and the interface must not rotate it again. `rotation` describes how a panel is bolted in (all four in `examples/rack.yaml` are `270`), which is a fact about the rack's carpentry and not about the picture.
- **Templates** — list, assign, detach. Not the visual editor, which is phase 2.
- **Integrations** — add and configure a Prometheus integration, edit its fields, and **Test**, which dry-runs every field and shows live values.
- **Settings** — admin password, timezone, global night window.

## 8. Failure handling

The invariant that shapes this milestone: **the server going away is normal, not exceptional.** The cluster gets rebuilt, the container gets restarted, the wifi drops.

- **Link lost** — the daemon keeps rendering from its cache and reconnects with backoff. Panels never go dark because a server did.
- **Invalid snapshot** — nacked with the reason; the previous config keeps running.
- **Server restart** — daemons reconnect and re-ack their version. Nothing is re-pushed if the versions already match.
- **Browser socket lost** — reconnects and refetches state. Frame subscriptions are re-established rather than assumed.
- **Daemon offline** — the UI shows it offline with its last-known status; edits are saved and pushed on reconnect.
- **Unwritable database** — the server reports it and refuses writes rather than pretending; the rack keeps running on its cache regardless.

## 9. Testing

Test-driven throughout. No test may sleep; the clock is injected on both ends. Everything runs in CI on x86 with no hardware.

| Area | Approach |
|---|---|
| **The link** | The seam most likely to bite, so it is tested as a seam: a real server and a real daemon client over an in-process WebSocket — pairing (good token, bad token, replayed token), snapshot push and ack, nack on an invalid snapshot, reconnect, and the offline-cache path |
| API | `TestClient` for every route; auth required on each; rate-limited login |
| Database | Round-trip per table; the export-then-rebuild path, asserting the export is complete and secrets are redacted |
| Secrets | Encrypted at rest; redacted in every response shape |
| Frames | Lifecycle: nothing is encoded while nobody watches; subscribe starts it; the last unsubscribe stops it; a congested link drops rather than queues |
| Daemon | Snapshot apply is atomic; a bad snapshot leaves the previous config running; the cache boots a daemon with no server |
| Web | vitest for logic; Playwright over log in → pair → add a screen → see a frame |
| Docker | The image builds and starts; the SPA is served; the database persists across a container restart |

## 10. Non-goals for M3

- The visual scene designer (phase 2)
- The workflow engine (phase 4)
- Integrations beyond Prometheus (M4)
- Multi-user accounts, roles, password reset
- TLS termination, reverse proxying, exposure beyond the LAN
- A daemon container as the supported path — documented, not default
- Automatic daemon updates

## 11. Definition of done for M3

- `uv run pytest` passes from a clean checkout with no hardware; ruff clean; the web build and its tests pass; CI green.
- `docker compose up` brings the server up, serves the SPA, and persists its database across a restart.
- You pair a daemon **in the browser**, and it appears online with its capabilities.
- You add a screen through the wizard, use **identify** to find its panel, assign a template, and the glass changes.
- The rack canvas shows live frames from all four panels, and nothing is encoded while the tab is closed.
- Stopping the server leaves the rack running; restarting it reconnects with no re-push.
- **The YAML file is no longer the source of truth for the paired daemon.**

## 12. What M4 picks up

M4 (integrations beyond Prometheus) consumes: the integration API and its `test` endpoint; the secret storage this milestone builds; and the `Integration` contract from M2, which was written as a pure fetcher precisely so a second one is a class and a config model rather than a new loop.
