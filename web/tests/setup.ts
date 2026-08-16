import "@testing-library/jest-dom/vitest"
import { notifyManager } from "@tanstack/react-query"
import { afterAll, afterEach } from "vitest"
import { server } from "./msw"

// React Query tells its observers on the next turn of the event loop, not on
// the one that changed the cache: `notifyManager`'s default scheduler is
// `systemSetTimeoutZero`, a bare `setTimeout(cb, 0)`. So a `setQueryData` --
// the live socket's `daemons` message, an invalidation, a mutation settling --
// reaches the DOM one macrotask after it happened, and under `vi.useFakeTimers`
// it does not reach it at all until somebody advances a clock.
//
// Neither is a fact about this interface. The delay is zero milliseconds and no
// person or component can observe it; what it is, in a test, is a macrotask
// every assertion about rendered output has to know to wait for, and a fake
// clock that has to be nudged for a reason that has nothing to do with the
// staleness threshold it was installed to pin. `setScheduler` is the hook the
// library provides for exactly this. Notifications still go through the whole
// real observer machinery -- nothing is stubbed and no result is invented --
// they simply land on the tick that caused them.
//
// What it costs, said here so the next author knows before relying on it. This
// is global and never restored -- module scope of the setup file, so every
// suite in this project, now and later, runs with synchronous cache
// propagation. The one property that is no longer observable anywhere in these
// tests is the *ordering* between a `setQueryData` and the async work after it:
// in a browser the notify lands one macrotask later, so a component may render
// once more with the old data before it sees the new, and no test here can
// assert that it does. Nothing in this milestone reads that ordering -- React
// batches within a task either way -- which is why the trade is taken.
//
// The narrower alternative, for a suite that ever does need the real schedule:
// leave the scheduler alone and write `await act(() =>
// vi.advanceTimersByTimeAsync(0))` after the message that changed the cache.
// That would have fixed the two tests this was installed for without changing
// the environment for anybody else.
notifyManager.setScheduler((notify) => notify())

// jsdom brings no `Request` of its own, so the global is Node's (undici), which
// -- unlike every browser -- refuses a relative URL: `new Request("/api/auth/me")`
// throws ERR_INVALID_URL. `api` is built with `baseUrl: "/"` because that is
// what is right in a browser, and openapi-fetch builds the `Request` *before*
// it calls fetch, so the throw lands before MSW can intercept anything.
//
// Resolving a relative URL against the document's origin is exactly what a
// browser does, so this stands in for the environment and not for any code
// under test: the client the tests drive is character-for-character the client
// the browser gets. Feature-detected rather than version-guarded, so a jsdom
// that grows a spec-compliant `Request` takes over and this disappears.
function acceptsRelativeUrls() {
  try {
    new Request("/probe")
    return true
  } catch {
    return false
  }
}

if (!acceptsRelativeUrls()) {
  const AbsoluteOnlyRequest = globalThis.Request
  globalThis.Request = class extends AbsoluteOnlyRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" ? new URL(input, window.location.origin) : input,
        init,
      )
    }
  }
}

// Listening here, at module scope, and *not* in `beforeAll`. A setup file runs
// before the test file is imported; a `beforeAll` hook runs after it. And
// `createClient` captures `globalThis.fetch` when `src/api/client.ts` is
// evaluated, which is during that import -- so a server started in `beforeAll`
// patches a `fetch` the client is no longer holding, every request escapes to
// the real network, and the failure reads as `ECONNREFUSED 127.0.0.1:3000`
// rather than as anything to do with mocking. Same argument for the `Request`
// polyfill above, which `createClient` also captures at that moment.
server.listen({ onUnhandledRequest: "error" })
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

// jsdom's `WebSocket` is not a stand-in for anything: it opens a real TCP
// connection to whatever the URL resolves to. Every test that renders the
// authenticated shell now builds one, because `AppShell` owns `/ws/ui`, and a
// refused connection is worse than a slow one -- `createLiveSocket` answers a
// close by arming a reconnect, so the timer outlives the test that provoked it
// and fires into a torn-down environment.
//
// The default is therefore a socket that does nothing at all: it never opens,
// never closes and never fires, so a test that is not about the connection
// neither reaches the network nor leaves a timer behind. The tests that *are*
// about it substitute their own -- `socket.test.ts` injects one through
// `openSocket`, `panel.test.tsx` stubs the global with `collectSockets` -- and
// `vi.unstubAllGlobals()` then restores this one rather than jsdom's.
//
// Installed *after* `server.listen()` on purpose, and this is the only ordering
// that works. MSW replaces `globalThis.WebSocket` with an interceptor of its
// own when it starts listening, and an unhandled connection is one it logs an
// error for and then lets through to the real network -- which is both the
// noise and the socket this is here to prevent. Assigning afterwards takes the
// global back. There is no `ws` handler in `./msw` for the same reason there is
// no `http` handler for a request no test makes: a live connection is
// substituted by the tests that drive one, at the seam they drive it through.
class InertWebSocket {
  readonly url: string
  onopen: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  constructor(url: string) {
    this.url = url
  }

  send(): void {}
  close(): void {}
}
// `defineProperty` rather than an assignment: MSW installs its interceptor as a
// non-writable property, so `globalThis.WebSocket = ...` throws in the module
// scope of a setup file, which fails every suite at once.
Object.defineProperty(globalThis, "WebSocket", {
  value: InertWebSocket,
  writable: true,
  configurable: true,
})

// jsdom implements neither of these, and both are reached on the first render:
// the sidebar asks matchMedia whether it is on a phone, and Radix's popper
// measures its trigger with a ResizeObserver.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// Radix menus call these while opening; jsdom leaves them undefined.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}
