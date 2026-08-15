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
export const api = createClient<paths>({
  baseUrl: "/",
  credentials: "same-origin",
});
