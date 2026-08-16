import { vi } from "vitest"

// jsdom brings no WebSocket worth using here -- it has one, and that is the
// problem: it dials a real TCP connection to whatever `window.location.host`
// resolves to, which is a test touching the network and a reconnect timer
// outliving the test that armed it. This is the socket the client is handed
// instead: it does nothing on its own, and every transition -- accepted, a
// message arrived, the link dropped -- is a line in a test.
//
// Written for `socket.test.ts`, where it is injected through `openSocket` so
// nothing leaks into another file and the client under test is
// character-for-character the one the browser gets. `panel.test.tsx` needs the
// same socket one level up, where `LiveProvider` builds its own client and
// there is no seam to inject through, so it stubs the global with
// `collectSockets` below -- which is the same substitution made at the same
// boundary.
export class FakeSocket {
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

/**
 * Put a `FakeSocket` behind `new WebSocket(...)` and collect what gets dialled.
 *
 * For the tests that drive a component which builds its own client and offers
 * no seam. `vi.unstubAllGlobals()` puts jsdom's own back.
 *
 * Read the *last* entry, not the first: `StrictMode` mounts a provider's effect
 * twice, so the first client is built, closed and replaced before anything the
 * test does. A test that talked to `dialled[0]` would be talking to a
 * connection the interface has already let go of.
 */
export function collectSockets(): FakeSocket[] {
  const dialled: FakeSocket[] = []
  class Collected extends FakeSocket {
    constructor(url: string) {
      super(url)
      dialled.push(this)
    }
  }
  vi.stubGlobal("WebSocket", Collected)
  return dialled
}
