import { afterEach, describe, expect, it, vi } from "vitest"

import { createLiveSocket, type LiveFrame, type LiveSocket } from "../src/live/socket"

// jsdom brings no WebSocket, and a real one would need a server and a wait. This
// is the socket the client is handed instead: it does nothing on its own, and
// every transition -- accepted, a message arrived, the link dropped -- is a line
// in a test. Injected through `openSocket` rather than assigned over a global,
// so nothing in this file can leak into another test file, and so the client
// under test is character-for-character the one the browser gets.
class FakeSocket {
  state: "connecting" | "open" | "closed" = "connecting"
  readonly sent: string[] = []
  onopen: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  readonly url: string

  // Spelled out rather than as a parameter property: `erasableSyntaxOnly` is on
  // in `tsconfig.app.json`, and a parameter property is the one class syntax
  // that cannot be erased.
  constructor(url: string) {
    this.url = url
  }

  send(data: string): void {
    // A browser throws `InvalidStateError` on a send in CONNECTING, and drops
    // one on a closed socket on the floor. Both are refused loudly here because
    // both are the same bug -- a subscription the tab believes it made and the
    // server never saw -- and because nothing in `socket.ts` catches around
    // `send`, so this surfaces as a failing test rather than as a skipped one.
    if (this.state !== "open") throw new Error(`sent on a ${this.state} socket: ${data}`)
    this.sent.push(data)
  }

  close(): void {
    // A browser fires `close` for a socket the page closed, too. Doing it
    // synchronously here is what makes the deliberate-close path testable: if
    // `close()` left the reconnect armed, this is where it would show.
    if (this.state === "closed") return
    this.state = "closed"
    this.onclose?.(new CloseEvent("close", { code: 1000, wasClean: true }))
  }

  /** The server accepted the handshake. */
  accept(): void {
    this.state = "open"
    this.onopen?.(new Event("open"))
  }

  /** One text frame from the server. */
  deliver(text: string): void {
    this.onmessage?.(new MessageEvent("message", { data: text }))
  }

  /** The link went away: a wifi blip, a restarted server, a pulled cable. */
  drop(): void {
    this.state = "closed"
    this.onclose?.(new CloseEvent("close", { code: 1006, wasClean: false }))
  }

  /** What this connection said upstream, parsed. */
  get requests(): unknown[] {
    return this.sent.map((text) => JSON.parse(text) as unknown)
  }
}

type Harness = {
  live: LiveSocket
  sockets: FakeSocket[]
  /** `performance.now()` at each dial, so the backoff can be read off as deltas. */
  dialledAt: number[]
  daemons: number[][]
  frames: LiveFrame[]
}

const running: LiveSocket[] = []

function harness(options: { random?: () => number } = {}): Harness {
  const sockets: FakeSocket[] = []
  const dialledAt: number[] = []
  const daemons: number[][] = []
  const frames: LiveFrame[] = []
  const live = createLiveSocket({
    url: "ws://rack.invalid/ws/ui",
    onDaemons: (online) => daemons.push(online),
    onFrame: (frame) => frames.push(frame),
    now: () => performance.now(),
    random: options.random ?? (() => 0),
    openSocket: (url) => {
      const socket = new FakeSocket(url)
      sockets.push(socket)
      dialledAt.push(performance.now())
      return socket
    },
  })
  running.push(live)
  return { live, sockets, dialledAt, daemons, frames }
}

/** The socket this harness is talking on now. */
function current(test: Harness): FakeSocket {
  const socket = test.sockets.at(-1)
  if (socket === undefined) throw new Error("nothing has been dialled")
  return socket
}

afterEach(() => {
  // Every client this file made is closed, so no reconnect timer outlives the
  // test that armed it -- a pending one would fire into the next test's fake
  // clock and dial a socket nobody is watching.
  while (running.length > 0) running.pop()?.close()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe("the browser socket", () => {
  it("re-subscribes the panels still on screen after a reconnect", async () => {
    // A wifi blip must not leave a frozen canvas until the tab is reloaded.
    vi.useFakeTimers()
    const test = harness()
    test.live.connect()
    const first = current(test)
    expect(first.url).toBe("ws://rack.invalid/ws/ui")
    first.accept()

    // Screens 31, 58 and 12: no id is its own index, none is another's
    // position, and the two that survive are not in ascending order -- so a
    // client that re-subscribed by index, or sorted, or sent the whole history,
    // says something different from what is asserted below.
    test.live.subscribe(31)
    test.live.subscribe(58)
    test.live.subscribe(12)
    test.live.unsubscribe(58)
    expect(first.requests).toEqual([
      { action: "subscribe", screen_id: 31 },
      { action: "subscribe", screen_id: 58 },
      { action: "subscribe", screen_id: 12 },
      { action: "unsubscribe", screen_id: 58 },
    ])

    first.drop()
    expect(test.live.state).toBe("reconnecting")
    await vi.advanceTimersByTimeAsync(1000)

    const second = current(test)
    expect(second).not.toBe(first)
    second.accept()
    expect(test.live.state).toBe("open")
    // 58 was given up before the drop and must not come back; 31 and 12 must,
    // in the order they were asked for, and each exactly once.
    expect(second.requests).toEqual([
      { action: "subscribe", screen_id: 31 },
      { action: "subscribe", screen_id: 12 },
    ])
  })

  it("backs off, and caps", async () => {
    // vi.useFakeTimers(); assert the delays are 1,2,4,8,16,30,30 -- not a hammer.
    vi.useFakeTimers()
    // `random: () => 0` is the top of the jitter band, which is the schedule
    // itself: the jitter only ever pulls a reconnect earlier, never later, so
    // this pins the ceiling every real delay is drawn from. `spreads its
    // reconnects` below is what pins that the draw actually happens.
    const test = harness({ random: () => 0 })
    test.live.connect()

    // Seven reconnects, each measured from the drop that caused it. Nothing is
    // ever accepted -- this is a server that is not there, which is the case
    // the cap exists for. 60s of waiting is longer than any delay may be, so
    // each round produces exactly one dial.
    const delays: number[] = []
    for (let round = 0; round < 7; round += 1) {
      const droppedAt = performance.now()
      const dials = test.sockets.length
      current(test).drop()
      await vi.advanceTimersByTimeAsync(60_000)
      expect(test.sockets).toHaveLength(dials + 1)
      delays.push((test.dialledAt.at(-1) ?? Number.NaN) - droppedAt)
    }

    expect(delays).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000])
  })

  it("skips a message it does not understand and stays open", () => {
    // The server's own rule: an unknown message is skipped and logged,
    // the socket stays open.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const test = harness()
    test.live.connect()
    const socket = current(test)
    socket.accept()

    // A type from a newer server, carrying a field this build does know how to
    // read: a client that dispatched on the payload instead of on `type` would
    // report rack 4 online.
    socket.deliver(JSON.stringify({ type: "hello", online: [4] }))
    socket.deliver("{not json at all")
    socket.deliver(JSON.stringify({ type: "daemons" }))
    socket.deliver(JSON.stringify([9, 23]))
    // A frame with no image in it. Delivered as one, it would reach the store
    // as `webp: undefined` and be drawn as nothing on a panel that was working.
    socket.deliver(JSON.stringify({ type: "frame", screen_id: 41, seq: 7 }))
    // And one whose id is a string. `screen_id` is a SQLite rowid bounded at
    // 2**63 - 1, and writing an int64 as a JSON string to keep its precision is
    // a thing serialisers do -- so this is the malformed frame that looks
    // right. It must not become a frame for screen `"41"`, which is a key no
    // panel subscribes under and a stall with nothing in the console.
    socket.deliver(
      JSON.stringify({ type: "frame", screen_id: "41", seq: 7, webp: "/+/+/+/+" }),
    )
    // And then the real thing, on the same socket, to prove it is still the
    // same socket and still working.
    socket.deliver(JSON.stringify({ type: "daemons", online: [9, 23] }))

    expect(test.daemons).toEqual([[9, 23]])
    expect(test.frames).toEqual([])
    expect(warn).toHaveBeenCalledTimes(6)
    expect(socket.state).toBe("open")
    expect(test.live.state).toBe("open")
    // Not closed *and* not replaced: a client that answered a bad message by
    // reconnecting would have dialled a second time.
    expect(test.sockets).toHaveLength(1)
  })

  it("decodes a frame with the standard alphabet, and would fail on the URL-safe one", () => {
    // The trap: atob throws on `-` and `_`. Use a payload whose two
    // encodings differ in EVERY character, so an ASCII fixture cannot
    // hide a wrong alphabet.
    const bytes = new Uint8Array([0xff, 0xef, 0xfe, 0xff, 0xef, 0xfe])
    // standard: "/+/+/+/+"   url-safe: "_-_-_-_-"
    const standard = "/+/+/+/+"
    const urlSafe = "_-_-_-_-"
    // The fixture's own claim, checked rather than commented: these really are
    // the two encodings of those bytes, and they really do differ everywhere.
    expect(btoa(String.fromCharCode(...bytes))).toBe(standard)
    expect(standard.split("").every((char, index) => char !== urlSafe[index])).toBe(true)

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const test = harness()
    test.live.connect()
    const socket = current(test)
    socket.accept()

    // Screen 41, sequence 7: the two are not each other and neither is the
    // index of the frame in `frames`, so a client that put one where the other
    // belongs cannot pass this.
    socket.deliver(JSON.stringify({ type: "frame", screen_id: 41, seq: 7, webp: standard }))
    expect(test.frames).toEqual([{ screenId: 41, seq: 7, bytes }])

    // The same six bytes, spelled the way pydantic would spell them. This must
    // *not* arrive: a client carrying `.replace(/-/g, "+").replace(/_/g, "/")`
    // would decode it to the identical bytes and deliver a second frame, and
    // that substitution is exactly what `ws_ui._frame_message` says is wrong
    // until its docstring changes.
    socket.deliver(JSON.stringify({ type: "frame", screen_id: 41, seq: 8, webp: urlSafe }))
    expect(test.frames).toHaveLength(1)
    expect(warn).toHaveBeenCalledTimes(1)
    // Unreadable, not fatal: a payload it cannot decode is skipped like any
    // other message it cannot read.
    expect(socket.state).toBe("open")
    expect(test.live.state).toBe("open")
  })

  it("says the two fields the server accepts, and nothing else", async () => {
    // `_Request` is `extra="forbid"` on purpose: a message carrying a field the
    // server does not know is refused whole, and the tab believes it
    // subscribed. So the wire form is asserted key by key, not just by value.
    vi.useFakeTimers()
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const test = harness()
    test.live.connect()
    const socket = current(test)
    socket.accept()

    test.live.subscribe(23)
    expect(Object.keys(socket.requests[0] as object)).toEqual(["action", "screen_id"])
    expect(typeof (socket.requests[0] as { screen_id: unknown }).screen_id).toBe("number")

    // A repeat changes nothing here, and the server treats it as a no-op that
    // still costs a rack a whole-set recomputation -- so it is not sent.
    test.live.subscribe(23)
    // Nor is a release of something this tab never held.
    test.live.unsubscribe(64)
    expect(socket.sent).toHaveLength(1)

    // Ids that are not row ids never reach the wire. `JSON.stringify` writes
    // NaN and Infinity as `null`, which the server refuses with a warning
    // nobody in the browser reads, and `true` is the one the server calls out
    // by name: pydantic's lax mode would take it as 1 and show panel A the
    // image of whatever screen has row id 1.
    test.live.subscribe(Number("not a screen"))
    test.live.subscribe(0)
    test.live.subscribe(-4)
    test.live.subscribe(3.5)
    test.live.subscribe(Number.POSITIVE_INFINITY)
    test.live.subscribe(true as unknown as number)
    expect(socket.sent).toHaveLength(1)
    expect(warn).toHaveBeenCalledTimes(6)

    // And none of them was remembered either, so the reconnect that follows
    // sends what a live connection was willing to send and nothing more.
    socket.drop()
    expect(test.live.state).toBe("reconnecting")
    await vi.advanceTimersByTimeAsync(1000)
    const second = current(test)
    second.accept()
    expect(second.requests).toEqual([{ action: "subscribe", screen_id: 23 }])
  })

  it("holds what it was asked for before the link is up, and says it on open", () => {
    // A panel mounts before the handshake finishes -- which is the ordinary
    // case, since the provider that owns this socket and the page that mounts
    // panels come up in the same tick. A browser answers `send` on a CONNECTING
    // socket with `InvalidStateError`, and nothing here catches it, so a client
    // that sent eagerly would throw out of the panel's own effect.
    const test = harness()
    test.live.connect()
    const socket = current(test)

    test.live.subscribe(31)
    test.live.subscribe(12)
    expect(socket.sent).toEqual([])
    expect(test.live.state).toBe("connecting")

    socket.accept()
    expect(socket.requests).toEqual([
      { action: "subscribe", screen_id: 31 },
      { action: "subscribe", screen_id: 12 },
    ])
  })

  it("dials once however often it is asked to connect", async () => {
    // React 19 mounts an effect twice in StrictMode and the provider that will
    // own this socket is one effect. A second dial would leave the first
    // connection open with nobody holding it -- and with its own reconnect
    // behind it, so closing the client the page *does* hold would not stop it.
    vi.useFakeTimers()
    const test = harness()
    test.live.connect()
    test.live.connect()
    expect(test.sockets).toHaveLength(1)

    // And again while it is down, where what is already armed is a timer rather
    // than a socket.
    current(test).accept()
    current(test).drop()
    test.live.connect()
    expect(test.sockets).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(test.sockets).toHaveLength(2)
  })

  it("spreads its reconnects, so many tabs do not come back in lockstep", async () => {
    vi.useFakeTimers()
    // The same first delay, drawn twice. Asserted as a band rather than as a
    // number, deliberately: the number is the formula, and a test that
    // restated it would pass a jitter that never varies. What matters is that
    // the draw is consulted, that two draws differ, and that neither escapes
    // the schedule the test above pins.
    const spread = async (random: () => number): Promise<number> => {
      const test = harness({ random })
      test.live.connect()
      current(test).drop()
      await vi.advanceTimersByTimeAsync(60_000)
      return test.dialledAt[1] - test.dialledAt[0]
    }

    const middle = await spread(() => 0.5)
    const far = await spread(() => 0.9)

    expect(middle).not.toBe(far)
    for (const delay of [middle, far]) {
      expect(delay).toBeLessThan(1000)
      expect(delay).toBeGreaterThanOrEqual(750)
    }
  })

  it("keeps backing off for a connection that did not stay up", async () => {
    // A server that accepts the handshake and drops it again is the case a
    // reset-on-open would dial once a second forever. The backoff is reset by a
    // connection that lasted, not by one that merely happened.
    vi.useFakeTimers()
    const test = harness()
    test.live.connect()

    const flap = async (openFor: number, expected: number) => {
      const socket = current(test)
      socket.accept()
      await vi.advanceTimersByTimeAsync(openFor)
      socket.drop()
      const dials = test.sockets.length
      await vi.advanceTimersByTimeAsync(expected - 1)
      expect(test.sockets).toHaveLength(dials)
      await vi.advanceTimersByTimeAsync(1)
      expect(test.sockets).toHaveLength(dials + 1)
    }

    // Five seconds is a flap, not a connection: each drop climbs.
    await flap(5000, 1000)
    await flap(5000, 2000)
    await flap(5000, 4000)
    // Half a minute is a server that is really there. The next drop starts over.
    await flap(30_000, 1000)

    // And the dial that followed it was never accepted at all, so it inherits
    // none of that connection's good standing: a server that stops answering
    // the handshake after a good hour must not be dialled once a second for
    // the rest of the day.
    const dials = test.sockets.length
    current(test).drop()
    await vi.advanceTimersByTimeAsync(1999)
    expect(test.sockets).toHaveLength(dials)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.sockets).toHaveLength(dials + 1)
  })

  it("starts over when a closed client is connected again", async () => {
    // StrictMode's mount, unmount, mount does exactly this to one client, and
    // so does a route that leaves the live view and comes back. What must not
    // survive it is the backoff: the first reconnect after a fresh connect is
    // the first one, not the ninth.
    vi.useFakeTimers()
    const test = harness()
    test.live.connect()
    current(test).drop()
    await vi.advanceTimersByTimeAsync(1000)
    current(test).drop()
    await vi.advanceTimersByTimeAsync(2000)

    test.live.close()
    test.live.connect()
    const droppedAt = performance.now()
    current(test).drop()
    await vi.advanceTimersByTimeAsync(60_000)
    expect((test.dialledAt.at(-1) ?? Number.NaN) - droppedAt).toBe(1000)
  })

  it("stops for good when the tab is done with it, up or down", async () => {
    vi.useFakeTimers()

    // Closed while it is up. The close the page asked for is not a drop to
    // recover from, and a client that could not tell them apart would dial
    // forever after the provider unmounted.
    const up = harness()
    up.live.connect()
    const socket = current(up)
    socket.accept()
    up.live.close()
    expect(socket.state).toBe("closed")
    expect(up.live.state).toBe("closed")
    await vi.advanceTimersByTimeAsync(60_000)
    expect(up.sockets).toHaveLength(1)

    // A message the browser had already taken off the wire when the page let
    // this connection go. It arrives on a socket nobody holds any more, and
    // acting on it would be this client reporting as live what it read from a
    // connection it has abandoned.
    socket.deliver(JSON.stringify({ type: "daemons", online: [9, 23] }))
    expect(up.daemons).toEqual([])

    // Closed while it is down, which is the harder half: there is no socket to
    // close, only a dial already armed, and disarming it is the whole of what
    // stopping means here. A route change during a wifi blip is this exact
    // case.
    const down = harness()
    down.live.connect()
    current(down).accept()
    current(down).drop()
    expect(down.live.state).toBe("reconnecting")
    down.live.close()
    expect(down.live.state).toBe("closed")
    await vi.advanceTimersByTimeAsync(60_000)
    expect(down.sockets).toHaveLength(1)
  })
})
