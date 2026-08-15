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

/**
 * Where a validation error's `loc` starts, and what it is worth reading out.
 *
 * FastAPI prefixes every `loc` with the part of the request the value came from
 * -- `["body", "display", "dc"]` -- which is true and says nothing to somebody
 * looking at a form: every field on every page of this interface is in the body.
 * The head is dropped so the path reads as the field, and only when there is a
 * field left to name: a `loc` of `["body"]` alone is a refusal of the whole
 * document, and "body" is then the most this can honestly say about where.
 */
const REQUEST_PARTS = new Set(["body", "query", "path", "header", "cookie"]);

/**
 * One entry of a validation report as a line a person can act on.
 *
 * `msg` is passed through exactly as pydantic wrote it, including its
 * `Value error, ` prefix. Rewriting it would mean this interface deciding what
 * the server meant, and the sentences it produces -- "a gc9a01 display needs dc
 * and rst" -- are already the rule stated by the model that enforces it.
 */
function validationLine(entry: unknown): string | null {
  if (typeof entry !== "object" || entry === null) return null;
  const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
  if (typeof msg !== "string" || msg.length === 0) return null;
  const parts = Array.isArray(loc)
    ? loc
        .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
        .map(String)
    : [];
  if (parts.length > 1 && REQUEST_PARTS.has(parts[0])) parts.shift();
  const where = parts.join(".");
  return where === "" ? msg : `${where}: ${msg}`;
}

// FastAPI's refusals are `{"detail": "..."}`; a 422 is `{"detail": [ ... ]}`,
// which is a validation report rather than a sentence. Both are read here,
// because every validation refusal the server gives is the list shape and a
// caller that only understood the string one had nothing to show but a generic
// apology -- on forms whose whole subject is which value is wrong. The report is
// rendered as `field: message` lines, joined; anything that is neither shape --
// a proxy's HTML, a `detail` of `null`, a list of things with no `msg` -- is
// `null`, and the caller's own sentence stands.
//
// The generated types only describe 200 and 422 -- the other statuses are not in
// the OpenAPI document -- so this reads the body defensively rather than
// trusting a type the server never promised.
export function detailFrom(error: unknown): string | null {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
    if (Array.isArray(detail)) {
      const lines = detail
        .map(validationLine)
        .filter((line): line is string => line !== null);
      if (lines.length > 0) return lines.join("; ");
    }
  }
  return null;
}

let unauthorized: ((url: string) => void) | null = null;

/**
 * Register the one place a 401 is handled, and forget any earlier one.
 *
 * Every session-guarded route answers 401 once the cookie is gone, and every
 * one of them would otherwise have to notice. The middleware below is the whole
 * of that: it says *that* a request was refused, and *which* request it was.
 * What to do about it -- clearing the cached session and going to /login exactly
 * once -- is the app's.
 *
 * The URL is load-bearing, not diagnostic. `POST /api/auth/login` answers 401
 * for a wrong password, which is not an expired session and must not be treated
 * as one; the only thing that tells the two apart is the endpoint that refused.
 */
export function setUnauthorizedHandler(handler: ((url: string) => void) | null) {
  unauthorized = handler;
}

api.use({
  onResponse({ request, response }) {
    if (response.status === 401) unauthorized?.(request.url);
    return undefined;
  },
});
