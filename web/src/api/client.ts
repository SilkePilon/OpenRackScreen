import createClient from "openapi-fetch";
import type { paths } from "./schema";

// `baseUrl: "/"` rather than an origin: the interface is served by the server it
// talks to, and in development Vite proxies /api and /ws to 127.0.0.1:8080. An
// absolute origin here would have to be right in both places and would put the
// session cookie on a cross-site request in one of them.
//
// `credentials: "same-origin"` is what carries that session cookie. The server
// sets it HttpOnly, so nothing in this codebase reads it -- the browser attaches
// it because this option says to, and no other code path can.
//
// What the relative base costs in tests: `api` cannot be driven through a mocked
// `fetch` under jsdom. openapi-fetch constructs a `Request` from `baseUrl + path`
// *before* it calls the fetch implementation, and Node's global `Request` (undici)
// rejects a relative URL -- so the mock never runs and the call throws
// `ERR_INVALID_URL: /api/daemons`. This is a property of the environment, not of
// the browser, where a relative base is exactly right. Two ways out, in order of
// preference:
//   1. Mock the `api` object itself (`vi.mock("@/api/client")`, stub `api.GET`),
//      which is also the seam the components actually depend on.
//   2. If the real openapi-fetch layer is what is under test, build a second
//      client in the test with an absolute same-origin base --
//      `createClient<paths>({ baseUrl: "http://localhost/", ... })` -- so undici
//      can parse it, and mock `fetch` under that.
// Do not "fix" this by changing `baseUrl` here; the browser is the case that has
// to be right.
export const api = createClient<paths>({
  baseUrl: "/",
  credentials: "same-origin",
});
