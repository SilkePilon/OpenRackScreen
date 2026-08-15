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
// What the relative base costs in tests: openapi-fetch constructs a `Request`
// from `baseUrl + path` *before* it calls the fetch implementation, and Node's
// global `Request` (undici) rejects a relative URL -- so under jsdom the call
// threw `ERR_INVALID_URL: /api/daemons` before any mock or interceptor could
// run. This is a property of the environment, not of the browser, where a
// relative base is exactly right.
//
// `tests/setup.ts` settles it for every test at once, with a feature-detected
// `Request` that resolves a relative URL against `window.location.origin` --
// which is what a browser does. So tests drive *this* client, unmocked, through
// MSW. Do not "fix" this by changing `baseUrl` here; the browser is the case
// that has to be right.
export const api = createClient<paths>({
  baseUrl: "/",
  credentials: "same-origin",
});

/** What a route answered when it refused, as an `Error` a query can throw. */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// FastAPI's refusals are `{"detail": "..."}`; a 422 is `{"detail": [ ... ]}`,
// which is a validation report and not a sentence, so only a string is taken.
// The generated types only describe 200 and 422 -- the other statuses are not in
// the OpenAPI document -- so this reads the body defensively rather than
// trusting a type the server never promised.
export function detailFrom(error: unknown): string | null {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return null;
}

let unauthorized: (() => void) | null = null;

/**
 * Register the one place a 401 is handled, and forget any earlier one.
 *
 * Every session-guarded route answers 401 once the cookie is gone, and every
 * one of them would otherwise have to notice. The middleware below is the whole
 * of that: it says *that* the session ended. What to do about it -- clearing the
 * cached session and going to /login exactly once -- is the app's, because only
 * the app knows where it currently is.
 */
export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorized = handler;
}

api.use({
  onResponse({ response }) {
    if (response.status === 401) unauthorized?.();
    return undefined;
  },
});
