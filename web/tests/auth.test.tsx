import { act, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { describe, expect, it } from "vitest"

import { api } from "../src/api/client"
import { server } from "./msw"
import { renderApp } from "./render"

// `GET /api/auth/me` is open and always answers 200 with two booleans -- it is
// the one route that tells the interface which of the two pre-session states
// the server is in, so it can neither 401 nor 409. The two booleans are always
// stubbed to different values where the behaviour depends on only one of them,
// so reading the wrong field cannot pass.
function me(state: { authenticated: boolean; password_set: boolean }) {
  return http.get("/api/auth/me", () => HttpResponse.json(state))
}

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
    server.use(
      http.get("/api/auth/me", () => {
        meCalls += 1
        // Signed in on the first ask, expired on every one after.
        return HttpResponse.json({ authenticated: meCalls === 1, password_set: true })
      }),
      // The route the expiry is discovered on. `/api/auth/me` never 401s; the
      // session-guarded routes do, and that 401 is what has to be noticed.
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    )
    const { pushes, path } = renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()

    // Two protected requests in flight when the session goes, which is the
    // ordinary case on a page that loads more than one thing.
    await act(async () => {
      await Promise.all([api.GET("/api/daemons"), api.GET("/api/daemons")])
    })

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()
    // Once. Two 401s are one return to /login, not one per refused request --
    // and nothing bounces back and forth between the guard and the login page.
    await waitFor(() => expect(meCalls).toBeLessThan(4))
    expect(pushes()).toBe(1)
    expect(path()).toBe("/login")
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

  it("signs in and lands on the page it turned away", async () => {
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
    )
    // Asked for the rack first, the way anyone actually arrives. That is what
    // leaves a "not signed in" answer in the cache for the guard to read again
    // a moment later, so signing in has to be more than a redirect.
    renderApp({ at: "/daemons" })
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument()

    await userEvent.type(await screen.findByLabelText(/password/i), "the-real-password")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByRole("heading", { name: "Daemons" })).toBeInTheDocument()
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
