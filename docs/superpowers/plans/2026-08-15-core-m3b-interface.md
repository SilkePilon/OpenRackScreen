# Core M3b — The Web Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A web interface that pairs a daemon, configures screens against live panels, and shows what the rack is actually drawing — shipped in the same container as the API.

**Architecture:** A Vite + React + TypeScript SPA in `web/`, served by the existing FastAPI app at `/` so there is one origin, one port and one cookie. Types are generated from the server's OpenAPI document and never hand-written. Three separate stores: TanStack Query for everything over HTTP, one `/ws/ui` connection whose `daemons` message writes into that cache, and a frame store deliberately outside React so eight frames a second do not re-render the page. The one place this milestone extends the server rather than consuming it is the detection protocol (tasks 9–12), because nothing in M3a can ask a rack what hardware it has.

**Tech Stack:** Vite 6, React 19, TypeScript 5, Tailwind 4, shadcn/ui (CLI only), TanStack Query 5, React Router 7, `openapi-typescript` + `openapi-fetch`, Vitest + React Testing Library + MSW, Playwright. Server side: the existing FastAPI/pydantic/SQLite stack.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-15-core-m3b-interface-design.md`. Where this plan and the spec disagree, the spec wins; where current library documentation and the spec disagree, **the documentation wins** — raise it, then implement.
- **Research before implementing.** Every library here has a current version with current docs. Read them. M3a's plan was wrong about pydantic's `bytes` serialisation, about DST, about uvicorn's `proxy_headers` default and about SQLite's `RETURNING` version bound; each time the implementer checked and was right.
- **TDD.** Failing test first, watch it fail *for the expected reason*, minimal implementation, watch it pass, commit.
- **Verify by exit code**, never by the tail of the output: `pnpm test; echo "exit=$?"` and `uv run pytest -q; echo "exit=$?"`. A red commit shipped once in M2 because a pipe swallowed the status.
- **Mutation-test your own work** before handing it over, on the code that has logic — the socket client, the frame store, the header parsing, the staleness rule, and every Python change. Not JSX. **Disclose survivors.** Every M3a task that claimed none had some.
- Mutation runs: sequential, nothing backgrounded, `PYTHONDONTWRITEBYTECODE=1` for Python, clear only the source trees' `__pycache__` (never `.venv`), `git checkout --` to restore, assert the tree is clean between mutants.
- **No test may sleep to wait for time to pass**, bind a fixed port, or touch SPI. Injected clocks and fake timers.
- **shadcn components are added through the CLI only** — `pnpm dlx shadcn@latest add <component>`. Never hand-written into `components/ui`.
- **The interface must not rotate a panel image.** Live frames and previews arrive before the mount correction by design.
- **`ors-server` must not import `ors-daemon`**, and no Python package may import `ors-server`.
- Nothing may be named `_stop` in Python — it shadows `threading.Thread._stop` and every `join()` then raises.
- Python: `uv run ruff check . && uv run ruff format --check .` must pass. TypeScript: `pnpm lint` and `pnpm typecheck` must pass. There is no mypy in this repo.
- **Two fixture traps this project has already hit.** An **identity fixture**, where one value coincides with another so a mix-up is invisible — screen id 1 at position 1 hid a real bug, and contiguous ids from 1 hid two more. Screen ids, positions and array indices must differ in every fixture. And a **test that signals through an exception the production code catches**, which shipped once and passed against broken code.

## File Structure

```
web/
  package.json, pnpm-lock.yaml, vite.config.ts, tsconfig.json
  index.html
  src/
    main.tsx                app entry, providers
    App.tsx                 router
    theme/                  ThemeProvider, ModeToggle          (shadcn recipe)
    api/
      schema.d.ts           GENERATED from /api/openapi.json — never edited
      client.ts             openapi-fetch client + the mutation wrapper
      unservable.ts         X-Unservable-Daemons parsing
      queries.ts            TanStack Query hooks, one per resource
    live/
      socket.ts             the /ws/ui connection, reconnect, message parsing
      frames.ts             the frame store — outside React
      LiveProvider.tsx      owns the socket, bridges `daemons` into Query
    components/
      ui/                   shadcn CLI output
      AppShell.tsx          sidebar-04 shell + rack status strip
      Panel.tsx             one round live panel, canvas + staleness
    routes/
      setup/  login/  daemons/  screens/  templates/  integrations/  settings/
  tests/                    Vitest
  e2e/                      Playwright specs + the real-stack fixture

packages/ors-schema/src/ors_schema/link.py     + detect/probe messages
server/src/ors_server/link/hub.py              + correlated request/reply
server/src/ors_server/api/daemons.py           + /detect and /probe
server/src/ors_server/app.py                   + the SPA mount
daemon/src/ors_daemon/hardware.py              NEW — enumerate and probe
daemon/src/ors_daemon/link.py                  + on_detect / on_probe
deploy/Dockerfile                              + the Node stage
```

---

### Task 1: The web workspace, the shell, and the theme

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/theme/theme-provider.tsx`, `web/src/theme/mode-toggle.tsx`, `web/src/components/AppShell.tsx`, `web/vitest.config.ts`, `web/tests/shell.test.tsx`
- Modify: `.gitignore` (add `web/node_modules`, `web/dist`)

**Interfaces:**
- Produces: `AppShell({children})`, `ThemeProvider({children, defaultTheme, storageKey})`, `useTheme() -> {theme, setTheme}`, and a working `pnpm dev`, `pnpm build`, `pnpm test`, `pnpm lint`, `pnpm typecheck`.

**Read first:** the shadcn Vite installation guide and the dark-mode Vite guide, both current. Use the CLI for every component; `sidebar-04` is a block, added with `pnpm dlx shadcn@latest add sidebar-04`.

- [ ] **Step 1: Scaffold, through the official tools only**

```bash
cd web
pnpm create vite@latest . --template react-ts
pnpm install
pnpm add -D tailwindcss @tailwindcss/vite
pnpm dlx shadcn@latest init
pnpm dlx shadcn@latest add sidebar-04 dropdown-menu button
pnpm add -D vitest @testing-library/react @testing-library/user-event @testing-library/jest-dom jsdom
```

Configure the Vite dev proxy so the interface runs against a real server:

```ts
// web/vite.config.ts
server: {
  proxy: {
    "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
    "/ws": { target: "ws://127.0.0.1:8080", ws: true },
  },
},
```

- [ ] **Step 2: Write the failing test**

`web/tests/shell.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach } from "vitest";
import { AppShell } from "../src/components/AppShell";
import { ThemeProvider } from "../src/theme/theme-provider";

function shell() {
  return render(
    <ThemeProvider defaultTheme="dark" storageKey="ors-theme">
      <AppShell>
        <p>rack</p>
      </AppShell>
    </ThemeProvider>,
  );
}

describe("the shell", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("light", "dark");
  });

  it("names every page the interface has", () => {
    shell();
    for (const page of ["Daemons", "Screens", "Templates", "Integrations", "Settings"]) {
      expect(screen.getByRole("link", { name: page })).toBeInTheDocument();
    }
  });

  it("starts dark, because that is what the design chose", () => {
    shell();
    expect(document.documentElement).toHaveClass("dark");
  });

  it("remembers a theme across a reload", async () => {
    shell();
    await userEvent.click(screen.getByRole("button", { name: /toggle theme/i }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Light" }));

    expect(document.documentElement).toHaveClass("light");
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("ors-theme")).toBe("light");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd web && pnpm test --run tests/shell.test.tsx; echo "exit=$?"`
Expected: FAIL — cannot resolve `../src/components/AppShell`. Not a config error; if the failure is about jsdom or the setup file, fix that first and re-run so the red is the one you meant.

- [ ] **Step 4: Implement**

`theme-provider.tsx` and `mode-toggle.tsx` are shadcn's published Vite recipe, verbatim, with `defaultTheme="dark"` and `storageKey="ors-theme"` supplied at the call site in `main.tsx`.

`AppShell.tsx` wraps the `sidebar-04` block: five `SidebarMenuItem` links, the footer carrying `<ModeToggle />` and a sign-out button, and a header slot for the rack status strip (filled in task 6 — leave a `<div data-testid="rack-strip" />` placeholder with a comment naming the task).

- [ ] **Step 5: Run tests, lint and build**

```bash
cd web && pnpm test --run; echo "exit=$?"
pnpm typecheck; echo "exit=$?"
pnpm lint; echo "exit=$?"
pnpm build; echo "exit=$?"
```
Expected: all exit 0.

- [ ] **Step 6: Commit**

```bash
git add web .gitignore
git commit -m "feat(web): the shell, and a theme that survives a reload"
```

---

### Task 2: Generated types, the typed client, and the drift check

**Files:**
- Create: `web/src/api/client.ts`, `web/scripts/generate-types.ts` (or a package script), `web/tests/api-client.test.ts`
- Generate: `web/src/api/schema.d.ts`
- Modify: `web/package.json`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `api` — an `openapi-fetch` client typed by `paths` from `schema.d.ts`; `generateTypes()` as a pnpm script; a CI job that fails on drift.

**Why this task exists at all:** the spec forbids hand-written API types. M3a gave every route an explicit response model *specifically* so this generation produces real types rather than `object`.

- [ ] **Step 1: Add the generator and produce the schema**

```bash
cd web
pnpm add -D openapi-typescript
pnpm add openapi-fetch
```

`package.json`:

```json
"scripts": {
  "generate:types": "openapi-typescript http://127.0.0.1:8080/api/openapi.json -o src/api/schema.d.ts"
}
```

Generating needs a running server:

```bash
cd .. && uv run ors-server &   # or python -m ors_server
cd web && pnpm generate:types
```

**Do not commit the server as a background process in any script.** Generate, then stop it.

- [ ] **Step 2: Write the failing test**

`web/tests/api-client.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { paths } from "../src/api/schema";

describe("the generated schema", () => {
  it("types a daemon rather than leaving it an object", () => {
    type Daemon =
      paths["/api/daemons"]["get"]["responses"][200]["content"]["application/json"][number];
    const daemon: Daemon = {
      id: 4,
      name: "pi-rack",
      status: "paired",
      online: true,
      config_version: 7,
      applied_version: 7,
      config_error: null,
      version: "0.1.0",
      last_seen: null,
      created_at: "2026-08-15T00:00:00+00:00",
      paired_at: null,
      capabilities: [],
    };
    expect(daemon.applied_version).toBe(7);
  });

  it("knows a pairing token is only ever on the create response", () => {
    type Created =
      paths["/api/daemons"]["post"]["responses"][201]["content"]["application/json"];
    type Listed =
      paths["/api/daemons"]["get"]["responses"][200]["content"]["application/json"][number];

    const created: Created = {} as Created;
    expectTypeOf(created).toHaveProperty("token");
    expectTypeOf({} as Listed).not.toHaveProperty("token");
  });
});
```

(`expectTypeOf` comes from `vitest`. Import it.)

The first test is not decoration: if a route ever loses its response model, the field access stops type-checking and this fails at `pnpm typecheck` rather than in a browser.

- [ ] **Step 3: Run it and watch it fail**

Run: `cd web && pnpm typecheck; echo "exit=$?"`
Expected: FAIL — `schema.d.ts` does not exist yet, or the property is missing. Note the actual message; if `applied_version` is absent, the server is not what this plan assumed and that is a finding to raise before continuing.

- [ ] **Step 4: Write the client**

`web/src/api/client.ts`:

```ts
import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({
  baseUrl: "/",
  credentials: "same-origin",
});
```

`credentials: "same-origin"` is what carries the session cookie. The server sets it `HttpOnly`, so nothing in this codebase reads it.

- [ ] **Step 5: Add the drift check to CI**

In `.github/workflows/ci.yml`, after the server job can run:

```yaml
- name: types match the server
  run: |
    uv run ors-server &
    SERVER=$!
    trap "kill $SERVER" EXIT
    for i in $(seq 1 40); do curl -sf localhost:8080/api/health && break; sleep 0.25; done
    cd web && pnpm generate:types && git diff --exit-code src/api/schema.d.ts
```

`git diff --exit-code` is the whole point: a server change that alters the API fails in the server's own pipeline.

- [ ] **Step 6: Verify and commit**

```bash
cd web && pnpm typecheck && pnpm test --run; echo "exit=$?"
git add web .github/workflows/ci.yml
git commit -m "feat(web): types generated from the server, and a check that they stay that way"
```

---

### Task 3: Authentication — setup, login, and the guard

**Files:**
- Create: `web/src/api/queries.ts`, `web/src/routes/setup/SetupPage.tsx`, `web/src/routes/login/LoginPage.tsx`, `web/src/routes/RequireSession.tsx`, `web/tests/auth.test.tsx`, `web/tests/msw.ts`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `api` (task 2).
- Produces: `useSession()`, `RequireSession({children})`, and the routes `/setup` and `/login`.

- [ ] **Step 1: Stand up MSW**

```bash
cd web && pnpm add -D msw
```

`web/tests/msw.ts` exports a `server` with per-test handlers. The server is started in a Vitest setup file and reset between tests.

- [ ] **Step 2: Write the failing test**

`web/tests/auth.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "./msw";
import { renderApp } from "./render";  // helper: router + QueryClient + providers

describe("getting in", () => {
  it("sends a server with no password to setup", async () => {
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({ detail: "not configured" }, { status: 409 }),
      ),
    );
    renderApp({ at: "/daemons" });

    expect(await screen.findByRole("heading", { name: /set a password/i })).toBeInTheDocument();
  });

  it("sends an unauthenticated session to login, not to setup", async () => {
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    );
    renderApp({ at: "/daemons" });

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("returns to login when a session expires mid-session, and only once", async () => {
    let meCalls = 0;
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1;
        return meCalls === 1
          ? HttpResponse.json({ authenticated: true })
          : HttpResponse.json({ detail: "not authenticated" }, { status: 401 });
      }),
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    );
    renderApp({ at: "/daemons" });

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    // A redirect loop would keep calling; assert it settled.
    await waitFor(() => expect(meCalls).toBeLessThan(4));
  });

  it("refuses a second browser racing the first to claim the password", async () => {
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({ detail: "not configured" }, { status: 409 }),
      ),
      http.post("/api/auth/setup", () =>
        HttpResponse.json({ detail: "already configured" }, { status: 409 }),
      ),
    );
    renderApp({ at: "/setup" });

    await userEvent.type(await screen.findByLabelText(/password/i), "hunter2hunter2");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    expect(await screen.findByText(/already configured/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd web && pnpm test --run tests/auth.test.tsx; echo "exit=$?"`
Expected: FAIL — no `SetupPage`.

- [ ] **Step 4: Implement**

`useSession()` is a TanStack Query hook over `GET /api/auth/me` with `retry: false`. `RequireSession` reads it and renders `<Navigate to="/setup">` on 409, `<Navigate to="/login">` on 401, and its children otherwise.

**The 401 handler lives in one place** — a `fetch` middleware on the openapi-fetch client that, on a 401 from any request, clears the session query and navigates once. Guard it with a flag so two concurrent 401s do not push two history entries.

- [ ] **Step 5: Verify, then mutation-test the guard**

Mutants to run and kill: the 409 branch removed (a fresh server goes to login and cannot be set up); the 401 branch removed; the once-only flag removed (assert the history length).

- [ ] **Step 6: Commit**

```bash
git add web
git commit -m "feat(web): the two states a server can be in before anyone is signed in"
```

---

### Task 4: The mutation layer and `X-Unservable-Daemons`

**Files:**
- Create: `web/src/api/unservable.ts`, `web/src/api/mutate.ts`, `web/tests/unservable.test.ts`
- Modify: `web/src/api/queries.ts`

**Interfaces:**
- Consumes: `api` (task 2).
- Produces: `parseUnservable(headers: Headers) -> number[]`, and `useMutate()` — a wrapper that invalidates the queries a mutation affects and surfaces the unservable racks.

**This is the contract M3a wrote down for this milestone.** Get it wrong and the interface silently reports edits as applied that never reached a panel.

- [ ] **Step 1: Write the failing test**

`web/tests/unservable.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { parseUnservable } from "../src/api/unservable";

describe("the header that says an edit did not reach a rack", () => {
  it("reads a list of ids", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": "3,7" }))).toEqual([3, 7]);
  });

  it("is empty when the header is absent, which means every rack got it", () => {
    expect(parseUnservable(new Headers())).toEqual([]);
  });

  it("reads it off a 201, because a create carries it too", () => {
    // The status code is not the signal. A create answers 201 and sets the
    // header when the row exists but no rack could be given it.
    const response = new Response(null, {
      status: 201,
      headers: { "X-Unservable-Daemons": "4" },
    });
    expect(parseUnservable(response.headers)).toEqual([4]);
  });

  it("survives whitespace and a single id", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": " 12 " }))).toEqual([12]);
  });

  it("ignores anything that is not a number rather than yielding NaN", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": "3,,x,7" }))).toEqual([3, 7]);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && pnpm test --run tests/unservable.test.ts; echo "exit=$?"`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// web/src/api/unservable.ts

/**
 * The racks an edit could not be given to, read from the response header.
 *
 * The header rather than the status code, and this is the whole reason the
 * function exists: `POST /api/screens` answers 201 even when nothing was
 * pushed, because the row does exist and its representation is in the body.
 * An interface that branched on `status === 202` would miss every create.
 */
export function parseUnservable(headers: Headers): number[] {
  const raw = headers.get("X-Unservable-Daemons");
  if (!raw) return [];
  return raw
    .split(",")
    .map((part) => Number(part.trim()))
    .filter((id) => Number.isInteger(id));
}
```

`useMutate()` wraps a mutation so that on success it invalidates the affected queries **and** returns the unservable ids to the caller, which renders them as "saved, but rack N did not get it — see its status".

**No optimistic updates anywhere.** M3a can accept an edit, save it, and not push it; an optimistic UI would show it applied.

- [ ] **Step 4: Verify and mutation-test**

Mutants: drop the `Number.isInteger` filter (a malformed header yields `[NaN]` and the interface names rack NaN); read `response.status === 202` instead of the header (the create case survives); return `[]` unconditionally.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): read the header that says which rack did not get the edit"
```

---

### Task 5: The `/ws/ui` client

**Files:**
- Create: `web/src/live/socket.ts`, `web/tests/socket.test.ts`
- Test helper: a fake WebSocket the test drives.

**Interfaces:**
- Consumes: nothing.
- Produces: `createLiveSocket({url, onDaemons, onFrame, now})` with `.connect()`, `.close()`, `.subscribe(screenId)`, `.unsubscribe(screenId)`, and `.state`.

- [ ] **Step 1: Write the failing test**

`web/tests/socket.test.ts` — the cases that matter, each named for what it protects:

```ts
describe("the browser socket", () => {
  it("re-subscribes the panels still on screen after a reconnect", async () => {
    // A wifi blip must not leave a frozen canvas until the tab is reloaded.
  });

  it("backs off, and caps", async () => {
    // vi.useFakeTimers(); assert the delays are 1,2,4,8,16,30,30 — not a hammer.
  });

  it("skips a message it does not understand and stays open", () => {
    // The server's own rule: an unknown message is skipped and logged,
    // the socket stays open.
  });

  it("decodes a frame with the standard alphabet, and would fail on the URL-safe one", () => {
    // The trap: atob throws on `-` and `_`. Use a payload whose two
    // encodings differ in EVERY character, so an ASCII fixture cannot
    // hide a wrong alphabet.
    const bytes = new Uint8Array([0xff, 0xef, 0xfe, 0xff, 0xef, 0xfe]);
    // standard: "/+/+/+/+"   url-safe: "_-_-_-_-"
  });
});
```

Write all four in full. The fourth is the one that would otherwise ship broken: `base64.b64decode` in Python accepts either alphabet, which is exactly how this survived a green test in M3a until it was pinned with a discriminating payload.

- [ ] **Step 2: Run and watch it fail**

Run: `cd web && pnpm test --run tests/socket.test.ts; echo "exit=$?"`

- [ ] **Step 3: Implement**

Reconnect with capped exponential backoff and jitter. Keep the subscribed set in the client, and on `open`, send a `subscribe` for each. Parse each message by `type`; anything unknown is logged and skipped without closing.

Decode a frame with `atob(message.webp)` and **no substitution** — the server sends the standard alphabet deliberately, and `ws_ui.py` assembles the message rather than dumping a pydantic model to guarantee it.

- [ ] **Step 4: Verify and mutation-test**

Mutants: drop the re-subscribe on open; uncap the backoff; close the socket on an unknown message; swap `atob` for a URL-safe substitution (the discriminating payload kills it); drop a screen from the subscribed set on unsubscribe but not from the wire.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): one socket, reconnecting, and the alphabet it decodes with"
```

---

### Task 6: The frame store, the panel, and staleness

**Files:**
- Create: `web/src/live/frames.ts`, `web/src/components/Panel.tsx`, `web/src/live/LiveProvider.tsx`, `web/tests/frames.test.ts`, `web/tests/panel.test.tsx`
- Modify: `web/src/components/AppShell.tsx` (the rack status strip)

**Interfaces:**
- Consumes: `createLiveSocket` (task 5), `api` (task 2).
- Produces: `frameStore` with `subscribe(screenId, cb) -> unsubscribe`, `push(frame)`, `lastAt(screenId)`; `<Panel screenId size />`; `<LiveProvider>`.

- [ ] **Step 1: Write the failing tests**

`frames.test.ts`:

```ts
describe("the frame store", () => {
  it("delivers a frame only to the panel that asked for it", () => {});

  it("closes the bitmap it replaces, so a tab does not grow", () => {});

  it("treats a backwards seq as a new stream rather than as frames to drop", () => {
    // The daemon restarted. Resetting is right; dropping would freeze the panel.
  });

  it("re-renders nothing above the panel", () => {
    // The whole reason this store exists. Assert with a render counter on a
    // parent component that eight pushes leave it at one render.
  });
});
```

`panel.test.tsx`:

```tsx
describe("a panel", () => {
  it("draws the frame exactly as it arrives, without rotating it", () => {
    // rotation describes how the panel is bolted in; the daemon already
    // streams the pre-correction image. Rotating here would be wrong twice.
  });

  it("goes stale when no frame has arrived for a while", () => {
    // vi.useFakeTimers; there is no stalled-stream event by design.
  });

  it("says offline, not stale, when its rack has gone", () => {
    // `daemons` is the signal. Never inferred from frames stopping.
  });

  it("keeps its last image when the socket drops", () => {
    // Blank would read as a dead rack.
  });
});
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement**

`frames.ts` holds one `ImageBitmap` per screen id and a `Map<number, Set<cb>>`. `push` decodes via `createImageBitmap(new Blob([bytes], {type: "image/webp"}))`, closes the previous bitmap for that screen, and calls the subscribers. No React state anywhere in this file.

`Panel.tsx` subscribes in an effect, draws to a canvas through a ref with a circular clip, and derives staleness from `lastAt(screenId)` against a ticking clock plus the rack's online flag.

`LiveProvider` owns the socket, writes `daemons` into the Query cache with `setQueryData`, and pushes frames into the store. The rack status strip reads the same query.

- [ ] **Step 4: Verify and mutation-test**

Mutants: never close the replaced bitmap; deliver every frame to every subscriber; treat a backwards `seq` as out-of-order and drop it; rotate the image by the screen's `rotation`; infer offline from frame silence instead of from `daemons`; blank the canvas on socket close.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): frames that never re-render the page above them"
```

---

### Task 7: The Daemons page

**Files:**
- Create: `web/src/routes/daemons/DaemonsPage.tsx`, `PairDialog.tsx`, `RotateKeyDialog.tsx`, `DeleteDaemonDialog.tsx`, `EventList.tsx`, `web/tests/daemons.test.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `api`, `useMutate`, `parseUnservable`, the `daemons` live query.
- Produces: the `/daemons` route.

- [ ] **Step 1: Write the failing test**

Cover, each in full:

```tsx
it("shows the pairing token once, with the command that carries it", async () => {});

it("never shows a token again from any other route", async () => {
  // The list response has no token field at all — assert on the rendered page.
});

it("reports delivered honestly when a push reached nobody", async () => {
  // POST /push answers 202 {delivered: false} for a connected rack whose
  // configuration is unservable. "Online" must not be rendered as "it got it".
});

it("says which rack did not get an edit, from the header", async () => {});

it("shows applied_version beside config_version when they differ", async () => {});

it("names what a delete takes with it", async () => {});

it("labels the event list as recent, not as history", async () => {
  // The ring holds 200 per rack and a flapping rack spends two per reconnect.
});
```

- [ ] **Step 2-4: Red, implement, green.**

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): the racks, their versions, and the token shown once"
```

---

### Task 8: The Screens page — rack canvas and inspector

**Files:**
- Create: `web/src/routes/screens/ScreensPage.tsx`, `RackCanvas.tsx`, `Inspector.tsx`, `ConfigTab.tsx`, `DataTab.tsx`, `SleepTab.tsx`, `web/tests/screens.test.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `<Panel>`, `api`, `useMutate`.
- Produces: the `/screens` route.

- [ ] **Step 1: Write the failing test**

```tsx
it("subscribes on mount and unsubscribes on unmount", async () => {
  // A closed tab that kept its subscription leaves the Pi encoding for nobody.
});

it("unsubscribes when the tab is hidden", async () => {
  // document.visibilityState — same reason.
});

it("orders the canvas by position, not by id", async () => {
  // FIXTURE TRAP: ids and positions must differ. Use ids 11,12,13 at
  // positions 3,1,2 so an implementation sorting by id fails.
});

it("reorders through the API rather than locally", async () => {});

it("edits a screen and shows which rack did not get it", async () => {});
```

- [ ] **Step 2-4: Red, implement, green.**

The inspector is three tabs over one form; `Config` writes wiring and template, `Data` binds the template's params to integration fields, `Sleep` writes the per-screen override.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): the rack as a row of live panels, and what each one is"
```

---

### Task 9: The detect and probe messages

**Files:**
- Modify: `packages/ors-schema/src/ors_schema/link.py`
- Test: `packages/ors-schema/tests/test_link.py`

**Interfaces:**
- Produces: `DetectRequest`, `DetectResult`, `ProbeRequest`, `ProbeResult`, each in the right union, plus `PanelCandidate`.

**Read first:** the existing `link.py` in full. Every message there carries `extra="forbid"`, a `type` discriminator, and bounds on anything that comes off the wire. Match it.

- [ ] **Step 1: Write the failing test**

```python
def test_a_detect_result_names_what_is_already_claimed():
    result = DetectResult(
        request_id="r1",
        panels=[
            PanelCandidate(bus=0, cs=0, claimed_by="CPU"),
            PanelCandidate(bus=1, cs=1, claimed_by=None),
        ],
    )
    assert result.panels[1].claimed_by is None


def test_a_probe_names_the_wiring_it_is_proving():
    probe = ProbeRequest(request_id="r2", bus=1, cs=0, dc=4, rst=27, hz=16_000_000, hold_s=3.0)
    assert probe.hold_s == 3.0


def test_a_probe_may_not_hold_a_bus_for_ever():
    with pytest.raises(ValidationError):
        ProbeRequest(request_id="r3", bus=0, cs=0, dc=25, rst=27, hz=16_000_000, hold_s=600.0)


def test_a_request_id_is_bounded_like_every_other_string_off_the_wire():
    with pytest.raises(ValidationError):
        DetectRequest(request_id="x" * 1000)


def test_a_flag_is_not_a_bus():
    # The defect this project has now fixed three times: `true` coerces to 1.
    with pytest.raises(ValidationError):
        ProbeRequest(request_id="r4", bus=True, cs=0, dc=25, rst=27, hz=16_000_000, hold_s=1.0)
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest packages/ors-schema/tests/test_link.py -q; echo "exit=$?"`
Expected: FAIL — `ImportError: cannot import name 'DetectRequest'`.

- [ ] **Step 3: Implement**

Add the four models and `PanelCandidate`. `DetectRequest` and `ProbeRequest` join `ServerMessage`; `DetectResult` and `ProbeResult` join `DaemonMessage`. Bound `request_id`, `hold_s`, and the pin numbers. Reuse the existing `not_a_flag` validator for every int that comes off the wire — it is public for exactly this.

- [ ] **Step 4: Verify**

Run: `uv run pytest -q; echo "exit=$?"` — nothing may regress.

- [ ] **Step 5: Commit**

```bash
git add packages/ors-schema
git commit -m "feat(schema): asking a rack what it has, and proving one panel"
```

---

### Task 10: A correlated reply in the hub

**Files:**
- Modify: `server/src/ors_server/link/hub.py`
- Test: `server/tests/test_hub.py`

**Interfaces:**
- Consumes: `Hub` as it stands.
- Produces: `await hub.request(daemon_id, message, timeout) -> reply | None`, and `hub.deliver_reply(message)` called from `ws_daemon`.

**Read first:** `hub.py` in full, and the ledger's note that **`Hub` is event-loop-affine** — every method must be called from the loop thread.

- [ ] **Step 1: Write the failing test**

```python
async def test_a_reply_reaches_the_caller_that_asked_for_it():
    ...

async def test_two_requests_in_flight_do_not_cross():
    # Different request_ids, replies delivered out of order.

async def test_a_rack_that_never_answers_times_out_rather_than_holding_the_handler():
    ...

async def test_a_reply_for_a_request_nobody_is_waiting_on_is_dropped():
    # A late reply after a timeout must not raise.

async def test_a_dropped_connection_fails_every_request_in_flight():
    # Otherwise the handler waits out the full timeout for a rack that has gone.

async def test_an_offline_rack_is_refused_immediately():
    ...
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Implement**

A `dict[str, asyncio.Future]` keyed by `request_id`, resolved by `deliver_reply`, cleaned up in a `finally` on both the timeout and the success path. `drop` fails every future belonging to that daemon.

- [ ] **Step 4: Verify and mutation-test**

Mutants: never clean up the future (a slow leak); resolve by daemon id rather than request id (two in-flight requests cross); do not fail futures on drop; swallow a late reply's `KeyError` by removing the guard rather than by handling it.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): ask a rack something and wait for the answer"
```

---

### Task 11: The daemon enumerates and probes

**Files:**
- Create: `daemon/src/ors_daemon/hardware.py`
- Modify: `daemon/src/ors_daemon/link.py`, `daemon/src/ors_daemon/__main__.py`, `daemon/src/ors_daemon/supervisor.py`
- Test: `daemon/tests/test_hardware.py`

**Interfaces:**
- Produces: `enumerate_panels(root: Path) -> list[tuple[int, int]]`, `Supervisor.claimed_devices() -> dict[tuple[int,int], str]`, `Supervisor.probe(bus, cs, dc, rst, hz, hold_s) -> None`.

**This is the task with the hardware constraint.** Read the ledger's M2 entries on the bus race and task 10's `_off_the_bus` before writing anything.

- [ ] **Step 1: Write the failing test**

```python
def test_enumeration_reads_the_devices_that_exist(tmp_path):
    for name in ("spidev0.0", "spidev0.1", "spidev1.0"):
        (tmp_path / name).touch()
    assert enumerate_panels(tmp_path) == [(0, 0), (0, 1), (1, 0)]


def test_a_device_the_rack_is_already_driving_is_named(...):
    # claimed_by is the screen's name, so the wizard can say why it is unavailable.


def test_probing_a_claimed_device_is_refused_rather_than_fought_over(...):
    # A live worker owns that SPI device. Taking it would be a torn frame at
    # best and a wedged bus at worst.


def test_a_probe_holds_every_worker_on_that_bus_off_the_bus(...):
    # THE M2 LESSON. Interleaving an init sequence with a bus-mate's frame
    # produced a pale grey rectangle, non-deterministically, and it stayed
    # wrong because the init registers were wrong rather than the framebuffer.
    # Assert the pause happened, per screen, bounded.


def test_a_worker_that_will_not_come_off_the_bus_refuses_the_probe(...):
    # Better a refused probe than a corrupted panel.


def test_a_probe_closes_the_device_afterwards(...):
    # Otherwise a probed panel stays claimed and the next apply cannot open it.


def test_a_probe_that_cannot_open_the_device_reports_why(...):
    ...
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement**

`enumerate_panels` globs `spidev<bus>.<cs>` under `/dev` (the root is a parameter so the test needs no `/dev`). `Supervisor.probe` refuses a claimed device, takes the same per-screen bus guard `_off_the_bus` uses, opens the device, paints the same pattern `identify` paints, holds for `hold_s`, then sleeps and closes it.

Wire `on_detect` and `on_probe` into `LinkClient` in `__main__._link()`. **Note the M3a defect this repeats:** `on_command` was never passed there, and the route reported `delivered: true` for a command nothing received. Add a test that the handlers are actually wired, not just that they work when called.

- [ ] **Step 4: Verify and mutation-test**

Mutants: probe a claimed device anyway; skip the bus guard; never close the device; never pass the handlers to `LinkClient` (this is the M3a defect — it must die).

- [ ] **Step 5: Commit**

```bash
git add daemon
git commit -m "feat(daemon): say what is wired, and prove one panel without disturbing the rest"
```

---

### Task 12: `/api/daemons/{id}/detect` and `/probe`

**Files:**
- Modify: `server/src/ors_server/api/daemons.py`
- Test: `server/tests/test_api_detect.py`

**Interfaces:**
- Consumes: `hub.request` (task 10), the schema (task 9).
- Produces: `POST /api/daemons/{id}/detect`, `POST /api/daemons/{id}/probe`, each with an explicit response model.

- [ ] **Step 1: Write the failing test**

```python
def test_detect_answers_with_what_the_rack_reported(...)
def test_detect_on_an_offline_rack_says_so_rather_than_hanging(...)
def test_a_rack_that_does_not_answer_times_out_with_a_reason(...)
def test_probe_refuses_a_device_the_rack_says_is_claimed(...)
def test_both_routes_refuse_an_unauthenticated_caller(...)
def test_both_routes_are_async_def(...)   # Hub is event-loop-affine
def test_neither_route_opens_a_change(...)  # they mutate nothing
```

- [ ] **Step 2-4: Red, implement, green.**

Both routes are `async def` — the existing sweep in `test_api_routes.py` enforces the rule, and these touch the hub. Neither opens a `change()`; they mutate nothing, and the sweep's `MUTATES_NOTHING` list is where that is declared.

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(server): the two questions the wizard asks a rack"
```

---

### Task 13: The add-screen wizard

**Files:**
- Create: `web/src/routes/screens/AddScreenWizard.tsx` and its steps, `web/tests/wizard.test.tsx`

**Interfaces:**
- Consumes: `/detect`, `/probe`, `POST /api/screens`.

- [ ] **Step 1: Write the failing test**

```tsx
it("offers only the devices the rack is not already driving", async () => {});

it("pre-fills DC and RST from the rack's existing screens", async () => {
  // They are wiring choices; nothing on the bus reports them.
});

it("will not add a screen until the probe was confirmed", async () => {});

it("says what went wrong when a probe fails, and lets you change the wiring", async () => {});

it("tells you plainly that a rack must be online to detect", async () => {});
```

- [ ] **Step 2-4: Red, implement, green.**

Four steps: detect → confirm wiring → probe → add. The probe step says what it is about to do before it lights a panel.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): the wizard that maps a circle of glass to a line of config"
```

---

### Task 14: Templates

**Files:** `web/src/routes/templates/*`, `web/tests/templates.test.tsx`

- [ ] Tests: the list; the preview is rendered **un-rotated**; assign; detach; a built-in cannot be deleted (the server refuses — assert the interface does not offer it).
- [ ] Implement, verify, commit as `feat(web): templates, assigned and detached`.

---

### Task 15: Integrations

**Files:** `web/src/routes/integrations/*`, `web/tests/integrations.test.tsx`

- [ ] Tests: create and edit; `credential` is a field going in and **never** comes back (assert `has_credential` is what a read gives); an empty string clears it; an *enabled* integration may not hold one and the 422 is rendered readably; **Test** is labelled a reachability check, not a preview, because the server reports the first sample and not the value the panel will show; a URL with userinfo is refused and the message does not echo the password.
- [ ] Implement, verify, commit as `feat(web): integrations, and a test button that says what it tested`.

---

### Task 16: Settings

**Files:** `web/src/routes/settings/*`, `web/tests/settings.test.tsx`

- [ ] Tests: change the password (and that the old one is required); timezone; the global night window; a settings change that reached three racks of four answers 200 **with** the header, and the interface names the fourth.
- [ ] Implement, verify, commit as `feat(web): settings, and the rack a change did not reach`.

---

### Task 17: Serving the interface from the API

**Files:**
- Modify: `server/src/ors_server/app.py`
- Test: `server/tests/test_spa.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_api_still_wins_over_the_spa(client):
    # /api/health must not be swallowed by the catch-all.
    assert client.get("/api/health").status_code == 200


def test_a_client_side_route_survives_a_reload(client):
    # GET /screens returns index.html, not 404.


def test_an_unknown_api_path_is_still_404_rather_than_the_spa(client):
    # Otherwise a typo in a fetch returns HTML and fails as a JSON parse error.


def test_the_sockets_are_not_shadowed(client):
    ...


def test_a_missing_build_is_a_clear_error_rather_than_a_500(client):
    # Running the server without `pnpm build` is the ordinary developer state.
```

- [ ] **Step 2-4: Red, implement, green.** Mount `StaticFiles` at `/` **after** every router, with an `html=True` fallback, and skip the mount entirely with one warning when `dist/` is absent.

- [ ] **Step 5: Commit** as `feat(server): serve the interface from the same origin as the API`.

---

### Task 18: The Node stage in the image

**Files:**
- Modify: `deploy/Dockerfile`, `server/tests/test_deploy.py`, `server/README.md`

**Read first:** the three defects task 14 found by *running* the image — `uv sync` installing workspace members editable, a venv built at one path and served from another, and `${ORS_SECRET_KEY:-}` substituting to an empty string that `load_or_create_key` refuses. Do not reintroduce them.

- [ ] Tests (documents, not Docker): a Node stage exists and is not the final stage; the final stage ships no `node_modules` and no package manager; the built assets are copied to where `app.py` looks for them; the lockfile is used with `--frozen-lockfile`.
- [ ] Implement, then **build and run it**: `docker build -f deploy/Dockerfile .`, start it, confirm the interface loads and `/api/health` answers. If Docker is unavailable, say so plainly rather than claiming it builds — it goes on the hardware checklist instead.
- [ ] Commit as `feat(deploy): build the interface into the image that serves it`.

---

### Task 19: End-to-end against the real stack

**Files:**
- Create: `web/e2e/fixture.ts`, `web/e2e/rack.spec.ts`, `web/playwright.config.ts`

**Interfaces:**
- Consumes: everything.

**This is the layer that catches what the others cannot.** The base64 trap passed a green Python test and a green component test; only a real server talking to a real browser would have caught it.

- [ ] **Step 1: The fixture**

Boots a real `ors-server` on an ephemeral port with a temp data dir, and a real `ors-daemon` with `virtual` panels, paired through the API. Tears both down and **verifies they are gone** — a leftover process corrupted twenty minutes of another task's test runs in M3a.

- [ ] **Step 2: The specs**

```
1. set a password, sign in
2. pair a rack; the token appears once
3. add a screen through the wizard: detect, confirm, probe, add
4. the panel renders — assert the canvas has non-blank pixels
5. edit the screen; the change reaches the rendered panel
6. stop the server; the interface says so and does not claim the rack is fine
7. restart it; the interface recovers without a reload
```

- [ ] **Step 3: Run, and report the timings.** A spec that needs a `sleep` to pass is a spec that will flake in CI; wait on a condition instead.

- [ ] **Step 4: Commit** as `test(web): the whole thing, against a real server and a real rack`.

---

## Known plan gaps

Recorded here rather than discovered at execution time, because M3a's two prose-only tasks were the two whose reviews found the most.

**Tasks 7, 8, 13, 14, 15 and 16 name their tests but do not write them out.** Every other task carries the actual test code. These six are the page tasks, where the test bodies are long, repetitive and mostly assertions about rendered text — and where writing them now, against components that do not exist, risks pinning an implementation rather than a behaviour.

**Expand each one immediately before dispatching it**, not at execution time inside the task, and expand it from the *spec section it implements* rather than from this plan's summary of it. The test names in those tasks are not placeholders — each names a specific behaviour the spec requires, and several name a defect this project has already shipped once. They are the contract; the bodies are the work.

The M3a precedent: its tasks 12 and 14 were left as prose. Task 12's drafted rule turned out to be **wrong** — it would have frozen three panels of the rack canvas every time a live view opened — and that was only caught when the task was expanded against the real `FrameStream`. Expanding a task is when you discover the plan was wrong about it.

## Definition of done for M3b

1. `pnpm test`, `pnpm typecheck`, `pnpm lint`, `pnpm build`, `uv run pytest` and ruff all pass. The generated types match the server's schema.
2. From a clean database: set a password, pair a daemon, add four screens through the wizard using detect and probe, and see all four rendering live.
3. An edit made in the interface reaches the glass.
4. Stopping the server leaves the rack rendering, the interface says so honestly, and it recovers when the server returns.
5. `docker build` produces one image serving both the API and the interface.
6. Playwright passes against the real stack.

## What M4 picks up

- Jellyfin, the \*arr applications, qBittorrent and Grafana — each a poller in the daemon and a form here.
- The visual template editor and the workflow builder.
- `frames_dropped`, which reaches `status.json` and stops.
- Whether `sleep`, `wake` and `reload` become real commands; M3a answers 501 and names the working mechanism, and the interface must not offer buttons for them until they exist.
- Cross-building the ARM image, which nothing has done — the 32-bit compile path has never run.
