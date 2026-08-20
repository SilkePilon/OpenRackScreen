import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { MemoryRouter } from "react-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "../src/api/client"
import { sessionKey } from "../src/api/queries"
import { RequireSession } from "../src/routes/RequireSession"
import { collectSockets, type FakeSocket } from "./fake-socket"
import { server } from "./msw"
import { renderApp } from "./render"

// The two tests at the foot of this file stub `WebSocket`; every other test in
// it renders the authenticated shell, which builds one, and would inherit the
// stub. `setup.ts`'s inert socket is what comes back.
afterEach(() => {
  vi.unstubAllGlobals()
})

// `GET /api/auth/me` is open and always answers 200 with two booleans -- it is
// the one route that tells the interface which of the two pre-session states
// the server is in, so it can neither 401 nor 409. The two booleans are always
// stubbed to different values where the behaviour depends on only one of them,
// so reading the wrong field cannot pass.
function me(state: { authenticated: boolean; password_set: boolean }) {
  return http.get("/api/auth/me", () => HttpResponse.json(state))
}

/**
 * The claim list, which the Daemons page asks for whenever it renders.
 *
 * Stubbed empty, and only ever empty. Nothing in this file is about a rack
 * waiting to join -- what is being pinned here is where a *refused* request
 * sends the browser, and the request that is refused is the daemon listing
 * beside it. This one is stubbed so that it is answered at all: `setup.ts`
 * starts MSW with `onUnhandledRequest: "error"`, and an unstubbed route would
 * fail these tests for a reason that has nothing to do with 401s.
 */
const NO_CLAIMS = http.get("/api/claims", () => HttpResponse.json([]))

describe("getting in", () => {
  it("sends a server with no password to setup", async () => {
    server.use(me({ authenticated: false, password_set: false }))
    renderApp({ at: "/daemons" })

    expect(
      await screen.findByRole("heading", { name: /set a password/i }),
    ).toBeInTheDocument()
  })

  it("sends an unauthenticated session to login, not to setup", async () => {
    server.use(me({ authenticated: false, password_set: true }))
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    expect(
      screen.queryByRole("heading", { name: /set a password/i }),
    ).not.toBeInTheDocument()
  })

  it("returns to login when a session expires mid-session, and only once", async () => {
    let meCalls = 0
    let listings = 0
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1
        // Signed in on the first ask, expired on every one after.
        return HttpResponse.json({ authenticated: meCalls === 1, password_set: true })
      }),
      // The route the expiry is discovered on. `/api/auth/me` never 401s; the
      // session-guarded routes do, and that 401 is what has to be noticed.
      //
      // Answered on the page's own first ask and refused afterwards, in step
      // with `me` above: *mid*-session is what this test is about, so the page
      // has to be up before the session goes. A fixture that refused from the
      // first request instead makes the redirect race the page's first render
      // -- the heading below then may never be observable, which is a fact
      // about how many requests a page happens to make at mount rather than
      // anything about 401s, and it changed the day the Daemons page grew a
      // second query.
      http.get("/api/daemons", () => {
        listings += 1
        return listings === 1
          ? HttpResponse.json([])
          : HttpResponse.json({ detail: "not authenticated" }, { status: 401 })
      }),
      NO_CLAIMS,
    )
    const { pushes, path } = renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()

    // Two protected requests in flight when the session goes, which is the
    // ordinary case on a page that loads more than one thing.
    await act(async () => {
      await Promise.all([api.GET("/api/daemons"), api.GET("/api/daemons")])
    })

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    // Once. Two 401s are one return to /login, not one per refused request.
    // NOT a loop assertion, despite reading like one: the guard redirects with
    // <Navigate replace>, which adds no history entry, so a guard/login bounce
    // would never move this number. It catches one-per-refusal, nothing else.
    expect(pushes()).toBe(1)
    expect(path()).toBe("/login")
  })

  it("treats a 401 from a guarded route as an expiry even while on the login page", async () => {
    server.use(
      me({ authenticated: false, password_set: true }),
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    )
    const { queryClient, pushes } = renderApp({ at: "/login" })
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()

    // A session that was still believed in a moment ago -- cached by the guard
    // on the way here, or by another tab -- and a guarded request that has just
    // discovered it is gone. The user's location says nothing about that.
    queryClient.setQueryData(sessionKey, { authenticated: true, password_set: true })
    await act(async () => {
      await api.GET("/api/daemons")
    })

    // Keyed on the endpoint that refused, this is an expiry and is handled: the
    // cached "there is a session" is dropped and the app returns to /login.
    // Keyed on the pathname the user is on, it is swallowed and the stale
    // answer stays in the cache for the next guard to believe.
    expect(queryClient.getQueryData(sessionKey)).toBeUndefined()
    expect(pushes()).toBe(1)
  })

  it("refuses a second browser racing the first to claim the password", async () => {
    server.use(
      me({ authenticated: false, password_set: false }),
      // What this server really answers when the password is already claimed.
      http.post("/api/auth/setup", () =>
        HttpResponse.json({ detail: "a password is already set" }, { status: 409 }),
      ),
    )
    renderApp({ at: "/setup" })

    await userEvent.type(await screen.findByLabelText(/password/i), "correct-horse-8")
    await userEvent.click(screen.getByRole("button", { name: /set password/i }))

    expect(await screen.findByText(/a password is already set/i)).toBeInTheDocument()
  })

  it("claims the password and goes on to sign in", async () => {
    let claimed: string | null = null
    server.use(
      me({ authenticated: false, password_set: false }),
      http.post("/api/auth/setup", async ({ request }) => {
        claimed = ((await request.json()) as { password: string }).password
        return HttpResponse.json({ ok: true })
      }),
    )
    renderApp({ at: "/setup" })

    await userEvent.type(await screen.findByLabelText(/password/i), "correct-horse-8")
    await userEvent.click(screen.getByRole("button", { name: /set password/i }))

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    expect(claimed).toBe("correct-horse-8")
  })

  it("says the password was wrong, and stays where it is", async () => {
    server.use(
      me({ authenticated: false, password_set: true }),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ detail: "wrong password" }, { status: 401 }),
      ),
    )
    const { pushes, path } = renderApp({ at: "/login" })

    await userEvent.type(await screen.findByLabelText(/password/i), "not-the-password")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByText(/wrong password/i)).toBeInTheDocument()
    // The 401 the login route answers is not an expired session, and must not
    // be redirected anywhere -- least of all onto another copy of this page.
    expect(pushes()).toBe(0)
    expect(path()).toBe("/login")
  })

  it("tells a rate-limited sign-in from a wrong password", async () => {
    server.use(
      me({ authenticated: false, password_set: true }),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ detail: "too many attempts" }, { status: 429 }),
      ),
    )
    renderApp({ at: "/login" })

    await userEvent.type(await screen.findByLabelText(/password/i), "the-real-password")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    // The server refused to even look at the password. Reporting that as a
    // wrong password would send the user off to change a password that works.
    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument()
    expect(screen.getByText(/minute/i)).toBeInTheDocument()
    expect(screen.queryByText(/wrong password/i)).not.toBeInTheDocument()
  })

  it("signs in and goes to the rack, not back to the page it was turned away from", async () => {
    let authenticated = false
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({ authenticated, password_set: true }),
      ),
      http.post("/api/auth/login", async ({ request }) => {
        const body = (await request.json()) as { password: string }
        authenticated = body.password === "the-real-password"
        return authenticated
          ? HttpResponse.json({ ok: true })
          : HttpResponse.json({ detail: "wrong password" }, { status: 401 })
      }),
      // Signing in lands on /daemons, which is a real page now and asks for the
      // racks. Empty, so no rack card is drawn and nothing asks for events
      // either: this test is about where the redirect goes, and an unstubbed
      // request would fail it for a reason that has nothing to do with that.
      http.get("/api/daemons", () => HttpResponse.json([])),
      NO_CLAIMS,
    )
    // Turned away from /screens, not /daemons. The destination after signing in
    // is unconditional (`LoginPage` navigates to /daemons), so starting at
    // /daemons would make the origin and the destination the same string and
    // the assertion could not tell "goes where it was going" from "always goes
    // to the rack". Returning to the origin is not implemented and is not this
    // task's requirement; this asserts what the code does.
    //
    // Arriving through the guard is still the point: it is what leaves a "not
    // signed in" answer in the cache for the guard to read again a moment
    // later, so signing in has to be more than a redirect.
    const { path } = renderApp({ at: "/screens" })
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()

    await userEvent.type(await screen.findByLabelText(/password/i), "the-real-password")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()
    expect(path()).toBe("/daemons")
  })

  it("gives up on the session after one refusal, not after a retry", async () => {
    let meCalls = 0
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1
        return HttpResponse.json({ detail: "boom" }, { status: 500 })
      }),
    )

    // Deliberately not `renderApp`: its client sets `retry: false` as a default
    // for *every* query, which masks each hook's own policy -- `useSession`'s
    // included. This client keeps the library's default of three retries and
    // only takes the backoff away, so the number of requests is the hook's
    // setting and nothing else, and no test waits for a timer.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <RequireSession>
            <h1>Daemons</h1>
          </RequireSession>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText(/did not answer/i)).toBeInTheDocument()
    // The error screen arrives either way; that it arrives after exactly one
    // refusal is `useSession`'s own `retry: false`. Which screen is legal to
    // show is what this answer decides, so a server that cannot answer has to
    // say so now rather than three backoffs from now.
    expect(meCalls).toBe(1)
  })

  it("says the server did not answer rather than guessing which screen to show", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByText(/did not answer/i)).toBeInTheDocument()
    // Neither pre-session screen: the interface does not know which is right.
    expect(screen.queryByRole("heading", { name: /sign in/i })).not.toBeInTheDocument()
    expect(
      screen.queryByRole("heading", { name: /set a password/i }),
    ).not.toBeInTheDocument()
  })
})

/**
 * The 401 nobody could see, and the two things it must not be confused with.
 *
 * `/ws/ui` is refused at the handshake when the session is gone -- a server
 * restart is enough, because `Sessions` is in memory and says so -- and a browser
 * is given **no status code** for a rejected handshake: `onclose` fires 1006,
 * which is character-for-character what a server that is not running produces.
 * Nothing polls, so before this rule no `fetch` ever met that 401 and the tab sat
 * on a stale panel dialling a refusal every thirty seconds with nothing anywhere
 * saying why.
 *
 * The fix is not a second rule. The socket asks one guarded route whether this
 * browser still has a session, and the answer -- if it is a refusal -- is an
 * ordinary 401 arriving at the one handler above, latch, exclusion and all.
 *
 * These two tests are the same event with one thing changed: whether a server
 * answered. That is the entire difference between a session that **ended** and a
 * server that is **down**, and it is the difference the WebSocket could not carry.
 */
describe("a refused handshake", () => {
  /** The connection the interface is holding -- the last one, past StrictMode's. */
  function held(sockets: FakeSocket[]): FakeSocket {
    const socket = sockets.at(-1)
    if (socket === undefined) throw new Error("the interface dialled nothing")
    return socket
  }

  it("is a 401 like any other, and returns to login once", async () => {
    const sockets = collectSockets()
    let asked = 0
    let meCalls = 0
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1
        // Signed in when the page loaded; forgotten by the time the socket is
        // refused, which is exactly what a server restart does.
        return HttpResponse.json({ authenticated: meCalls === 1, password_set: true })
      }),
      http.get("/api/daemons", () => HttpResponse.json([])),
      NO_CLAIMS,
      // The route the question is asked on. It is guarded, so it can say the one
      // thing `GET /api/auth/me` never can.
      http.get("/api/settings", () => {
        asked += 1
        return HttpResponse.json({ detail: "not authenticated" }, { status: 401 })
      }),
    )
    const { pushes, path } = renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()

    // Refused before it ever opened -- which is all the browser will ever say
    // about it.
    act(() => held(sockets).drop())

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    expect(path()).toBe("/login")
    // Once, and asked once: the redirect is stopped from repeating by the latch
    // in `App`, and the question by the client's own.
    expect(pushes()).toBe(1)
    expect(asked).toBe(1)
  })

  it("is not a session that ended when nothing answered at all", async () => {
    const sockets = collectSockets()
    let asked = 0
    server.use(
      me({ authenticated: true, password_set: true }),
      http.get("/api/daemons", () => HttpResponse.json([])),
      NO_CLAIMS,
      // No server. `HttpResponse.error()` is a `fetch` that rejects, which is
      // what a stopped server gives a browser -- no status, nothing to read.
      http.get("/api/settings", () => {
        asked += 1
        return HttpResponse.error()
      }),
    )
    const { pushes, path } = renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()

    act(() => held(sockets).drop())
    // The question really was asked -- otherwise this test would pass against an
    // interface that does nothing at all, which is the interface it exists to
    // refuse.
    await waitFor(() => expect(asked).toBe(1))

    // And nothing was concluded from silence. The rack is still rendering and
    // the panels still say what they were saying; a server that is down is not a
    // session that ended, and signing somebody out because their server is
    // restarting would be this interface inventing a fact it was never told.
    expect(path()).toBe("/daemons")
    expect(pushes()).toBe(0)
    expect(screen.queryByRole("heading", { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Daemons" })).toBeInTheDocument()
  })

  /**
   * The real order of events, with one thing varied: what the first question got.
   *
   * **This is what makes "ask once" the wrong rule.** The server goes away
   * first, and comes back second having forgotten this browser's session -- so a
   * client that spent its one question on the outage would never notice the
   * refusal that follows, which is the original defect with an extra step in it.
   * The two callers below are the same event in different clothes: `fetch`
   * rejecting because nothing accepted the connection at all, and a proxy's 502
   * because something did and the server behind it is not running. Neither
   * answers "does this browser still have a session", and either taken for an
   * answer leaves the tab refused forever with nothing saying why.
   *
   * Drives the client's own backoff rather than a fake clock: the second dial is
   * the one the client makes on its own, 750-1000 ms later, which is also the
   * only way this test can be sure the refusal did not stop it dialling.
   */
  async function outageThenRefusal(firstAnswer: () => Response) {
    const sockets = collectSockets()
    const asked: number[] = []
    let meCalls = 0
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1
        return HttpResponse.json({ authenticated: meCalls === 1, password_set: true })
      }),
      http.get("/api/daemons", () => HttpResponse.json([])),
      NO_CLAIMS,
      http.get("/api/settings", () => {
        asked.push(asked.length)
        return asked.length === 1
          ? firstAnswer()
          : HttpResponse.json({ detail: "not authenticated" }, { status: 401 })
      }),
    )
    const { path } = renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()
    const dialled = sockets.length

    act(() => held(sockets).drop())
    await waitFor(() => expect(asked).toHaveLength(1))
    // Nothing has been concluded yet, because nothing has said anything yet.
    expect(path()).toBe("/daemons")

    await waitFor(() => expect(sockets).toHaveLength(dialled + 1), { timeout: 3000 })
    act(() => held(sockets).drop())
    return { asked, path }
  }

  it("asks again when the server comes back, because the outage happens first", async () => {
    const { asked, path } = await outageThenRefusal(() => HttpResponse.error())

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    expect(path()).toBe("/login")
    // Twice in total: once for the outage that answered nothing, once for the
    // refusal that did. Not once per dial, and not only ever once.
    expect(asked).toHaveLength(2)
  })

  it("does not take a proxy's 502 for an answer either", async () => {
    // The same story with something at the other end of the socket that speaks
    // HTTP without knowing what a session is. A client that latched on "some
    // status came back" would be refused forever behind any reverse proxy --
    // which is how this rule would have been reintroduced by a deployment
    // nobody tested.
    const { asked, path } = await outageThenRefusal(() =>
      HttpResponse.json({ detail: "bad gateway" }, { status: 502 }),
    )

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    expect(path()).toBe("/login")
    expect(asked).toHaveLength(2)
  })
})
