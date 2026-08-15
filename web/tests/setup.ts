import "@testing-library/jest-dom/vitest"
import { afterAll, afterEach } from "vitest"
import { server } from "./msw"

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
