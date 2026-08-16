import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, screen, within } from "@testing-library/react"
import { Profiler, StrictMode, type ReactNode } from "react"
import { MemoryRouter } from "react-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import { daemonsKey, type Daemon } from "../src/api/queries"
import { AppShell } from "../src/components/AppShell"
import { Panel, STALE_AFTER_MS } from "../src/components/Panel"
import { LiveProvider } from "../src/live/LiveProvider"
import { ThemeProvider } from "../src/theme/theme-provider"
import { collectSockets, type FakeSocket } from "./fake-socket"
import { recordCanvas, stubDecoder, type BitmapDecoder, type CanvasRecorder } from "./paint"

// Racks 7 and 23, screens 41, 12, 58, 31, 77, 63 and 29. No rack id is a screen
// id and no id is its own index in the arrays below, so a panel that read the
// wrong rack's `online`, or delivered by index, cannot pass by coincidence.
//
// The stronger property -- that a panel's own rack is *second* in the daemons
// list, which is what kills "read the first rack you find" -- holds in every
// test that reads a rack's `online`, and those are the ones it has to hold in:
// `says offline, not stale, when its rack has gone` puts panel 58 on rack 23
// with rack 7 ahead of it. It is not claimed of the whole file, and it is not
// true there: `puts the racks the socket names online` has its panel on rack 7,
// which is first, and asserts the cache rather than anything the panel read.
// Deliberately **not** `RackCanvas`'s exported `PANEL_SIZE`, and deliberately a
// different number from it. `<Panel>` takes `size` as a prop and has to draw at
// whatever it is handed; a file that tested it at the one value the rack happens
// to pass could not tell a panel that honours the prop from one that hardcodes
// 160. What the rack passes is a decision of the rack's, and it is pinned where
// that decision is made -- `screens.test.tsx`, on the canvas element's own
// `width`.
const PANEL_SIZE = 120

/** One rack as `GET /api/daemons` reports it, with only the fields this task reads. */
function rack(id: number, name: string, online: boolean): Daemon {
  return {
    id,
    name,
    online,
    status: online ? "connected" : "offline",
    config_version: 9,
    applied_version: 9,
    config_error: null,
    version: "0.1.0",
    capabilities: {},
    last_seen: null,
    paired_at: null,
    created_at: "2026-08-15T00:00:00Z",
  }
}

/**
 * One `frame` message exactly as `ws_ui._frame_message` assembles it.
 *
 * `btoa` is the standard alphabet with padding, which is what that function
 * guarantees and what `socket.ts` decodes with no substitution. Building the
 * message here rather than pushing into the store directly is the point of
 * these tests: it drives the whole path the browser runs, socket to canvas.
 */
function frameMessage(screenId: number, seq: number, bytes: Uint8Array): string {
  return JSON.stringify({
    type: "frame",
    screen_id: screenId,
    seq,
    webp: btoa(String.fromCharCode(...bytes)),
  })
}

const bytesFor = (mark: number) => new Uint8Array([0xff, 0xd8, mark, 0x00, mark])

type Rig = {
  sockets: FakeSocket[]
  /** The connection the interface is actually holding. See `collectSockets`. */
  live(): FakeSocket
  decoder: BitmapDecoder
  canvas: CanvasRecorder
  queryClient: QueryClient
}

/**
 * Mount something under the providers `main.tsx` mounts the interface in.
 *
 * `StrictMode` because that is what ships, and because the two effects this
 * task adds -- the panel's subscription and the provider's connection -- are
 * both mounted, torn down and mounted again by it. Nothing here may depend on
 * an effect running once.
 *
 * The QueryClient is built fresh per call so no rack an earlier test cached can
 * decide what this one draws, and `queryFn` is never reached: the daemons query
 * in this task is read-only, written by the socket and by nothing else, so no
 * test here makes a network request and MSW has nothing to refuse.
 *
 * `strict: false` is for the one kind of test that counts renders, where
 * StrictMode's deliberate double render would make the number mean mounts
 * rather than renders. Nothing else may use it.
 */
function mount(children: ReactNode, racks?: Daemon[], { strict = true } = {}) {
  const canvas = recordCanvas()
  const decoder = stubDecoder()
  const sockets = collectSockets()
  const queryClient = new QueryClient()
  if (racks !== undefined) queryClient.setQueryData(daemonsKey, racks)

  const tree = <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  const view = render(strict ? <StrictMode>{tree}</StrictMode> : tree)

  const rig: Rig = {
    sockets,
    live() {
      const socket = sockets.at(-1)
      if (socket === undefined) throw new Error("the interface dialled nothing")
      return socket
    },
    decoder,
    canvas,
    queryClient,
  }
  return { ...view, ...rig }
}

/** A panel of screen `screenId`, on rack `daemonId`, under a live connection. */
function panelOf(screenId: number, daemonId: number) {
  return (
    <LiveProvider>
      <Panel screenId={screenId} daemonId={daemonId} size={PANEL_SIZE} />
    </LiveProvider>
  )
}

const panel = (screenId: number) => screen.getByTestId(`panel-${screenId}`)

/**
 * Hide or show the tab, exactly as a browser does it.
 *
 * jsdom's `visibilityState` is a getter on `Document.prototype` that always
 * answers "visible" and fires nothing, so both halves are stood in for: the
 * property is redefined once for this file, and the event is dispatched by
 * hand. Substituting the environment, not the code -- what `LiveProvider`
 * listens to and reads is what a browser gives it.
 */
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
  // Back to a visible tab, or the next test in this file inherits a hidden one
  // and subscribes nothing.
  visibility = "visible"
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("a panel", () => {
  it("draws the frame exactly as it arrives, without rotating it", async () => {
    // rotation describes how the panel is bolted in; the daemon already streams
    // the pre-correction image. Rotating here would be wrong twice.
    const bytes = bytesFor(41)
    const rig = mount(panelOf(41, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(41, 104, bytes)))
    await act(() => rig.decoder.settleAll())

    const context = rig.canvas.only()
    // The whole draw, in order: clip to a circle, put the image in it, and put
    // the context back. Asserted as the complete list rather than as a search,
    // so anything added to the draw -- a rotate, a flip, a clear -- shows up
    // here as a call nobody asked for.
    expect(context.ops).toEqual(["save", "beginPath", "arc", "clip", "drawImage", "restore"])
    expect(context.drawn[0].args).toEqual([
      rig.decoder.made[0],
      0,
      0,
      PANEL_SIZE,
      PANEL_SIZE,
    ])
    // The circle is the panel: a GC9A01 is round and the corners of a square
    // image are not on the glass.
    expect(context.calls[2].args).toEqual([
      PANEL_SIZE / 2,
      PANEL_SIZE / 2,
      PANEL_SIZE / 2,
      0,
      Math.PI * 2,
    ])
    // Said again by name, because this is the rule the spec states twice and
    // the one a well-meaning change would break: all four of the user's panels
    // are mounted at 270, and the browser must not know or care.
    for (const forbidden of ["rotate", "scale", "setTransform", "transform", "translate"]) {
      expect(context.ops).not.toContain(forbidden)
    }
    // And the bytes really made the round trip: standard-alphabet base64 off
    // the wire, into the blob the browser decodes.
    expect([...new Uint8Array(await rig.decoder.blobs[0].arrayBuffer())]).toEqual([...bytes])
  })

  it("goes stale when no frame has arrived for a while", async () => {
    // vi.useFakeTimers; there is no stalled-stream event by design.
    vi.useFakeTimers()
    // The rack this panel is on is *online* throughout, so "offline" is an
    // available wrong answer and staleness is not being read off the rack.
    const rig = mount(panelOf(12, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(12, 104, bytesFor(12))))
    await act(() => rig.decoder.settleAll())
    expect(panel(12)).toHaveAttribute("data-state", "live")

    // The number is derived, not chosen by feel: the daemon is asked for 2 fps,
    // so a frame is due every 500 ms, and this is four of them.
    expect(STALE_AFTER_MS).toBe(2000)

    const drawnWhileLive = rig.canvas.only().ops
    await act(() => vi.advanceTimersByTimeAsync(STALE_AFTER_MS - 1))
    // One millisecond short of the threshold. A panel that went stale here
    // would flash on every hiccup in the stream.
    expect(panel(12)).toHaveAttribute("data-state", "live")
    expect(screen.queryByText("Stale")).not.toBeInTheDocument()

    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(panel(12)).toHaveAttribute("data-state", "stale")
    expect(screen.getByText("Stale")).toBeInTheDocument()
    // Marked stale, not blanked: blank would read as a dead rack.
    expect(rig.canvas.only().ops).toEqual(drawnWhileLive)

    // And the next frame clears it, without a reload.
    act(() => rig.live().deliver(frameMessage(12, 105, bytesFor(12))))
    await act(() => rig.decoder.settleAll())
    expect(panel(12)).toHaveAttribute("data-state", "live")
    expect(rig.canvas.only().drawn).toHaveLength(2)
  })

  it("says offline, not stale, when its rack has gone", async () => {
    // `daemons` is the signal. Never inferred from frames stopping.
    vi.useFakeTimers()
    const rig = mount(panelOf(58, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(58, 104, bytesFor(58))))
    await act(() => rig.decoder.settleAll())
    expect(panel(58)).toHaveAttribute("data-state", "live")

    // Rack 23 is no longer in the online list. This is the only thing that
    // reports a Pi has gone -- it covers the reboot, the pulled cable and the
    // wifi blip alike, and none of them is visible as anything else.
    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [7] })))

    expect(panel(58)).toHaveAttribute("data-state", "offline")
    expect(screen.getByText("Offline")).toBeInTheDocument()
    expect(screen.queryByText("Stale")).not.toBeInTheDocument()
    // Said at once -- no clock was advanced between the message and the
    // assertion above. A rack that has gone is known to have gone, and waiting
    // out the staleness threshold to say so would be the interface inferring
    // from silence what it had been told outright.

    // The other direction of the same rule: a frame arriving from a rack the
    // server says is gone does not make it online again. Only `daemons` does.
    act(() => rig.live().deliver(frameMessage(58, 105, bytesFor(58))))
    await act(() => rig.decoder.settleAll())
    expect(panel(58)).toHaveAttribute("data-state", "offline")

    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [7, 23] })))
    expect(panel(58)).toHaveAttribute("data-state", "live")
  })

  it("keeps its last image when the socket drops", async () => {
    // Blank would read as a dead rack.
    vi.useFakeTimers()
    const rig = mount(panelOf(31, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(31, 104, bytesFor(31))))
    await act(() => rig.decoder.settleAll())
    const painted = rig.canvas.only().ops

    act(() => rig.live().drop())

    // Nothing was drawn, cleared or filled in response to the link going away.
    expect(rig.canvas.only().ops).toEqual(painted)
    expect(panel(31)).toHaveAttribute("data-state", "live")
    // A dropped socket is not a rack that has gone. The server is what stopped
    // answering, and the rack is still rendering -- saying "offline" here would
    // be the interface inventing a server state it was never told.
    expect(screen.queryByText("Offline")).not.toBeInTheDocument()

    // It does go stale, on its own clock, with the picture still there.
    await act(() => vi.advanceTimersByTimeAsync(STALE_AFTER_MS))
    expect(panel(31)).toHaveAttribute("data-state", "stale")
    expect(rig.canvas.only().ops).toEqual(painted)
    expect(rig.decoder.made[0].closed).toBe(false)
  })

  it("puts the racks the socket names online into the query cache, and invents none", async () => {
    // The bridge the online dots are drawn from: `setQueryData`, so nothing
    // refetches. Rack 7 starts online and rack 23 starts offline, so a bridge
    // that wrote a constant, or ignored the message, differs from one that read
    // it in both directions at once.
    const rig = mount(panelOf(77, 7), [rack(7, "Loft", true), rack(23, "Attic", false)])
    act(() => rig.live().accept())

    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [23, 96] })))

    const racks = rig.queryClient.getQueryData<Daemon[]>(daemonsKey)
    expect(racks?.map((entry) => [entry.id, entry.online])).toEqual([
      [7, false],
      [23, true],
    ])
    // Rack 96 is one this browser has never fetched. The server's own list is
    // the only thing that says a rack exists, and a client that appended it
    // would be inventing a row -- with no name, no versions and no `status` --
    // for the Daemons page to render.
    expect(racks).toHaveLength(2)
    // The rest of each row survived the patch: only `online` is the socket's to
    // say.
    expect(racks?.[0].name).toBe("Loft")
    expect(racks?.[0].config_version).toBe(9)
  })

  it("writes no daemons at all into a cache that has never been filled", () => {
    // Nothing has fetched `GET /api/daemons` yet -- the ordinary first second of
    // a page load. The socket knows which racks are up but not what they are,
    // and a cache seeded from it would make the Daemons page render rows the
    // server never sent.
    const rig = mount(panelOf(63, 23))
    act(() => rig.live().accept())
    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [7, 23] })))

    expect(rig.queryClient.getQueryData(daemonsKey)).toBeUndefined()
    // And a panel with nothing cached is not offline: the interface has not been
    // told anything about its rack, which is not the same as being told it is
    // gone.
    expect(panel(63)).toHaveAttribute("data-state", "live")
  })

  it("dials /ws/ui on the origin the interface was served from", () => {
    // One origin, by design: the server serves the interface below its own API,
    // so the session cookie needs no CORS and this socket needs no second host.
    const rig = mount(panelOf(41, 23))

    expect(rig.live().url).toBe(`${window.location.origin.replace(/^http/, "ws")}/ws/ui`)
    expect(rig.live().url).toBe("ws://localhost:3000/ws/ui")
    // Two mounts of the provider's effect, one live connection: StrictMode
    // mounts it twice and the first was closed rather than left dialling.
    expect(rig.sockets).toHaveLength(2)
    expect(rig.sockets.map((socket) => socket.state)).toEqual(["closed", "connecting"])
  })

  it("watches a screen while a panel is on it, and only while", async () => {
    // Frames flow only while someone is watching: a tab that kept its
    // subscription would leave the Pi encoding WebP for nobody.
    function Rack({ show, inspector }: { show: boolean; inspector: boolean }) {
      return (
        <LiveProvider>
          {show && <Panel screenId={12} daemonId={23} size={PANEL_SIZE} />}
          {inspector && <Panel screenId={12} daemonId={23} size={PANEL_SIZE} />}
        </LiveProvider>
      )
    }

    const rig = mount(<Rack show inspector={false} />)
    act(() => rig.live().accept())
    expect(rig.live().requests).toEqual([{ action: "subscribe", screen_id: 12 }])

    // A second panel on the same screen -- the inspector, beside the canvas.
    // One screen, one subscription: the server pays a whole-rack recomputation
    // for a repeat it then treats as a no-op.
    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <Rack show inspector />
        </QueryClientProvider>
      </StrictMode>,
    )
    expect(rig.live().requests).toHaveLength(1)

    // The canvas goes, the inspector stays: still watched.
    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <Rack show={false} inspector />
        </QueryClientProvider>
      </StrictMode>,
    )
    expect(rig.live().requests).toHaveLength(1)

    // And the last one goes.
    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <Rack show={false} inspector={false} />
        </QueryClientProvider>
      </StrictMode>,
    )
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 12 },
      { action: "unsubscribe", screen_id: 12 },
    ])
  })

  it("watches nothing while the tab is hidden, and everything again when it comes back", async () => {
    // Spec §5.4: "Subscribe on mount, unsubscribe on unmount *and on tab-hide*.
    // A closed tab that kept its subscription would leave the Pi encoding WebP
    // for nobody." A tab on a second monitor is four panels' worth of WebP a
    // rack encodes twice a second for a page no one can see.
    const rig = mount(panelOf(12, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(12, 104, bytesFor(12))))
    await act(() => rig.decoder.settleAll())
    const painted = rig.canvas.only().ops
    expect(rig.live().requests).toEqual([{ action: "subscribe", screen_id: 12 }])

    act(() => tabIs("hidden"))
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 12 },
      { action: "unsubscribe", screen_id: 12 },
    ])
    // On the wire and nowhere else. The panel is still mounted, so the store
    // still holds its bitmap and the glass still shows it -- a tab coming back
    // to a black canvas that fills in half a second later is the failure the
    // store's "forget the screen with its last panel" rule would cause if it
    // were reused here.
    expect(rig.canvas.only().ops).toEqual(painted)
    expect(rig.decoder.made[0].closed).toBe(false)

    act(() => tabIs("visible"))
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 12 },
      { action: "unsubscribe", screen_id: 12 },
      { action: "subscribe", screen_id: 12 },
    ])

    // And the stream really is running again, into the same canvas.
    act(() => rig.live().deliver(frameMessage(12, 105, bytesFor(12))))
    await act(() => rig.decoder.settleAll())
    expect(rig.canvas.only().drawn).toHaveLength(2)
  })

  it("does not subscribe a panel that mounts while the tab is hidden", async () => {
    // The other half of the same rule. A hidden tab is not a still page: a
    // query settling or a socket message renders it, and a panel that mounted
    // there would put a screen back on the wire that the hide had just taken
    // off it -- the Pi encoding for a page nobody can see, which is what §5.4
    // forbids however the subscription got there.
    function Rack({ inspector }: { inspector: boolean }) {
      return (
        <LiveProvider>
          <Panel screenId={41} daemonId={23} size={PANEL_SIZE} />
          {inspector && <Panel screenId={58} daemonId={23} size={PANEL_SIZE} />}
        </LiveProvider>
      )
    }
    const rig = mount(<Rack inspector={false} />, [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => tabIs("hidden"))
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 41 },
      { action: "unsubscribe", screen_id: 41 },
    ])

    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <Rack inspector />
        </QueryClientProvider>
      </StrictMode>,
    )
    expect(rig.live().requests).toHaveLength(2)

    // It goes out with the rest on the way back, from the set the provider
    // keeps -- not from the socket's, which the hide emptied.
    act(() => tabIs("visible"))
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 41 },
      { action: "unsubscribe", screen_id: 41 },
      { action: "subscribe", screen_id: 41 },
      { action: "subscribe", screen_id: 58 },
    ])
  })

  it("follows its screen when the one it is asked for changes", async () => {
    // A panel is a component at a position, not a component per screen: a page
    // that renders one panel and changes which screen it shows -- the inspector
    // following a selection -- reconciles to the same instance. A subscription
    // that did not follow would leave this canvas drawing the screen it started
    // on, and on a rack of panels showing the same kind of picture that is a
    // wrong image nobody would notice.
    const rig = mount(panelOf(41, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    expect(rig.live().requests).toEqual([{ action: "subscribe", screen_id: 41 }])

    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <LiveProvider>
            <Panel screenId={12} daemonId={23} size={PANEL_SIZE} />
          </LiveProvider>
        </QueryClientProvider>
      </StrictMode>,
    )

    // Off the old one before on to the new one, and both on the wire: a screen
    // left subscribed goes on costing its rack the WebP encode for a panel that
    // is not showing it.
    expect(rig.live().requests).toEqual([
      { action: "subscribe", screen_id: 41 },
      { action: "unsubscribe", screen_id: 41 },
      { action: "subscribe", screen_id: 12 },
    ])

    // And it draws the new screen's frames and not the old screen's, which is
    // the thing the subscription was for.
    act(() => rig.live().deliver(frameMessage(12, 104, bytesFor(12))))
    act(() => rig.live().deliver(frameMessage(41, 105, bytesFor(41))))
    await act(() => rig.decoder.settleAll())
    expect(rig.canvas.only().drawn).toHaveLength(1)
    expect(rig.decoder.blobs).toHaveLength(1)
  })

  it("re-renders neither the interface above it nor itself, eight frames running", async () => {
    // The rule §4.4 states and the reason this store exists, pinned where a
    // regression would actually land: the whole path, socket to canvas, with
    // `LiveProvider` mounted. `frames.test.tsx` counts renders on a parent that
    // pushes into the store directly, which cannot fail for state held in the
    // provider -- and the provider is the one component holding both the socket
    // and a position above the entire signed-in interface, so a `useState` for
    // the latest frame written there would re-render the shell eight times a
    // second and pass that test.
    //
    // Three counters, because they fail for different mistakes. `shell` commits
    // for anything in the tree, which is where provider state shows up; `page`
    // is a component between the provider and the panel, which is where a
    // context or a prop would; `panel` is the panel's own render, which is
    // where the `staleRef` bail-out in `setStaleness` shows up -- see the
    // daemons message below for why it only shows up after one.
    let shellCommits = 0
    let pageRenders = 0
    let panelCommits = 0

    function Page() {
      pageRenders += 1
      return (
        <Profiler id="panel" onRender={() => (panelCommits += 1)}>
          <Panel screenId={41} daemonId={23} size={PANEL_SIZE} />
        </Profiler>
      )
    }

    // No StrictMode: it renders everything twice on purpose, and this test's
    // numbers have to mean renders. What StrictMode proves -- that neither
    // effect leaks when it is mounted twice -- every other test here covers.
    const rig = mount(
      <Profiler id="shell" onRender={() => (shellCommits += 1)}>
        <LiveProvider>
          <Page />
        </LiveProvider>
      </Profiler>,
      [rack(7, "Loft", true), rack(23, "Attic", true)],
      { strict: false },
    )
    act(() => rig.live().accept())

    const mounted = { shell: shellCommits, page: pageRenders, panel: panelCommits }
    expect(mounted.page).toBe(1)

    // One genuine re-render of the panel's own fiber, deliberately, before the
    // frames -- and the only reason this test can see the `staleRef` bail-out
    // at all. Rack 7 goes offline and rack 23 stays up; the panel is on 23, so
    // `offline` is false before and after and nothing it displays moves. What
    // does move is React's bookkeeping: the daemons query the panel reads
    // notifies, the panel renders, and that render leaves a lane on the fiber's
    // *alternate*, which is what the eager-state-bailout path is gated on.
    // Without this message the panel's fiber is pristine for the whole test,
    // every `setStale(false)` is discarded eagerly whether the guard is there
    // or not, and the counter below reads the same number either way.
    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [23] })))
    expect(panel(41)).toHaveAttribute("data-state", "live")

    for (let frame = 0; frame < 8; frame += 1) {
      act(() => rig.live().deliver(frameMessage(41, 200 + frame, bytesFor(41))))
      await act(() => rig.decoder.settleAll())
    }

    // Eight frames really did land: without this the three counters below hold
    // just as well for an interface that drew nothing at all.
    expect(rig.canvas.only().drawn).toHaveLength(8)
    expect(pageRenders).toBe(mounted.page)
    // Exactly one, and it is the daemons message: the eight frames add nothing.
    // Delete `if (staleRef.current === next) return` and this is two -- the
    // first frame after that render finds the alternate still carrying its
    // lane, so `setStale(false)` schedules a render and a commit that change
    // nothing at all. Only one, because that render clears the lane again; the
    // cost of dropping the guard is one wasted commit per genuine render of
    // this panel, not one per frame.
    expect(panelCommits).toBe(mounted.panel + 1)
    expect(shellCommits).toBe(mounted.shell + 1)
  })

  it("keeps its picture across a resize, without waiting for the next frame", async () => {
    // `width` and `height` on a canvas are its drawing surface, and assigning
    // either resets it to transparent black. The Screens page changes panel
    // sizes with its layout, so a panel that only ever drew on arrival would go
    // black for up to a frame interval -- half a second at 2 fps -- every time.
    const rig = mount(panelOf(63, 23), [rack(7, "Loft", true), rack(23, "Attic", true)])
    act(() => rig.live().accept())
    act(() => rig.live().deliver(frameMessage(63, 104, bytesFor(63))))
    await act(() => rig.decoder.settleAll())
    expect(rig.canvas.only().drawn).toHaveLength(1)

    const bigger = PANEL_SIZE + 80
    rig.rerender(
      <StrictMode>
        <QueryClientProvider client={rig.queryClient}>
          <LiveProvider>
            <Panel screenId={63} daemonId={23} size={bigger} />
          </LiveProvider>
        </QueryClientProvider>
      </StrictMode>,
    )

    // Drawn again, at the new size, from the picture the store still holds --
    // and no frame arrived and nothing was decoded a second time.
    const drawn = rig.canvas.only().drawn
    expect(drawn).toHaveLength(2)
    expect(drawn[1].args).toEqual([rig.decoder.made[0], 0, 0, bigger, bigger])
    expect(rig.decoder.blobs).toHaveLength(1)
    expect(rig.decoder.made[0].closed).toBe(false)
    // The subscription did not follow the size: a re-subscribe would drop this
    // screen's last subscriber for an instant, and the store forgets a screen
    // with its last panel -- closing the very bitmap the redraw wants.
    expect(rig.live().requests).toEqual([{ action: "subscribe", screen_id: 63 }])
  })

  it("leaves no timer armed when it unmounts", () => {
    // The staleness deadline is a `setTimeout`, and a panel that goes away with
    // one still armed leaves a closure holding its canvas until it fires. On the
    // Screens page panels come and go with the selection, so that is one dangling
    // timer per panel somebody clicked past -- and nothing anywhere would say so,
    // because a `setState` on an unmounted component is silent in React 19.
    //
    // Rendered against its own client with `gcTime: Infinity` so the count means
    // what it says: a query whose last observer unmounts otherwise schedules its
    // own collection, and a timer belonging to the cache is indistinguishable
    // here from one belonging to the panel.
    vi.useFakeTimers()
    // Nothing here asserts a draw, but the recorder still goes in: jsdom's own
    // `getContext` logs "without installing the canvas npm package" to the
    // console every time a panel asks it for one, and a suite whose output is
    // not silent is a suite nobody reads the warnings of.
    recordCanvas()
    const sockets = collectSockets()
    const queryClient = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } })
    queryClient.setQueryData(daemonsKey, [rack(7, "Loft", true), rack(23, "Attic", true)])
    const view = render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>{panelOf(29, 23)}</QueryClientProvider>
      </StrictMode>,
    )
    const socket = sockets.at(-1)
    if (socket === undefined) throw new Error("the interface dialled nothing")
    act(() => socket.accept())

    // Armed while it is on screen. Without this the assertion below would hold
    // just as well for a panel that never watched its own silence at all.
    expect(vi.getTimerCount()).toBe(1)

    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("draws nothing rather than throwing when the browser gives no 2d context", async () => {
    // A canvas whose context is refused -- an exhausted GPU process, a hardened
    // browser. The panel has nothing to draw on, and must still be a panel:
    // what it says about the rack does not come from the canvas.
    vi.useFakeTimers()
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
    const decoder = stubDecoder()
    const sockets = collectSockets()
    render(
      <StrictMode>
        <QueryClientProvider client={new QueryClient()}>{panelOf(77, 23)}</QueryClientProvider>
      </StrictMode>,
    )
    const socket = sockets.at(-1)
    if (socket === undefined) throw new Error("the interface dialled nothing")

    act(() => socket.accept())
    act(() => socket.deliver(frameMessage(77, 104, bytesFor(77))))
    await act(() => decoder.settleAll())

    expect(panel(77)).toHaveAttribute("data-state", "live")
    await act(() => vi.advanceTimersByTimeAsync(STALE_AFTER_MS))
    expect(panel(77)).toHaveAttribute("data-state", "stale")
  })
})

describe("the rack status strip", () => {
  it("shows one dot per rack, from the same query the panels read", () => {
    // The header strip is the only place the whole interface says a Pi has
    // gone, and it is fed by the same `daemons` message and the same cache
    // entry -- not by a second fetch and not by a second rule.
    const rig = mount(
      <ThemeProvider>
        <MemoryRouter>
          <AppShell>
            <p>rack</p>
          </AppShell>
        </MemoryRouter>
      </ThemeProvider>,
      [rack(7, "Loft", true), rack(23, "Attic", false)],
    )

    // Scoped to the strip: the sidebar beside it is a list of links, and an
    // unscoped `getAllByRole("listitem")` would be reading the navigation.
    const dots = () =>
      within(screen.getByTestId("rack-strip"))
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("aria-label"))
    expect(dots()).toEqual(["Loft is online", "Attic is offline"])

    act(() => rig.live().accept())
    act(() => rig.live().deliver(JSON.stringify({ type: "daemons", online: [23] })))

    expect(dots()).toEqual(["Loft is offline", "Attic is online"])
  })
})
