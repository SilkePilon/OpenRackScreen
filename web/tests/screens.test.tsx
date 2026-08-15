import { act, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, Integration, Screen as ScreenRow, Template } from "../src/api/queries"
import { collectSockets, type FakeSocket } from "./fake-socket"
import { server } from "./msw"
import { recordCanvas, stubDecoder, type BitmapDecoder, type CanvasRecorder } from "./paint"
import { renderApp } from "./render"

// Racks 8 (pi-loft) and 42 (pi-cellar), and 63, which is in no listing. Screens
// 11, 12 and 13 at positions 3, 1 and 2. Every one of those is deliberate, and
// each has caught something in this project before:
//
//   * **No screen id is its own position and none is its own index.** The
//     server answers `ORDER BY position, id`, so the list arrives 12, 13, 11 --
//     an interface that re-sorted by id would draw 11, 12, 13, and an interface
//     that read the panel at index n as screen n+11 would draw the same. An
//     identity fixture (screen 1 at position 1) hid a real bug twice in M3a.
//   * **The rack a screen is on (8) is not the rack the unservable header names
//     (42, 63).** No answer the server really gives separates "the rack this
//     screen belongs to" from "the racks that did not get this edit" -- a PATCH
//     of screen 11 affects rack 8 and nothing else -- so only a fixture where
//     they differ can tell the header being read from the body being guessed.
//   * **No name is a substring of another**, so `not.toHaveTextContent` on one
//     cannot be satisfied by the absence of a different one.
const RACK = 8
const OTHER_RACK = 42
const UNLISTED_RACK = 63

const SIGNED_IN = http.get("/api/auth/me", () =>
  HttpResponse.json({ authenticated: true, password_set: true }),
)

/** The events route, stubbed empty. Only the Daemons page asks it. */
const NO_EVENTS = http.get("/api/events", () => HttpResponse.json([]))

function rack(id: number, name: string, online = true): Daemon {
  return {
    id,
    name,
    online,
    status: online ? "connected" : "offline",
    config_version: 9,
    applied_version: 9,
    config_error: null,
    version: "0.3.1",
    capabilities: {},
    last_seen: null,
    paired_at: "2026-08-01T09:15:00Z",
    created_at: "2026-08-01T09:00:00Z",
  }
}

// pi-cellar is **offline**, and that is load-bearing rather than colour. A panel
// reads *offline* from the `daemons` cache entry and from the rack id it is
// handed -- nothing else in the interface can say it -- so a canvas that passed
// a constant rack id, or the first screen's, would draw every panel as live on
// any fixture where every rack is online. One rack that has gone is what makes
// the id the canvas passes observable at all.
const RACKS = [rack(RACK, "pi-loft"), rack(OTHER_RACK, "pi-cellar", false)]

/** One screen as `GET /api/screens` reports it: every field the page reads. */
function panel(over: Partial<ScreenRow> & Pick<ScreenRow, "id" | "name" | "position">): ScreenRow {
  return {
    daemon_id: RACK,
    display: { backend: "gc9a01", spi_bus: 0, spi_cs: 0, dc: 25, rst: 27, hz: 40_000_000 },
    rotation: 270,
    hflip: false,
    enabled: true,
    template: "big-number",
    params: {},
    sleep_override: null,
    ...over,
  }
}

/**
 * The rack as the server answers it: `ORDER BY position, id`, so 12, 13, 11.
 *
 * Served in the server's order rather than sorted here, because that is what
 * the interface really receives and the whole of what test three is about.
 */
const WEATHER = panel({ id: 12, name: "Weather", position: 1, display: { backend: "gc9a01", spi_bus: 0, spi_cs: 0, dc: 25, rst: 27, hz: 40_000_000 } })
// Flipped as well as rotated, so a page that applied the mount correction has
// two things to apply and the test below has both to catch.
const TRAINS = panel({
  id: 13,
  name: "Trains",
  position: 2,
  hflip: true,
  display: { backend: "gc9a01", spi_bus: 0, spi_cs: 1, dc: 22, rst: 23, hz: 40_000_000 },
})
const KITCHEN = panel({
  id: 11,
  name: "Kitchen",
  position: 3,
  template: "ring-gauge",
  params: { title: "Kitchen" },
  // Its own night window, and one that is neither the server's nor the
  // schema's defaults, so a form that fell back to either is visible.
  sleep_override: { enabled: true, start: "22:30", end: "06:15" },
  display: { backend: "gc9a01", spi_bus: 0, spi_cs: 2, dc: 5, rst: 6, hz: 32_000_000 },
})

const RING_GAUGE: Template = {
  id: 3,
  name: "ring-gauge",
  category: "gauge",
  builtin: true,
  scenes: [],
  // The real `ring-gauge`'s schema, plus two shapes it does not have and the
  // model does: a boolean, and a default that is a document rather than a
  // value. `ParamSpec.default` is `Any`, so the second is a state a
  // user-written template can be in, and an editor that wrote a JSON string
  // back into it would turn a palette into text nothing can read.
  params_schema: {
    title: { type: "string", label: "Title", default: "CPU" },
    value: { type: "binding", label: "Value 0-100", default: "0" },
    hint: { type: "binding", label: "Hint line", default: "" },
    palette: { type: "palette", label: "Palette", default: "cyan" },
    dimmed: { type: "boolean", label: "Dimmed", default: false },
    thresholds: {
      type: "palette",
      label: "Thresholds",
      default: { stops: [{ at: 80, color: "#ff0000" }] },
    },
  },
}

const BIG_NUMBER: Template = {
  id: 7,
  name: "big-number",
  category: "gauge",
  builtin: true,
  scenes: [],
  params_schema: { big: { type: "binding", label: "Centre text", default: "0" } },
}

/**
 * One Prometheus integration on rack 8, with the two field shapes that differ.
 *
 * `cpu` reduces to a scalar and binds as `{{prom.cpu}}`; `cpu_hot` reduces to
 * `top`, which the poller answers as `{"node": ..., "value": ...}`, so what a
 * screen binds is `{{prom.cpu_hot.node}}` or `{{prom.cpu_hot.value}}`. The
 * integration's *name* is the namespace a binding's head identifier has to
 * match -- `ors_daemon.config._dependencies` intersects exactly that.
 */
const PROM: Integration = {
  id: 5,
  daemon_id: RACK,
  type: "prometheus",
  name: "prom",
  poll_interval: 5,
  enabled: true,
  has_credential: false,
  config: {
    type: "prometheus",
    name: "prom",
    url: "http://prometheus.example:9090",
    fields: {
      cpu: { query: "avg(node_cpu)", reduce: "scalar" },
      cpu_hot: { query: "max(node_cpu) by (instance)", reduce: "top", label: "instance" },
    },
  },
}

// Deliberately not `NightWindow`'s own defaults of 23:00 and 07:00: a panel
// given an override starts from the window it is already keeping, which is this
// one, and a form falling back to the schema's defaults instead would move the
// panel's night by an hour each way without anybody asking.
const NIGHT = { enabled: true, start: "22:00", end: "08:00" }
const SETTINGS = http.get("/api/settings", () =>
  HttpResponse.json({ timezone: "Europe/Amsterdam", night: NIGHT }),
)
/**
 * A second integration on the same rack, switched off.
 *
 * A disabled integration is not polled -- `ors_daemon.config` builds pollers
 * from the enabled ones -- so its name never appears in `snapshot.data` and a
 * binding naming it resolves to nothing, which `expand_params` then blanks. The
 * field it was bound to draws empty and nothing says why. So its readings must
 * not be offered, and this fixture is what notices: without it every
 * integration in the file is enabled and a tab that ignored the flag would be
 * indistinguishable from one that respects it.
 */
const OFF_AIR: Integration = {
  id: 9,
  daemon_id: RACK,
  type: "prometheus",
  name: "spare",
  poll_interval: 30,
  enabled: false,
  has_credential: false,
  config: {
    type: "prometheus",
    name: "spare",
    url: "http://spare.example:9090",
    fields: { disk: { query: "node_filesystem_free", reduce: "scalar" } },
  },
}

const TEMPLATES = http.get("/api/templates", () => HttpResponse.json([RING_GAUGE, BIG_NUMBER]))
const INTEGRATIONS = http.get("/api/integrations", () => HttpResponse.json([PROM, OFF_AIR]))

/** Every route the page reads before anything is selected. */
function reading(screens: ScreenRow[]) {
  return [
    SIGNED_IN,
    http.get("/api/daemons", () => HttpResponse.json(RACKS)),
    http.get("/api/screens", () => HttpResponse.json(screens)),
  ]
}

/** The three the inspector adds when a panel is selected. */
const INSPECTING = [TEMPLATES, INTEGRATIONS, SETTINGS]

/**
 * A write held open, so a test can be somewhere in the middle of one.
 *
 * There is no other way to stand in the window that matters here: everything
 * else in this file settles inside the click that started it, and "the user did
 * something while the PATCH was still on the wire" is a state msw only reaches
 * if the handler is made to wait for the test.
 */
function held() {
  let release = () => {}
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release: () => release() }
}

type Rig = {
  sockets: FakeSocket[]
  live(): FakeSocket
  /** Wait for the shell to dial, and accept the handshake. */
  connect(): Promise<void>
  decoder: BitmapDecoder
  canvas: CanvasRecorder
}

/**
 * Render the whole interface at `/screens`, with the browser's decoder, its 2D
 * context and its WebSocket stood in for.
 *
 * All three are the environment, not this milestone's code: jsdom implements
 * none of them, and its `WebSocket` opens a real TCP connection to
 * `window.location.host`.
 */
function mountScreens(): ReturnType<typeof renderApp> & Rig {
  const canvas = recordCanvas()
  const decoder = stubDecoder()
  const sockets = collectSockets()
  const view = renderApp({ at: "/screens" })
  const live = () => {
    const socket = sockets.at(-1)
    if (socket === undefined) throw new Error("the interface dialled nothing")
    return socket
  }
  return {
    ...view,
    canvas,
    decoder,
    sockets,
    live,
    // Nothing is dialled on the first render: `RequireSession` has not heard
    // back from `GET /api/auth/me`, so the shell that owns the connection --
    // and the page below it -- is not mounted yet.
    async connect() {
      await waitFor(() => expect(sockets.length).toBeGreaterThan(0))
      act(() => live().accept())
    },
  }
}

/**
 * The screens this tab has on the wire, replayed from what it sent.
 *
 * Read as a *state* rather than as a sequence because StrictMode mounts a
 * newly-arrived panel's effect twice -- setup, cleanup, setup -- so a canvas
 * that appears when its query settles subscribes, unsubscribes and subscribes
 * again, on a socket that is already open and therefore recording all three.
 * That churn is React's, not the interface's, and asserting the sequence would
 * be pinning a development-mode double-invoke rather than the rule. What the Pi
 * pays for is the set that is subscribed when the dust settles, and that is
 * what this computes.
 */
function watched(requests: unknown[]): number[] {
  const on = new Set<number>()
  for (const request of requests as { action: string; screen_id: number }[]) {
    if (request.action === "subscribe") on.add(request.screen_id)
    else if (request.action === "unsubscribe") on.delete(request.screen_id)
    else throw new Error(`the socket was sent an unknown action: ${JSON.stringify(request)}`)
  }
  return [...on].sort((left, right) => left - right)
}

/** The rack's canvas: the row of live panels, in the order it draws them. */
function canvasOf(name: string) {
  return within(screen.getByRole("list", { name: `Panels on ${name}` }))
}

/**
 * The screen ids of the live panels on that rack, left to right.
 *
 * The panels themselves, by the `data-testid` `<Panel>` has carried since task
 * 6, rather than the labels beside them: a canvas whose captions were in one
 * order and whose pictures were in another would satisfy an assertion about the
 * text alone, and the picture is the thing a person reads the rack from.
 */
function drawn(name: string): number[] {
  return canvasOf(name)
    .getAllByRole("listitem")
    .map((item) => {
      const live = item.querySelector("[data-testid^='panel-']")
      if (live === null) throw new Error("a rack item with no live panel in it")
      return Number(live.getAttribute("data-testid")?.slice("panel-".length))
    })
}

/** One `frame` message exactly as `ws_ui._frame_message` assembles it. */
function frameMessage(screenId: number, seq: number, bytes: Uint8Array): string {
  return JSON.stringify({
    type: "frame",
    screen_id: screenId,
    seq,
    webp: btoa(String.fromCharCode(...bytes)),
  })
}

const bytesFor = (mark: number) => new Uint8Array([0xff, 0xd8, mark, 0x00, mark])

// jsdom's `visibilityState` is a getter on `Document.prototype` that always
// answers "visible" and fires nothing. Both halves are stood in for: the
// property is redefined once for this file, and the event is dispatched by hand.
let visibility: DocumentVisibilityState = "visible"
Object.defineProperty(document, "visibilityState", {
  configurable: true,
  get: () => visibility,
})

function tabIs(state: DocumentVisibilityState) {
  visibility = state
  document.dispatchEvent(new Event("visibilitychange"))
}

afterEach(() => {
  visibility = "visible"
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("the screens page", () => {
  it("subscribes on mount and unsubscribes on unmount", async () => {
    // A closed tab that kept its subscription leaves the Pi encoding for nobody.
    // Asserted at the wire -- the `{action, screen_id}` messages the socket
    // really sent -- and not at the frame store, because the store knowing a
    // panel is on screen is not the Pi being told to stop.
    server.use(...reading([WEATHER, TRAINS, KITCHEN]), NO_EVENTS)
    const rig = mountScreens()
    await rig.connect()

    expect(await screen.findByRole("button", { name: "Weather" })).toBeInTheDocument()
    // Every panel the canvas shows, and only those: the canvas drives this for
    // all three, not just for the one an inspector happens to be open on.
    await waitFor(() => expect(watched(rig.live().requests)).toEqual([11, 12, 13]))

    // Leaving the page. A navigation rather than an unmount, because that is
    // how somebody leaves it -- and the shell, the socket and the provider all
    // stay exactly where they were, so nothing but the canvas going away can
    // account for what follows.
    await userEvent.click(screen.getByRole("link", { name: "Daemons" }))
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()

    await waitFor(() => expect(watched(rig.live().requests)).toEqual([]))
    // The same connection throughout: a page that had merely dropped its socket
    // would also stop the frames, and would not be this.
    expect(rig.live().state).toBe("open")
    for (const screenId of [11, 12, 13]) {
      expect(rig.live().requests).toContainEqual({ action: "unsubscribe", screen_id: screenId })
    }
  })

  it("unsubscribes when the tab is hidden", async () => {
    // document.visibilityState -- same reason. A tab on a second monitor is
    // three panels' worth of WebP a rack encodes twice a second for a page
    // nobody can see.
    server.use(...reading([WEATHER, TRAINS, KITCHEN]))
    const rig = mountScreens()
    await rig.connect()

    expect(await screen.findByRole("button", { name: "Weather" })).toBeInTheDocument()
    await waitFor(() => expect(watched(rig.live().requests)).toEqual([11, 12, 13]))

    act(() => rig.live().deliver(frameMessage(12, 104, bytesFor(12))))
    await act(() => rig.decoder.settleAll())
    const painted = rig.canvas.contexts.map((context) => context.ops)
    expect(painted.flat()).toContain("drawImage")

    act(() => tabIs("hidden"))

    expect(watched(rig.live().requests)).toEqual([])
    // On the wire and nowhere else. The panels are still mounted, so the store
    // still holds their bitmaps and the glass still shows them -- a tab coming
    // back to three black canvases that fill in half a second later is the
    // failure a wire-and-store hide would cause.
    expect(rig.canvas.contexts.map((context) => context.ops)).toEqual(painted)
    expect(rig.decoder.made[0].closed).toBe(false)
    expect(drawn("pi-loft")).toEqual([12, 13, 11])

    act(() => tabIs("visible"))
    expect(watched(rig.live().requests)).toEqual([11, 12, 13])
  })

  it("orders the canvas by position, not by id", async () => {
    // FIXTURE TRAP: ids and positions differ. Ids 11, 12, 13 sit at positions
    // 3, 1, 2, and the server answers `ORDER BY position, id` -- so the fixture
    // arrives 12, 13, 11 and an implementation that sorted by id renders
    // 11, 12, 13.
    server.use(...reading([WEATHER, TRAINS, KITCHEN]))
    const rig = mountScreens()
    await rig.connect()

    expect(await screen.findByRole("button", { name: "Weather" })).toBeInTheDocument()

    // The live panels, in the order they are drawn -- not merely that all three
    // are present.
    expect(drawn("pi-loft")).toEqual([12, 13, 11])
    // And the captions agree with the pictures.
    expect(
      canvasOf("pi-loft")
        .getAllByRole("listitem")
        .map((item) => within(item).getByRole("button", { pressed: false }).getAttribute("aria-label")),
    ).toEqual(["Weather", "Trains", "Kitchen"])

    // The ends of the rack have nowhere to go. Said with `disabled` rather than
    // by ignoring the press, because a button that does nothing when pressed is
    // one a keyboard user tabs to and learns nothing from.
    const rack = canvasOf("pi-loft")
    expect(rack.getByRole("button", { name: "Move Weather left" })).toBeDisabled()
    expect(rack.getByRole("button", { name: "Move Weather right" })).toBeEnabled()
    expect(rack.getByRole("button", { name: "Move Kitchen left" })).toBeEnabled()
    expect(rack.getByRole("button", { name: "Move Kitchen right" })).toBeDisabled()
  })

  it("puts nothing between a panel and the glass that would turn it", async () => {
    // Spec §5.4, twice over: "Panels are drawn exactly as received. No
    // rotation, no flip." All three of these are bolted in at 270 and one is
    // flipped, and the daemon streams the frame *before* it applies either --
    // so the picture already shows what a person standing at the rack sees, and
    // rotating it again would be wrong twice.
    //
    // `panel.test.tsx` pins the drawing itself: the context is handed
    // `rotate`, `scale` and `setTransform` by the recorder precisely so a
    // transforming panel dies on an assertion rather than a TypeError. What is
    // pinned here is the *other* way this page could turn a picture, and the
    // one only this page can: a transform on something wrapped around the
    // canvas, where the values it would be built from -- `rotation` and
    // `hflip` -- are in scope for the first time.
    server.use(...reading([WEATHER, TRAINS, KITCHEN]))
    const rig = mountScreens()
    await rig.connect()

    await screen.findByRole("list", { name: "Panels on pi-loft" })
    act(() => rig.live().deliver(frameMessage(13, 104, bytesFor(13))))
    await act(() => rig.decoder.settleAll())

    for (const item of canvasOf("pi-loft").getAllByRole("listitem")) {
      const glass = item.querySelector("canvas")
      expect(glass).not.toBeNull()
      // Every element from the canvas up to the rack item it sits in.
      for (let node: HTMLElement | null = glass; node !== null; node = node.parentElement) {
        expect(node.style.transform).toBe("")
        // And the class-shaped way of saying the same thing, which no
        // stylesheet is loaded to reveal: `rotate-90`, `-scale-x-100`.
        expect(node.className).not.toMatch(/rotate|scale-x/)
        if (node === item) break
      }
    }

    // And the draw itself, in this page's own tree: the complete list of what
    // was asked of the context, so anything added to it shows up here.
    const painted = rig.canvas.contexts.filter((context) => context.ops.length > 0)
    expect(painted).toHaveLength(1)
    expect(painted[0].ops).toEqual(["save", "beginPath", "arc", "clip", "drawImage", "restore"])
  })

  it("reorders through the API rather than locally", async () => {
    // What was sent, and what came back, are deliberately different: between
    // the click and the refetch another tab moved a panel, so the server's
    // order is 13, 11, 12 where this tab asked for 13, 12, 11. An interface
    // that drew the order it *sent* shows 13, 12, 11 and fails here.
    const sent: unknown[] = []
    let order = [WEATHER, TRAINS, KITCHEN]
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(order)),
      http.post("/api/screens/reorder", async ({ request }) => {
        sent.push(await request.json())
        order = [TRAINS, KITCHEN, WEATHER]
        return HttpResponse.json(order)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    expect(await screen.findByRole("button", { name: "Weather" })).toBeInTheDocument()
    expect(drawn("pi-loft")).toEqual([12, 13, 11])

    // Operable without a drag: a pointer-only reorder is unusable by keyboard,
    // and there is no synthetic drag a test could honestly perform either.
    await userEvent.click(canvasOf("pi-loft").getByRole("button", { name: "Move Trains left" }))

    // The ids the interface asked for, in the new left-to-right order. Every
    // screen named is renumbered from 1 by the server; the interface names all
    // of that rack's screens so no panel keeps a number this request did not
    // assign.
    expect(sent).toEqual([{ ids: [13, 12, 11] }])
    // And the canvas shows what the server has, not what this tab asked for.
    await waitFor(() => expect(drawn("pi-loft")).toEqual([13, 11, 12]))

    // A refusal is total: the route checks every id before it writes anything,
    // so a rack whose reorder was refused is in the order it was already in,
    // and an interface showing the order it sent would be showing a rack that
    // does not exist.
    server.use(
      http.post("/api/screens/reorder", () =>
        HttpResponse.json({ detail: "a screen may appear once in a reorder" }, { status: 422 }),
      ),
    )
    await userEvent.click(canvasOf("pi-loft").getByRole("button", { name: "Move Kitchen right" }))

    expect(await screen.findByText("a screen may appear once in a reorder")).toBeInTheDocument()
    expect(drawn("pi-loft")).toEqual([13, 11, 12])
  })

  it("gives each rack its own row, and reorders only the rack that moved", async () => {
    // `position` is a panel's ordinal *within its rack*, and the listing is
    // ordered by `position, id` across every rack at once -- so two racks
    // numbered from 1 arrive interleaved. One row for both would describe no
    // wall, and a reorder driven from it would name the other rack's screens
    // too: `POST /api/screens/reorder` renumbers *everything named*, from 1, so
    // the second rack's panels would come back numbered from the first rack's
    // positions. Nobody asked for that and nothing would say it had happened.
    const DOORBELL = panel({ id: 27, name: "Doorbell", position: 1, daemon_id: OTHER_RACK })
    const sent: unknown[] = []
    server.use(
      // In the order the server answers it: (1,12), (1,27), (2,13), (3,11).
      ...reading([WEATHER, DOORBELL, TRAINS, KITCHEN]),
      http.post("/api/screens/reorder", async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json([])
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await screen.findByRole("list", { name: "Panels on pi-cellar" })
    expect(drawn("pi-loft")).toEqual([12, 13, 11])
    expect(drawn("pi-cellar")).toEqual([27])

    // Each panel is told which rack it is on, and pi-cellar's is gone. Said
    // here because this is the only fixture with two racks in different states:
    // a canvas handing every panel a constant rack id -- the first screen's,
    // say -- draws Doorbell as live, and no assertion anywhere else in this
    // file could tell the difference.
    expect(screen.getByTestId("panel-27")).toHaveAttribute("data-state", "offline")
    expect(within(screen.getByTestId("panel-27")).getByText("Offline")).toBeInTheDocument()
    for (const id of [11, 12, 13]) {
      expect(screen.getByTestId(`panel-${id}`)).not.toHaveAttribute("data-state", "offline")
    }

    await userEvent.click(canvasOf("pi-loft").getByRole("button", { name: "Move Trains left" }))

    // Rack 8's screens, all of them, and nothing belonging to rack 42.
    expect(sent).toEqual([{ ids: [13, 12, 11] }])
  })

  it("edits a screen and shows which rack did not get it", async () => {
    // The PATCH is about screen 11, which is on rack 8. The header names 42 and
    // 63. No answer the server really gives separates those, which is the whole
    // reason the fixture does: a page that named the rack from the body's
    // `daemon_id` is indistinguishable from a correct one against any realistic
    // response. 63 is in no listing -- a rack another tab deleted between this
    // page's fetch and this write -- and is still a rack somebody has to go and
    // look at.
    let patched: unknown = null
    let patchedId: string | undefined
    // The list the server would answer *after* the write, which is the state
    // this page has to end up drawing. A PATCH that invalidated nothing leaves
    // the canvas and the inspector heading saying "Kitchen" for ever, and --
    // because "changed" is measured against the row the page holds -- leaves
    // Save enabled, so every further press re-sends the same field, bumps
    // `config_version` and pushes to the rack again.
    let rows = [WEATHER, TRAINS, KITCHEN]
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request, params }) => {
        patched = await request.json()
        patchedId = String(params.screen_id)
        const renamed = { ...KITCHEN, name: "Hallway" }
        rows = rows.map((row) => (row.id === KITCHEN.id ? renamed : row))
        return HttpResponse.json(renamed, {
          headers: { "X-Unservable-Daemons": `${OTHER_RACK},${UNLISTED_RACK}` },
        })
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))
    const named = await screen.findByRole("textbox", { name: "Name" })
    await userEvent.clear(named)
    await userEvent.type(named, "Hallway")
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    // Only the field that changed. `ScreenBody` forbids extra fields, so a body
    // carrying the whole `ScreenView` -- `id`, `daemon_id` -- is a 422; and a
    // body repeating every field the form holds overwrites whatever another tab
    // changed in the meantime with what this page last read.
    await waitFor(() => expect(patched).toEqual({ name: "Hallway" }))
    expect(patchedId).toBe("11")

    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-cellar")
    // An id with no row is named as an id rather than drawn as nothing.
    expect(notice).toHaveTextContent("daemon 63")
    // Not the rack the edited screen is on, which is the answer a page reading
    // the body instead of the header would give.
    expect(notice).not.toHaveTextContent("pi-loft")

    // And the page asked the server again. The write is what makes the list
    // stale, so the caption under the panel, the inspector's own heading and
    // the row every tab measures "changed" against all follow the rename.
    await waitFor(() =>
      expect(canvasOf("pi-loft").getByRole("button", { name: "Hallway" })).toBeInTheDocument(),
    )
    expect(canvasOf("pi-loft").queryByRole("button", { name: "Kitchen" })).not.toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Hallway" })).toBeInTheDocument()
    // The compounding half of it: with the fresh row in hand the form holds
    // nothing unsaved, so Save goes quiet. Against a stale row it stays live and
    // every press re-sends `{name: "Hallway"}`, bumping the rack's
    // `config_version` and pushing a snapshot again for an edit already made.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()
  })

  it("stops saying an edit landed once a form has moved on from it", async () => {
    // "Saved, and every rack was given it." is an answer about a write that has
    // happened. Left on screen while somebody edits three more boxes and walks
    // away, it is a reassurance about something else -- and the destructive
    // "not every rack was given that change" is worse, because it names racks
    // against an edit the user has since abandoned.
    //
    // All three tabs, because each reports this for itself: one rule, and one
    // place per form to forget it in.
    let rows = [WEATHER, TRAINS, KITCHEN]
    const saved = (over: Partial<ScreenRow>) => {
      const next = { ...KITCHEN, ...over }
      rows = rows.map((row) => (row.id === KITCHEN.id ? next : row))
      return HttpResponse.json(next)
    }
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      // Answering the row as it now is, which is what makes each form clean
      // again after its own write -- and the state in which the notice is the
      // only thing on screen saying anything about that write.
      http.get("/api/screens", () => HttpResponse.json(rows)),
      ...INSPECTING,
    )
    const rig = mountScreens()
    await rig.connect()
    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))

    // Config: saved from the switch, forgotten by the name box.
    server.use(http.patch("/api/screens/:screen_id", () => saved({ hflip: true })))
    await userEvent.click(await screen.findByRole("switch", { name: "Horizontal flip" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByText(/every rack was given it/i)).toBeInTheDocument()
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "!")
    expect(screen.queryByText(/every rack was given it/i)).not.toBeInTheDocument()

    // Data: saved from a param, forgotten by another param.
    server.use(
      http.patch("/api/screens/:screen_id", () => saved({ params: { title: "Hallway" } })),
    )
    await userEvent.click(screen.getByRole("tab", { name: "Data" }))
    const title = await screen.findByRole("textbox", { name: "Title" })
    await userEvent.clear(title)
    await userEvent.type(title, "Hallway")
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByText(/every rack was given it/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole("switch", { name: "Dimmed" }))
    expect(screen.queryByText(/every rack was given it/i)).not.toBeInTheDocument()

    // Sleep: saved by handing the window back, forgotten by taking one again.
    server.use(http.patch("/api/screens/:screen_id", () => saved({ sleep_override: null })))
    await userEvent.click(screen.getByRole("tab", { name: "Sleep" }))
    await userEvent.click(await screen.findByRole("switch", { name: "Override for this panel" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByText(/every rack was given it/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole("switch", { name: "Override for this panel" }))
    expect(screen.queryByText(/every rack was given it/i)).not.toBeInTheDocument()
  })

  it("follows a reorder in the boxes nobody has touched, and keeps the ones they have", async () => {
    // A reorder moves the panel the inspector is open on. The row changes on the
    // server, the list is refetched, and a Position box seeded once at mount
    // still holds the ordinal the panel used to have -- so the form *differs*
    // from the row, Save lights up for an edit nobody made, and pressing it
    // PATCHes the panel back where it just came from. An interface offering a
    // button that silently reverses what somebody just did is the defect; the
    // rule that closes it is that an untouched control follows the row.
    //
    // And the other half of the rule in the same test, because a fix that
    // reseeded *everything* would pass the first half and throw away what
    // somebody was typing: a touched box keeps what they put in it.
    const sent: unknown[] = []
    const bodies: unknown[] = []
    let rows = [WEATHER, TRAINS, KITCHEN]
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      ...INSPECTING,
      // `POST /api/screens/reorder` renumbers everything named, from 1, and
      // answers the rack as it now is. Computed from the request rather than
      // hard-coded, because this test drives two reorders.
      http.post("/api/screens/reorder", async ({ request }) => {
        const body = (await request.json()) as { ids: number[] }
        sent.push(body)
        rows = body.ids.flatMap((id, index) => {
          const row = rows.find((each) => each.id === id)
          return row === undefined ? [] : [{ ...row, position: index + 1 }]
        })
        return HttpResponse.json(rows)
      }),
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(rows.find((row) => row.id === TRAINS.id) ?? TRAINS)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Trains" }))
    const box = await screen.findByRole("spinbutton", { name: "Position" })
    expect(box).toHaveValue(2)
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()

    await userEvent.click(canvasOf("pi-loft").getByRole("button", { name: "Move Trains left" }))
    await waitFor(() => expect(drawn("pi-loft")).toEqual([13, 12, 11]))

    // The box nobody touched says where the panel now is, and there is nothing
    // to save. Against a box frozen at mount it reads 2, Save is live, and the
    // press undoes the move.
    expect(box).toHaveValue(1)
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()
    expect(screen.getByText("Position 1 on pi-loft")).toBeInTheDocument()

    // Now one box is touched and the rack moves under it again.
    const named = screen.getByRole("textbox", { name: "Name" })
    await userEvent.type(named, " (loft)")
    await userEvent.click(canvasOf("pi-loft").getByRole("button", { name: "Move Trains right" }))
    await waitFor(() => expect(drawn("pi-loft")).toEqual([12, 13, 11]))

    expect(sent).toEqual([{ ids: [13, 12, 11] }, { ids: [12, 13, 11] }])
    // What somebody typed survived the refetch; what they did not touch followed
    // it.
    expect(named).toHaveValue("Trains (loft)")
    expect(box).toHaveValue(2)

    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    // The name and nothing else. A `position` in this body is the reorder being
    // undone by the save, which is the whole defect.
    await waitFor(() => expect(bodies).toEqual([{ name: "Trains (loft)" }]))
  })

  it("forgets a write once the form has moved past it, even mid-flight", async () => {
    // The same rule, on the other side: "Saved, and every rack was given it." --
    // and the destructive unservable alert with it -- has to go when the form
    // moves past the write it describes, *including* when the moving happened
    // while that write was still on the wire. The report of an edit made during
    // a PATCH cannot be acted on at the time, because `reset()` would throw away
    // an outcome nobody has seen; if it is dropped rather than deferred there is
    // no second clean-to-unsaved edge afterwards to clear the notice, and it
    // stands over a form holding newer edits.
    const patched: unknown[] = []
    let rows = [WEATHER, TRAINS, KITCHEN]
    const flight = held()
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched.push(await request.json())
        await flight.promise
        const next = { ...KITCHEN, hflip: true }
        rows = rows.map((row) => (row.id === KITCHEN.id ? next : row))
        return HttpResponse.json(next)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))
    await userEvent.click(await screen.findByRole("switch", { name: "Horizontal flip" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() => expect(patched).toEqual([{ hflip: true }]))
    // Still on the wire: nothing has been said about it yet.
    expect(screen.queryByText(/every rack was given it/i)).not.toBeInTheDocument()

    // Changed their mind while it was in flight -- the switch goes back, so the
    // form matches the row again -- and then typed. That second edit is the one
    // that has to survive being made at the wrong moment.
    await userEvent.click(screen.getByRole("switch", { name: "Horizontal flip" }))
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "!")

    await act(async () => {
      flight.release()
    })
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    )

    // The write landed and the row came back with it, so the switch -- touched,
    // and therefore kept -- now disagrees with the row and the form is unsaved.
    expect(screen.getByRole("switch", { name: "Horizontal flip" })).toHaveAttribute(
      "aria-checked",
      "false",
    )
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Kitchen!")
    // And nothing on screen claims that state was saved.
    expect(screen.queryByText(/every rack was given it/i)).not.toBeInTheDocument()

    // The notice is reachable in this fixture, which is what stops the assertion
    // above from passing because something else went wrong.
    server.use(
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched.push(await request.json())
        const next = { ...KITCHEN, name: "Kitchen!", hflip: false }
        rows = rows.map((row) => (row.id === KITCHEN.id ? next : row))
        return HttpResponse.json(next)
      }),
    )
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByText(/every rack was given it/i)).toBeInTheDocument()
    expect(patched).toEqual([{ hflip: true }, { name: "Kitchen!", hflip: false }])
  })

  it("changes the template a panel draws, and sends nothing else with it", async () => {
    // The other half of "only the changed field", on a field that is not text:
    // the select is the control, and what goes on the wire is one key.
    let patched: unknown = null
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched = await request.json()
        return HttpResponse.json({ ...KITCHEN, template: "big-number" })
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))
    // What the screen already names, from the row and not from the first row of
    // the template list.
    expect(await screen.findByRole("combobox", { name: "Template" })).toHaveTextContent(
      "ring-gauge",
    )

    // Nothing has been changed yet, so there is nothing to save. Not a nicety:
    // an empty PATCH is accepted, and it bumps the rack's config_version,
    // records an edit nobody made and pushes a fresh snapshot to the Pi.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()

    // Choosing another panel builds a fresh form rather than leaving this one's
    // values in the boxes. Without that, the next Save writes Kitchen's
    // template onto Weather -- the form holds what was loaded, and what was
    // loaded was another screen.
    await userEvent.click(screen.getByRole("button", { name: "Weather" }))
    expect(screen.getByRole("combobox", { name: "Template" })).toHaveTextContent("big-number")
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Weather")
    await userEvent.click(screen.getByRole("button", { name: "Kitchen" }))

    const chosen = screen.getByRole("combobox", { name: "Template" })
    expect(chosen).toHaveTextContent("ring-gauge")
    await userEvent.click(chosen)
    await userEvent.click(screen.getByRole("option", { name: "big-number" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() => expect(patched).toEqual({ template: "big-number" }))
    // Saved, and the racks all got it -- the header was absent, which is not
    // the same as the header being empty and must not be drawn as a failure.
    expect(await screen.findByText(/every rack was given it/i)).toBeInTheDocument()
    expect(screen.queryByText(/nothing was sent/i)).not.toBeInTheDocument()
  })

  it("writes the wiring the boxes say, and an unset pin as nothing at all", async () => {
    // The wiring is the only non-trivial logic on this page, and every part of
    // it decides what is written to a Pi: an empty pin box is `null` -- "not
    // set" -- and never GPIO 0. `DisplayConfig.dc` and `.rst` have no default
    // for exactly that reason: defaulting them to 0 described a panel driving
    // data/command and reset off GPIO0, a board that does not exist, and made
    // `DisplayConfig(backend="gc9a01")` a valid description of nothing that
    // failed hours later as an SPI error on the rack. An empty *clock speed*, by
    // contrast, is the model's own 40 MHz default, because `hz` has one.
    //
    // Driven on the virtual backend so the document this sends is one the
    // server accepts: `dc` and `rst` are only required by gc9a01, and this is
    // the wiring change that legitimately clears them.
    let patched: unknown = null
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched = await request.json()
        return HttpResponse.json(TRAINS)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Trains" }))
    // The row's own wiring, narrowed out of a column the server answers as
    // `dict[str, Any]` -- not the schema's defaults and not the panel next to it.
    expect(await screen.findByRole("spinbutton", { name: "DC pin" })).toHaveValue(22)
    expect(screen.getByRole("spinbutton", { name: "RST pin" })).toHaveValue(23)
    expect(screen.getByRole("spinbutton", { name: "SPI chip select" })).toHaveValue(1)
    expect(screen.getByRole("combobox", { name: "Backend" })).toHaveTextContent("gc9a01")
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()

    // The select first, then the text boxes: a Radix select opened immediately
    // after an input was edited produces `act` warnings from its focus scope in
    // this harness, which is written up in the task 8 report.
    await userEvent.click(screen.getByRole("combobox", { name: "Backend" }))
    await userEvent.click(screen.getByRole("option", { name: "virtual" }))
    await userEvent.type(
      screen.getByRole("textbox", { name: "Output directory" }),
      "/var/lib/ors/panels",
    )
    await userEvent.clear(screen.getByRole("spinbutton", { name: "DC pin" }))
    await userEvent.clear(screen.getByRole("spinbutton", { name: "RST pin" }))
    // Emptied, so the model's default rather than 0 Hz -- an SPI clock of zero
    // is not a slower panel, it is a panel that never clocks a byte out.
    await userEvent.clear(screen.getByRole("spinbutton", { name: "Clock speed" }))
    await userEvent.clear(screen.getByRole("spinbutton", { name: "SPI bus" }))
    await userEvent.type(screen.getByRole("spinbutton", { name: "SPI bus" }), "1")

    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    // The exact document, and only it. `display` is one column and one model, so
    // it goes whole when any part of it moves -- and nothing else the form holds
    // goes with it.
    await waitFor(() =>
      expect(patched).toEqual({
        display: {
          backend: "virtual",
          spi_bus: 1,
          spi_cs: 1,
          dc: null,
          rst: null,
          hz: 40_000_000,
          out_dir: "/var/lib/ors/panels",
        },
      }),
    )
  })

  it("says which wiring rule the server refused, and not that it refused", async () => {
    // `DisplayConfig`'s two rules are a `model_validator`, so a refusal arrives
    // as FastAPI's validation report -- `detail` is a *list*, never a sentence.
    // A client that could only read a string detail answered "the server refused
    // the change" here, on a form whose entire subject is which pin is soldered
    // where. The pin numbers themselves stay unvalidated in this interface:
    // which GPIO lines exist is a fact about the board, and the schema
    // deliberately has no range check and no `dc != rst` rule.
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", () =>
        HttpResponse.json(
          {
            detail: [
              {
                type: "value_error",
                loc: ["body", "display"],
                msg: "Value error, a gc9a01 display needs dc",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Weather" }))
    await userEvent.clear(await screen.findByRole("spinbutton", { name: "DC pin" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent("display: Value error, a gc9a01 display needs dc")
    expect(refusal).not.toHaveTextContent(/the server refused the change/i)
  })

  it("asks for no position when the box says nothing, and for the switches it shows", async () => {
    // `Number("")` is `0`, and `Number.isFinite(0)` is true -- the pair that has
    // now bitten this project three times. An emptied Position box read as a
    // number is a request to move the panel to position 0, which
    // `ScreenBody.position` (`ge=1`) refuses with a validation report; and the
    // `min={1}` on the input is HTML, enforced by nothing here. An empty box is
    // not an edit.
    let patched: unknown = null
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched = await request.json()
        return HttpResponse.json({ ...WEATHER, position: 4, hflip: true, enabled: false })
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Weather" }))
    const box = await screen.findByRole("spinbutton", { name: "Position" })
    expect(box).toHaveValue(1)

    await userEvent.clear(box)
    // Nothing to save: the panel keeps the position it has, and the form says so
    // rather than leaving somebody to find out from a 422.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()
    expect(screen.getByText(/^Empty, so this panel keeps the position it has/)).toBeInTheDocument()

    // And a box holding something that is not a position either. `position` is
    // an `int` on the far end, so 2.5 is a validation report and not a place on
    // a wall; the panel keeps the one it has and the line under the box says
    // which of the two states it is in, because "empty" would be a lie here.
    await userEvent.type(box, "2.5")
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()
    expect(
      screen.getByText(/^Not a whole number, so this panel keeps the position it has/),
    ).toBeInTheDocument()
    await userEvent.clear(box)

    await userEvent.type(box, "4")
    await userEvent.click(screen.getByRole("switch", { name: "Horizontal flip" }))
    await userEvent.click(screen.getByRole("switch", { name: "Enabled" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    // A number, not the box's text, and the two switches as booleans.
    await waitFor(() => expect(patched).toEqual({ position: 4, hflip: true, enabled: false }))
  })

  it("gives one panel its own night window, and hands it back", async () => {
    // The Sleep tab. Turning the override off is `sleep_override: null`, which
    // is a change and not an absence: the route's `_columns` uses
    // `exclude_unset` rather than `exclude_none` precisely so that "this screen
    // has stopped overriding the rack" is sayable at all.
    const bodies: unknown[] = []
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(KITCHEN)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    // A panel that overrides. Its own times are shown, not the server's.
    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))
    await userEvent.click(screen.getByRole("tab", { name: "Sleep" }))
    expect(await screen.findByText(/Dark between 22:00 and 08:00/)).toBeInTheDocument()
    expect(screen.getByRole("switch", { name: "Override for this panel" })).toHaveAttribute(
      "aria-checked",
      "true",
    )
    expect(screen.getByLabelText("Dark from")).toHaveValue("22:30")
    expect(screen.getByLabelText("Light again at")).toHaveValue("06:15")
    // And nothing to save until one of them moves: writing the window a panel
    // already keeps is an edit nobody made, with a push to the rack behind it.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()

    await userEvent.click(screen.getByRole("switch", { name: "Override for this panel" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() => expect(bodies).toEqual([{ sleep_override: null }]))

    // And a panel that overrides nothing gets a window of its own, starting
    // from **the window it is already keeping** -- the server's 22:00/08:00,
    // from `GET /api/settings` -- rather than from `NightWindow`'s own defaults
    // of 23:00/07:00 or from the panel beside it, whose override is
    // 22:30/06:15. All three are different in this fixture precisely so the
    // assertion below can only be satisfied one way: turning the override on
    // must change *when the panel is dark* by nothing at all, and a form that
    // fell back to the schema would move it an hour each way without anybody
    // asking.
    await userEvent.click(screen.getByRole("button", { name: "Weather" }))
    await userEvent.click(screen.getByRole("tab", { name: "Sleep" }))
    expect(
      await screen.findByRole("switch", { name: "Override for this panel" }),
    ).toHaveAttribute("aria-checked", "false")
    await userEvent.click(screen.getByRole("switch", { name: "Override for this panel" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() =>
      expect(bodies).toEqual([
        { sleep_override: null },
        { sleep_override: { enabled: true, start: "22:00", end: "08:00" } },
      ]),
    )
  })

  it("deletes the panel it named, and the rest of the rack survives", async () => {
    const deleted: number[] = []
    let rows = [WEATHER, TRAINS, KITCHEN]
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      ...INSPECTING,
      http.delete("/api/screens/:screen_id", ({ params }) => {
        const id = Number(params.screen_id)
        deleted.push(id)
        rows = rows.filter((row) => row.id !== id)
        // The default 200 carrying the id it removed. No M3a route answers 204.
        return HttpResponse.json({ deleted: id })
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Trains" }))
    await userEvent.click(screen.getByRole("button", { name: "Delete this panel" }))

    const dialog = within(await screen.findByRole("dialog"))
    // The panel it is about, so a confirmation cannot be read against the wrong
    // one, and what goes with it.
    expect(dialog.getByRole("heading", { name: "Delete Trains?" })).toBeInTheDocument()
    expect(dialog.getByText(/cannot be undone/i)).toBeInTheDocument()
    await userEvent.click(dialog.getByRole("button", { name: "Delete the panel" }))

    // The id that was on the wire is the panel the confirmation named -- not 1,
    // which is what an index would have said, and not either of the others.
    await waitFor(() => expect(drawn("pi-loft")).toEqual([12, 11]))
    expect(deleted).toEqual([13])
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    // And the inspector let go of the panel that no longer exists.
    expect(screen.queryByRole("textbox", { name: "Name" })).not.toBeInTheDocument()
  })

  it("edits the template's params, and offers the readings a binding can name", async () => {
    // The Data tab. `ParamSpec.type` decides the control; the value written for
    // a `binding` is a plain string carrying `{{ ... }}`, which is what
    // `ors_render.render.expand_params` resolves and what
    // `ors_daemon.config._dependencies` reads the integration name out of.
    let patched: unknown = null
    server.use(
      ...reading([WEATHER, TRAINS, KITCHEN]),
      ...INSPECTING,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        patched = await request.json()
        return HttpResponse.json(KITCHEN)
      }),
    )
    const rig = mountScreens()
    await rig.connect()

    await userEvent.click(await screen.findByRole("button", { name: "Kitchen" }))
    await userEvent.click(screen.getByRole("tab", { name: "Data" }))

    // One control per param, labelled from the schema, typed by `type`, and
    // filled from the screen's own value where it has one and the template's
    // default where it has not.
    // `title` is the screen's own; everything else falls back to the template's
    // declared default, which is what `Template.bind_params` would have used.
    const title = await screen.findByRole("textbox", { name: "Title" })
    expect(title).toHaveValue("Kitchen")
    expect(screen.getByRole("textbox", { name: "Value 0-100" })).toHaveValue("0")
    expect(screen.getByRole("textbox", { name: "Palette" })).toHaveValue("cyan")
    expect(screen.getByRole("switch", { name: "Dimmed" })).toHaveAttribute("aria-checked", "false")
    // A default that is a document is shown and left alone. Writing the JSON
    // back as a string would turn a palette into text the renderer cannot use,
    // and this editor writes single values.
    const thresholds = screen.getByRole("textbox", { name: "Thresholds" })
    expect(thresholds).toHaveValue('{"stops":[{"at":80,"color":"#ff0000"}]}')
    expect(thresholds).toHaveAttribute("readonly")
    expect(
      screen.queryByRole("combobox", { name: "Insert a reading into Thresholds" }),
    ).not.toBeInTheDocument()

    // Nothing has been touched, so there is nothing to save -- and this tab has
    // to say so for itself. An empty `params` PATCH is accepted, bumps the
    // rack's `config_version` and pushes a snapshot for an edit nobody made;
    // the Config tab's own version of this assertion pins only the Config tab.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled()

    // The readings this rack's integrations really publish, offered by the name
    // a binding has to use: the integration's own `name`, then the field.
    await userEvent.click(screen.getByRole("combobox", { name: "Insert a reading into Hint line" }))
    expect(
      within(screen.getByRole("listbox")).getAllByRole("option").map((each) => each.textContent),
    ).toEqual([
      "{{prom.cpu}}",
      // A `top` field answers `{node, value}`, so the two halves are offered
      // separately -- `{{prom.cpu_hot}}` alone binds a dict, which draws as one.
      "{{prom.cpu_hot.node}}",
      "{{prom.cpu_hot.value}}",
      // And nothing from `spare`, which is on this rack and switched off: it is
      // never polled, so `{{spare.disk}}` would resolve to nothing and the field
      // bound to it would silently draw blank.
    ])
    await userEvent.click(screen.getByRole("option", { name: "{{prom.cpu_hot.node}}" }))
    expect(screen.getByRole("textbox", { name: "Hint line" })).toHaveValue("{{prom.cpu_hot.node}}")

    await userEvent.click(screen.getByRole("switch", { name: "Dimmed" }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    // `params` is one column and one field of `ScreenBody`, so the whole map
    // goes -- and only the keys that moved are in it. `title` was the screen's
    // own before this edit and is carried through unchanged; `value`, `palette`
    // and `thresholds` are the template's defaults, untouched, and writing them
    // would freeze today's default into this screen forever.
    await waitFor(() =>
      expect(patched).toEqual({
        params: { title: "Kitchen", hint: "{{prom.cpu_hot.node}}", dimmed: true },
      }),
    )
  })
})
