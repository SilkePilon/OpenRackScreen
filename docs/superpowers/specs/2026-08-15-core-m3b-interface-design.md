# Core M3b — The Web Interface

Status: approved, not yet implemented
Depends on: M3a (merged at `62fe271`), M2, M1

## 0. Working rules for whoever implements this

The same rules that governed M3a, because they are what made it survive contact with the rack.

- **Research before implementing.** Every library named here has a current version with current documentation. Read it. Where the documentation contradicts this spec, the documentation wins — raise it, then implement. M3a's plan was wrong about pydantic's `bytes` serialisation, about DST, about `proxy_headers`, and about SQLite's `RETURNING` bound; in each case the implementer checked and was right.
- **TDD.** Failing test first, watch it fail for the expected reason, minimal implementation, watch it pass, commit.
- **Verify by exit code**, never by the tail of the output. A red commit shipped once in M2 because a pipe swallowed the status.
- **Mutation-test your own work** before handing it over, and disclose survivors. Every M3a task that claimed none had some; the four that disclosed their own were the four that shipped soundest.
- **The API is fixed.** M3b consumes M3a as built. The one deliberate exception is §6, the detection protocol, which the user asked for explicitly and which M3a does not provide. Everything else: if the interface cannot be built against the API as it stands, that is a signal the API was wrong, and it should be raised rather than patched around.

## 1. Problem

M3a produced a server that owns a rack's configuration, a daemon that applies it, and two websockets. Nothing renders any of it. The rack is configured by editing YAML on the Pi and the server is driven by `curl`.

## 2. Goal

A web interface that pairs a daemon, configures screens against live panels, and shows what the rack is actually drawing — usable from a desktop browser, reachable from a phone, and shipped in the same container as the API.

## 3. Decisions taken

| Decision | Choice | Why |
| --- | --- | --- |
| Framework | Vite + React 19 + TypeScript | Settled in M3's design; smallest toolchain that shadcn supports first-class |
| Components | shadcn/ui via the official CLI only | The user's requirement, stated twice |
| Shell | `sidebar-04` — floating, collapsible to icons | The user's requirement |
| Rack canvas | Horizontal row of round live panels | The approved mockup, layout B |
| Theme | Both, shadcn's Vite dark-mode recipe, dark by default | The user chose both and named the recipe |
| Devices | Desktop first, usable on a phone | One layout with breakpoints, not two layouts |
| Scope | All five pages in one milestone | The user chose a usable product over an earlier partial one |
| Test depth | Component tests **and** end-to-end against a real server | The base64 trap proves a mocked contract is not a contract |
| Detection | Enumerate SPI devices, then guided probe | The user asked for detection; the hardware cannot introduce itself (§6) |
| Package manager | pnpm | Lockfile determinism; no bearing on the shipped image beyond the build stage |

## 4. Architecture

### 4.1 Layout

```
web/                          new, not a uv workspace member
  src/
    api/          generated schema.d.ts, typed client, query hooks
    live/         the /ws/ui connection, the daemons cache bridge, the frame store
    components/   shadcn components (CLI-managed) and app components
    routes/       one directory per page
    theme/        ThemeProvider, ModeToggle
  tests/          Vitest component and hook tests
  e2e/            Playwright specs and the real-stack fixture
```

`e2e/` sits inside `web/` rather than at the repo root, because it is driven by `pnpm` and shares the interface's TypeScript configuration. The fixture it boots is a Python server and a Python daemon; that is a subprocess boundary, not a package one.

`web/` is TypeScript and does not enter the `uv` workspace. The Python packages do not import it and it does not import them; the only contract between them is the OpenAPI document and the `/ws/ui` protocol.

### 4.2 Serving and building

FastAPI mounts the built assets at `/`, below every existing route. One container, one port, one origin — the session cookie needs no CORS and `/ws/ui` needs no second host. `/api/*` and the two sockets keep their paths; anything else falls through to `index.html` so client-side routes survive a reload.

The Dockerfile gains the Node stage task 14 deliberately did not build against a placeholder: `pnpm install --frozen-lockfile`, `pnpm build`, and the output copied into the Python image. The runtime stage still ships no Node.

In development, Vite proxies `/api` and `/ws` to `:8080`, so the interface runs hot-reloaded against a real server.

### 4.3 Types are generated, never written

`openapi-typescript` generates `web/src/api/schema.d.ts` from `/api/openapi.json`. `openapi-fetch` provides the typed client. A CI step regenerates and fails on any diff, so a server change that breaks the interface fails in the server's own pipeline instead of at runtime in a browser.

This is why M3a gave every route an explicit response model, including the ones whose body is a single field: a route returning a bare `dict` generates as `object` and would have to be typed by hand.

### 4.4 State: three stores, deliberately separate

**TanStack Query** owns everything fetched over HTTP. Mutations invalidate the queries they affect. Nothing polls — `GET /api/daemons` in particular assembles a snapshot per rack (measured at 9.9 ms for four racks, on the event loop), so polling it would spend the server's frame-relay time on a list that changes only when something happens.

**One `/ws/ui` connection**, owned by a provider at the app root. The `daemons` message writes straight into the Query cache with `setQueryData`, so online dots update without a refetch.

**A frame store outside React.** Panels subscribe by screen id and receive an `ImageBitmap`; the panel component draws it to its own canvas through a ref. No component above a panel re-renders when a frame arrives. Four panels at 2 fps is eight frames a second — through React state, that is eight renders a second of the page that owns them, forever, while a tab is open.

### 4.5 Painting a panel

`createImageBitmap(blob)` decodes off the main thread; `drawImage` puts it on a per-panel canvas clipped to a circle. Each bitmap is `close()`d after drawing.

The alternatives were considered and rejected: `<img src={objectURL}>` needs disciplined revocation or it leaks and gives no clean circular clip; a data URL allocates a fresh base64 string eight times a second forever.

**The base64 on this socket is the standard alphabet, with padding.** `atob(message.webp)` with no substitution. This is deliberately not what pydantic emits — `Frame` on the *link* uses the URL-safe alphabet, and `atob` throws on `-` and `_`. M3a's `ws_ui.py` assembles the message rather than dumping the model, and pins it with a payload whose two encodings differ in every character. Do not "simplify" it back.

### 4.6 Theme

shadcn's Vite recipe as published: `components/theme-provider.tsx` exposing `ThemeProvider` (`defaultTheme`, `storageKey`) and a `useTheme` hook, the theme persisted in `localStorage`, applied by swapping `light`/`dark` on the document root, with `system` resolved through `matchMedia("(prefers-color-scheme: dark)")`. `components/mode-toggle.tsx` is the published dropdown with Sun and Moon from lucide-react.

`defaultTheme` is `"dark"` — the approved mockups are dark and a rack dashboard usually sits on a second monitor.

### 4.7 Routing

React Router. `/setup`, `/login`, `/daemons`, `/screens`, `/templates`, `/integrations`, `/settings`. A guard sends an unconfigured server to `/setup` and an unauthenticated session to `/login`; the server already distinguishes those two states, so the interface does not guess.

## 5. Pages

### 5.1 Shell

`sidebar-04`: floating sidebar collapsible to icons, five nav items, footer carrying the mode toggle and sign-out. The header carries a rack-status strip — one dot per rack, fed by the `daemons` message, which is the only thing that reports a Pi has gone. Below the `md` breakpoint the sidebar becomes a sheet.

### 5.2 Setup and login

A first run has no admin password. `/setup` claims it; the server's claim is atomic (`ON CONFLICT DO NOTHING`), so a second browser racing it is refused rather than silently overwriting. Afterwards, `/login`. Any 401 from any request returns to `/login` once, without a redirect loop.

### 5.3 Daemons

Each rack shows status, online dot, `config_version`, `applied_version`, and — when those differ, or `config_error` is set — what is wrong, in a sentence.

Actions:
- **Pair.** Mints a token, shows it **exactly once**, beside the `ors-daemon connect` line to paste on the Pi. Losing it means rotating again; the interface says so at the moment it shows it.
- **Rotate key.** Same once-only display. Explains that the rack is unpaired until the new token reaches it.
- **Delete.** Names what cascades: the rack's screens go with it.
- **Push configuration now.** Reports `delivered` as the server reports it — `true` only when a snapshot really left. A connected rack with no servable configuration answers `delivered: false`, and the interface must not translate "online" into "it got it".

Recent events from `GET /api/events?daemon_id={id}&limit=`. The ring holds 200 per rack and a flapping rack spends two per reconnect, so the panel is labelled *recent events*, not history, and must not assume an event it saw earlier is still there.

### 5.4 Screens

The approved layout: a horizontal rack of round live panels as the canvas, selection by click, and a tabbed inspector on the right.

**Panels are drawn exactly as received.** No rotation, no flip. `rotation` and `hflip` describe how a panel is bolted into the rack — all four of the user's are `270` — and the daemon streams the frame *before* it applies them, so the browser already shows what a person standing at the rack sees. Rotating again would be wrong twice.

**Staleness is the panel's own business.** M3a deliberately ships no stalled-stream event, because an oversized frame is the rarest way a panel goes quiet and an event covering only that case would teach the interface that silence otherwise means healthy. A panel is stale when no frame has arrived for it recently, and offline when `daemons` says its rack is gone — which covers the reboot, the pulled cable and the wifi blip.

**Frames flow only while someone is watching.** Subscribe on mount, unsubscribe on unmount and on tab-hide. A closed tab that kept its subscription would leave the Pi encoding WebP for nobody.

Inspector tabs: **Config** (name, position, template, wiring), **Data** (the template's params bound to integration fields), **Sleep** (per-screen override against the global night window). Reordering writes through `POST /api/screens/reorder`.

### 5.5 The add-screen wizard

Detect → confirm → probe → add. See §6 for the protocol.

1. **Detect.** The daemon reports which SPI devices exist and which its running configuration already claims.
2. **Confirm wiring.** For each free device, DC and RST are asked for, pre-filled from the rack's existing screens — they are wiring choices and nothing on the bus reports them.
3. **Probe.** The daemon lights that one panel with a test pattern and holds it; you confirm visually.
4. **Add.** The screen is created with the wiring just proved.

`identify` remains available afterwards from the Screens page, for re-checking the order without adding anything.

### 5.6 Templates

List, preview, assign, detach. The preview is the server-rendered PNG from `GET /api/screens/{id}/preview` and is **not mount-corrected**, exactly like the live frames. The visual editor is phase 2 and is not in this milestone.

### 5.7 Integrations

Add and edit Prometheus integrations, edit their fields, and **Test**.

Test is labelled a **reachability check**, not a preview: M3a reports the first sample of each query and deliberately does not reimplement the daemon poller's `reduce`, `label` and `strip`, so the number shown is not the number the panel will show.

`credential` is write-only — a field going in, `has_credential` coming back, never a value. An empty string clears it. An *enabled* integration may not hold one in M3a and the API says so; M4 is what gives a credential somewhere to go.

### 5.8 Settings

Admin password, timezone, global night window.

### 5.9 Everywhere: saved but not pushed

A mutation that could not be given to every rack it affects says so, and the interface reads **the `X-Unservable-Daemons` header**, not the status code.

- The header is present on `200`, `201` and `202` alike, and carries the daemon ids, ascending.
- `202` means *no* affected rack got it — per edit, not per server.
- A route that declares its own status keeps it: `POST /api/screens` answers `201` even when nothing was pushed, so an interface checking only for `202` misses the create case.
- The reason per rack is `config_error` on `GET /api/daemons`, and the recovery is usually the next edit plus **push configuration now**.

## 6. The detection protocol — the one new server and daemon surface

M3a has no way to ask a rack what hardware it has. The user asked for detection explicitly, so M3b adds it. This is the only place M3b extends M3a rather than consuming it.

### 6.1 What is discoverable, and what is not

The Pi can enumerate `/dev/spidev*` — which buses and chip selects exist. It cannot discover DC and RST: those are GPIO pins chosen when the panel was wired, nothing on the bus reports them, and a GC9A01 has no readable ID over 4-wire SPI. A panel cannot introduce itself.

So detection is **enumeration plus a guided probe**: the daemon says what devices exist and which it is already driving; the operator supplies the wiring; the daemon proves it by lighting that panel.

### 6.2 Shape

The link is push-based and commands are fire-and-forget, so this adds a correlated request/reply:

- `DetectRequest{request_id}` → `DetectResult{request_id, panels[{bus, cs, claimed_by}]}`
- `ProbeRequest{request_id, bus, cs, dc, rst, hz, hold_s}` → `ProbeResult{request_id, ok, error?}`

`claimed_by` is the name of the screen currently driving that device, or null when it is free. The probe paints one fixed pattern chosen by the daemon — a large ordinal on a coloured field, the same thing `identify` draws — rather than taking a pattern from the wire, because a pattern the server picks is a rendering instruction the daemon would have to validate, and there is nothing the operator needs to choose. `hold_s` is bounded by the daemon.

The server holds a bounded wait keyed by `request_id`, so `POST /api/daemons/{id}/detect` and `/probe` answer with the result or time out honestly. The wait is bounded by the same reasoning as `SEND_TIMEOUT`: a rack that has stopped answering must not hold an HTTP handler open.

This is one new pattern, and it is the shape every later "ask the rack something" feature needs.

### 6.3 The constraint that matters

**A probe takes the same bus guard `apply` uses.** Lighting a candidate panel while a bus-mate is mid-frame is exactly the interleaving that produced the pale grey rectangles in M2 — the failure that cost the most debugging in this project — and the wizard would be doing it deliberately. A probe must:

- refuse a device the running configuration already claims, rather than fighting a live worker for it;
- hold every worker on that bus off the bus for the probe, exactly as `apply` does, with the same per-screen bound;
- close the device afterwards, so a probed panel does not stay claimed.

### 6.4 Rate

Both endpoints are session-guarded and mutate nothing. Detection is cheap; a probe is a real panel init (~160 ms plus a frame) and holds a bus. One probe at a time per rack.

## 7. Failure handling

The interface inherits M3a's invariant — *no failure of the server, the link, or the network may darken the rack* — and adds two of its own: **nothing the browser does may slow the panels down**, and **no server state is invented by the client.**

| Failure | Behaviour |
| --- | --- |
| `/ws/ui` drops | Reconnect with capped backoff; re-subscribe the panels still on screen. Panels keep their last frame marked stale — blank would read as a dead rack |
| A rack goes offline | The `daemons` message is the signal; panels are marked offline. Never inferred from frames stopping |
| A mutation fails | Invalidate and refetch. No optimistic patching: M3a can accept an edit, save it, and not push it, and an optimistic UI would show it applied |
| Session expires | Any 401 routes to `/login` once, without a loop |
| A frame for an unwatched screen | Dropped |
| A backwards `seq` | The daemon restarted; the panel resets rather than treating old frames as out of order |
| The server is gone entirely | The interface says so plainly. The rack is still rendering, and the interface must not imply otherwise |

## 8. Testing

Three layers, each catching what the layer below cannot.

**Vitest + React Testing Library** for components, hooks and the socket client, with MSW at the network boundary. This is where the frame store, the staleness rule, the header parsing and the reconnect logic are pinned.

**Playwright against a real stack** — a real `ors-server` and a real daemon with `virtual` panels, booted by a fixture. This is the layer that would have caught the base64 trap, and the only one that can prove a pushed edit reaches a rendered panel.

**Contract tests** — the generated types must match the running server's OpenAPI document, and the end-to-end fixture asserts the `X-Unservable-Daemons` and `delivered` semantics against the real server rather than a mock.

Mutation testing applies to the code that has logic — the socket client, the frame store, the header parsing, the staleness rule — and not to JSX. Disclose survivors.

**Two fixture traps this project has already hit, both of which apply here:**
- An **identity fixture**, where a value coincides with another so a mix-up is invisible. Screen id 1 at position 1 hid a real bug in M3a task 11; contiguous ids from 1 hid two more in task 12. Screen ids, positions and array indices must differ in every fixture.
- A **test that signals through an exception the production code catches**. One shipped in task 9 and passed against broken code.

## 9. Definition of done

1. `pnpm test`, `pnpm build`, `uv run pytest` and ruff all pass. The generated types match the server's schema.
2. From a clean database: set a password, pair a daemon, add four screens through the wizard using detect and probe, and see all four rendering live.
3. An edit made in the interface reaches the glass.
4. Stopping the server leaves the rack rendering, the interface says so honestly, and it recovers when the server returns.
5. `docker build` produces one image serving both the API and the interface.
6. Playwright passes against the real stack.

## 10. Non-goals for M3b

- The visual template editor (phase 2).
- The n8n-style workflow builder.
- Jellyfin, the \*arr applications, qBittorrent and Grafana integrations — M4.
- Multi-user accounts, roles, audit trails. One admin password, as M3a built.
- Serving the interface from anywhere but the API's own origin.

## 11. What M4 picks up

- The integrations above, each of which is a poller in the daemon and a form here.
- `frames_dropped`, which reaches `status.json` and stops: `Heartbeat.status` is still sent empty and there is no exporter. Whichever of M3b or M4 builds one first owns that key.
- Whether `sleep`, `wake` and `reload` become real commands. M3a answers `501` and names the working mechanism instead; the interface must not offer buttons for them until they exist.
- Cross-building the ARM image, which nothing has done — the 32-bit compile path has never run.
