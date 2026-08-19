import { act, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, PendingClaim } from "../src/api/queries"
import { collectSockets, type FakeSocket } from "./fake-socket"
import { server } from "./msw"
import { renderApp } from "./render"

// Two racks asking to join: pi-shed and pi-barn. **No value in one is equal to,
// or a substring of, any value in the other**, and none of them equals anything
// in the rack fixtures below either -- not the hostname, not the address, not
// the short code, not the version, not the moment it was first seen. That is
// the whole reason the fixture looks like this: a component that read the wrong
// claim's field, or keyed a dialog on `hostname` where the route wants
// `fingerprint`, is invisible against a fixture where two of those coincide,
// and this suite has been caught by exactly that four times.
//
// The fingerprints and codes are real: `sha256(b"shed")` and `sha256(b"barn")`
// hex, with the first six characters of their base32 as the code, which is what
// `daemon/src/ors_daemon/identity.py` computes. So the pair a person compares --
// what the Pi printed and what this page shows -- is derived here the way it is
// derived there, rather than being two strings that happen to sit in one object.

const SHED = {
  fingerprint: "2f3c3e5cf3c63b648b44850ff5e9a88aac1d4498e94e7575f2fe6ad93f35c66b",
  hostname: "pi-shed",
  address: "192.168.1.34",
  short_code: "F46D4X",
  version: "0.4.2",
  first_seen: Date.parse("2026-08-20T09:14:07Z") / 1000,
} satisfies PendingClaim

const BARN = {
  fingerprint: "e43a7ece3a362be44a9eadddd66fc927b8d592c22060595b5b2d2e20cbd22be9",
  hostname: "pi-barn",
  address: "10.0.7.211",
  short_code: "4Q5H5T",
  version: "0.5.0",
  first_seen: Date.parse("2026-08-20T09:41:52Z") / 1000,
} satisfies PendingClaim

const SIGNED_IN = http.get("/api/auth/me", () =>
  HttpResponse.json({ authenticated: true, password_set: true }),
)

/** One paired rack, as `GET /api/daemons` reports it. Every field the page reads. */
function rack(id: number, name: string): Daemon {
  return {
    id,
    name,
    status: "paired",
    online: false,
    config_version: 8,
    applied_version: 8,
    config_error: null,
    version: "0.3.1",
    capabilities: {},
    last_seen: null,
    paired_at: "2026-08-01T09:15:00Z",
    created_at: "2026-08-01T09:00:00Z",
  }
}

/** The racks already paired. Named so no name is a claim's hostname, or a substring of one. */
const RACKS = http.get("/api/daemons", () =>
  HttpResponse.json([rack(17, "pi-loft"), rack(42, "pi-cellar")]),
)

/** Every rack card asks for its own events; nothing here is about them. */
const NO_EVENTS = http.get("/api/events", () => HttpResponse.json([]))

const claims = (pending: PendingClaim[]) =>
  http.get("/api/claims", () => HttpResponse.json(pending))

/** The card for one waiting rack, found by the accessible name of its region. */
function waiting(hostname: string) {
  return within(screen.getByRole("region", { name: hostname }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("the racks waiting to join", () => {
  it("draws no section at all when nothing is waiting", async () => {
    let asked = 0
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      http.get("/api/claims", () => {
        asked += 1
        return HttpResponse.json([])
      }),
    )
    renderApp({ at: "/daemons" })

    // The page really rendered, so the absences below are absences and not a
    // page that has not arrived yet.
    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    // And the list really was asked for, so "no section" is a decision this
    // page took about an empty answer rather than a query nobody mounted.
    await waitFor(() => expect(asked).toBeGreaterThan(0))

    // No heading, and no landmark either. An empty "Waiting to join" is a
    // region a screen reader can enter and find nothing in -- the same rule
    // `RackCanvas` follows for a rack with no panels.
    expect(screen.queryByRole("heading", { name: /waiting to join/i })).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: /waiting to join/i })).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/waiting to join/i)
  })

  it("shows each waiting rack's hostname, address, short code and version", async () => {
    server.use(SIGNED_IN, RACKS, NO_EVENTS, claims([SHED, BARN]))
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()

    const shed = waiting(SHED.hostname)
    expect(shed.getByText(SHED.address)).toBeInTheDocument()
    expect(shed.getByText(SHED.short_code)).toBeInTheDocument()
    expect(shed.getByText(SHED.version)).toBeInTheDocument()
    // When it was first seen, as the instant the server recorded, written the
    // way every other timestamp in this interface is written -- so the row
    // lines up with the server's log and the Pi's.
    expect(shed.getByText("2026-08-20T09:14:07Z")).toBeInTheDocument()

    const barn = waiting(BARN.hostname)
    expect(barn.getByText(BARN.address)).toBeInTheDocument()
    expect(barn.getByText(BARN.short_code)).toBeInTheDocument()
    expect(barn.getByText(BARN.version)).toBeInTheDocument()
    expect(barn.getByText("2026-08-20T09:41:52Z")).toBeInTheDocument()

    // And each entry carries only its own. Two rows drawn from one claim, or
    // from index 0 twice, passes every positive assertion above.
    const shedRegion = screen.getByRole("region", { name: SHED.hostname })
    const barnRegion = screen.getByRole("region", { name: BARN.hostname })
    expect(shedRegion).not.toHaveTextContent(BARN.address)
    expect(shedRegion).not.toHaveTextContent(BARN.short_code)
    expect(shedRegion).not.toHaveTextContent(BARN.version)
    expect(barnRegion).not.toHaveTextContent(SHED.address)
    expect(barnRegion).not.toHaveTextContent(SHED.short_code)
    expect(barnRegion).not.toHaveTextContent(SHED.version)

    // The fingerprint is the key the two admin routes take, and it is not
    // something a person compares against anything. It is not drawn.
    expect(document.body).not.toHaveTextContent(SHED.fingerprint)
    expect(document.body).not.toHaveTextContent(BARN.fingerprint)
  })

  it("puts the short code in the approve dialog and says it must match the Pi", async () => {
    server.use(SIGNED_IN, RACKS, NO_EVENTS, claims([SHED, BARN]))
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()
    await userEvent.click(waiting(BARN.hostname).getByRole("button", { name: "Approve" }))

    const dialog = within(await screen.findByRole("dialog"))
    // The rack it is about, so the code below cannot be read against the wrong
    // card.
    expect(dialog.getByRole("heading", { name: `Approve ${BARN.hostname}?` })).toBeInTheDocument()

    // The code itself -- this claim's, not the other one's. It is the only
    // thing that makes the click mean anything, so its absence is the defect
    // this assertion exists for.
    expect(dialog.getByText(BARN.short_code)).toBeInTheDocument()
    expect(dialog.queryByText(SHED.short_code)).not.toBeInTheDocument()

    // What to do with it, and what happens if you do not. Spec S6.4 asks for
    // this sentence in these words.
    expect(dialog.getByText(/must match/i)).toBeInTheDocument()
    expect(dialog.getByText(/printed/i)).toBeInTheDocument()
    expect(
      dialog.getByText(/approving a stranger's rack onto your server/i),
    ).toBeInTheDocument()

    // What approving grants, named rather than left to be guessed: this
    // server's configuration, and this rack's panels.
    expect(dialog.getByText(/configuration/i)).toBeInTheDocument()
    expect(dialog.getByText(/panels/i)).toBeInTheDocument()

    // And what the code does *not* prove. Thirty bits is a check against
    // confusing two racks you are choosing between, not against somebody who
    // has already seen the code -- and a dialog that implied otherwise would be
    // overstating the one guarantee this whole flow rests on.
    expect(dialog.getByText(/30 bits/i)).toBeInTheDocument()
    expect(dialog.getByText(/already seen/i)).toBeInTheDocument()
  })

  it("approves the claim it named, and that entry leaves the list", async () => {
    // Two waiting racks, so "it approved a rack" and "it approved *this* rack"
    // are different outcomes. pi-barn is the one confirmed; pi-shed must stay.
    const approved: string[] = []
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      // The claim really goes, so the list after the click is the list the
      // invalidation asks for rather than a fixture that pretends.
      http.get("/api/claims", () =>
        HttpResponse.json(
          [SHED, BARN].filter((claim) => !approved.includes(claim.fingerprint)),
        ),
      ),
      http.post("/api/claims/:fingerprint/approve", ({ params }) => {
        approved.push(String(params.fingerprint))
        return HttpResponse.json({ id: 61, name: BARN.hostname })
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()
    await userEvent.click(waiting(BARN.hostname).getByRole("button", { name: "Approve" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Approve this rack",
      }),
    )

    // The admin routes key on the fingerprint, and it is this claim's -- not
    // the other one's, not the hostname, and not the short code, all three of
    // which are strings a component could have reached for instead.
    await waitFor(() => expect(approved).toEqual([BARN.fingerprint]))
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: BARN.hostname })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole("region", { name: SHED.hostname })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("names the collision when an approval is refused for the hostname", async () => {
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      claims([SHED, BARN]),
      // What a `daemon.name` collision looks like from here: a *different* rack
      // already holds that hostname, the claim stays pending and deniable, and
      // the sentence is the route's own.
      http.post("/api/claims/:fingerprint/approve", () =>
        HttpResponse.json(
          { detail: "a daemon with that name already exists" },
          { status: 409 },
        ),
      ),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()
    await userEvent.click(waiting(SHED.hostname).getByRole("button", { name: "Approve" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Approve this rack",
      }),
    )

    // Still up, still asking, and saying what is actually wrong. A 409 rendered
    // as the same red line every other refusal gets tells an admin nothing
    // about the one thing they can do about it.
    const dialog = within(await screen.findByRole("dialog"))
    const refusal = await dialog.findByRole("alert")
    // The name that collided, so the rack to go and look at is named.
    expect(refusal).toHaveTextContent(SHED.hostname)
    // That it is a *different* rack -- not this one already being paired.
    expect(refusal).toHaveTextContent(/another rack/i)
    // The way out, and the promise that nothing was lost by trying.
    expect(refusal).toHaveTextContent(/rename/i)
    expect(refusal).toHaveTextContent(/still waiting/i)
    // And the server's own words, because they are the evidence for all of it.
    expect(refusal).toHaveTextContent("a daemon with that name already exists")
    expect(dialog.getByRole("button", { name: "Approve this rack" })).toBeInTheDocument()
  })

  it("says a denial lasts 24 hours before anything is denied", async () => {
    const denied: string[] = []
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      http.get("/api/claims", () =>
        HttpResponse.json([SHED, BARN].filter((claim) => !denied.includes(claim.fingerprint))),
      ),
      http.post("/api/claims/:fingerprint/deny", ({ params }) => {
        denied.push(String(params.fingerprint))
        return HttpResponse.json({ ok: true })
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()
    await userEvent.click(waiting(SHED.hostname).getByRole("button", { name: "Deny" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByRole("heading", { name: `Deny ${SHED.hostname}?` })).toBeInTheDocument()
    // Before it happens, because afterwards is too late to decide otherwise:
    // the fingerprint is suppressed for a day, and a rack denied by accident
    // cannot be let in again until that day is up.
    expect(dialog.getByText(/24 hours/i)).toBeInTheDocument()
    // Nothing has been asked of the server yet -- so the sentence above really
    // is said *before* the click and not in whatever is drawn after it.
    expect(denied).toEqual([])

    await userEvent.click(dialog.getByRole("button", { name: "Deny this rack" }))

    await waitFor(() => expect(denied).toEqual([SHED.fingerprint]))
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: SHED.hostname })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole("region", { name: BARN.hostname })).toBeInTheDocument()
  })

  it("shows a claim that arrives while the page is open, without a reload", async () => {
    let filed = false
    let asked = 0
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      http.get("/api/claims", () => {
        asked += 1
        return HttpResponse.json(filed ? [SHED] : [])
      }),
    )
    // Stubbed before the render, because `AppShell` dials on mount.
    const sockets: FakeSocket[] = collectSockets()
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    await waitFor(() => expect(asked).toBe(1))

    // The rack files its claim. Nothing on this page has been told, and nothing
    // here polls, so the page is still right about what it last read.
    filed = true
    expect(screen.queryByRole("region", { name: SHED.hostname })).not.toBeInTheDocument()
    expect(asked).toBe(1)

    // The last socket, not the first: StrictMode builds, closes and replaces
    // the provider's client before anything a test does.
    const live = sockets.at(-1)
    if (live === undefined) throw new Error("the interface dialled nothing")
    act(() => live.accept())
    act(() => live.deliver(JSON.stringify({ type: "daemons", online: [17] })))

    expect(
      await screen.findByRole("region", { name: SHED.hostname }),
    ).toBeInTheDocument()
    // Re-read from the server rather than patched here: no server state is
    // invented by this client, and the socket says nothing about what a claim
    // contains.
    expect(asked).toBeGreaterThan(1)
    expect(screen.getByText(SHED.short_code)).toBeInTheDocument()
  })

  it("keeps the token flow, described as what a rack that cannot be discovered uses", async () => {
    server.use(SIGNED_IN, RACKS, NO_EVENTS, claims([]))
    renderApp({ at: "/daemons" })

    await userEvent.click(await screen.findByRole("button", { name: "Pair a rack" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByRole("textbox", { name: /name/i })).toBeInTheDocument()
    // Reachable, and honest about when it is the one to use: a rack that cannot
    // find this server on its own -- a network that drops multicast, a rack on
    // another subnet -- has no other way in.
    expect(dialog.getByText(/cannot find this server/i)).toBeInTheDocument()
  })
})
