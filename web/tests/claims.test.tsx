import { act, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  CLAIMS_FRESH_MS,
  CLAIMS_POLL_MS,
  type Daemon,
  type PendingClaim,
} from "../src/api/queries"
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
  // Real timers first: React Testing Library's automatic cleanup unmounts the
  // tree after this hook, and unmounting under a frozen clock is how a pending
  // effect becomes a test that hangs rather than a test that fails.
  vi.useRealTimers()
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

  it("approves the claim it named, and that entry becomes a rack on the page", async () => {
    // Two waiting racks, so "it approved a rack" and "it approved *this* rack"
    // are different outcomes. pi-barn is the one confirmed; pi-shed must stay.
    const approved: string[] = []
    // And the racks the server holds, which an approval really adds to: two
    // before the click and three after. This is the half that cannot be left
    // out. An approval that only re-read the claims would make the card vanish
    // and put nothing in its place -- and nothing else on this page could put
    // it there afterwards, because `LiveProvider.withOnline` maps over the rows
    // the cache already holds, so a rack that did not exist a moment ago never
    // enters the daemon list from the socket. The user would be left looking at
    // the visible end of this whole flow having silently not happened.
    const racks = [rack(17, "pi-loft"), rack(42, "pi-cellar")]
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(racks)),
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
        // What the route does: creates the rack. From here on the daemon
        // listing has it, exactly as the server's would.
        racks.push(rack(61, BARN.hostname))
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

    // The rack that claim became, on the page, with nobody navigating. Asked
    // for as a *rack* card rather than by name: from now on the name is
    // ambiguous by design -- a claim card and a rack card would both answer to
    // it -- and `RackCard`'s heading is an `h2` where `ClaimCard`'s is an `h3`.
    expect(
      await screen.findByRole("heading", { name: BARN.hostname, level: 2 }),
    ).toBeInTheDocument()

    // And the entry left the waiting list, which is the other half.
    const stillWaiting = within(screen.getByRole("region", { name: /waiting to join/i }))
    expect(stillWaiting.queryByRole("heading", { name: BARN.hostname })).not.toBeInTheDocument()
    expect(stillWaiting.getByRole("heading", { name: SHED.hostname })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("says what a refusal that is not a name collision actually said", async () => {
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      claims([SHED, BARN]),
      // Not a 409. There is no remedy behind this one and no rack to go and
      // rename: something on the server could not write, and the only honest
      // thing to draw is what it said.
      http.post("/api/claims/:fingerprint/approve", () =>
        HttpResponse.json({ detail: "the database is locked" }, { status: 500 }),
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

    // Something is said at all. A refusal that rendered nothing leaves a dialog
    // that looks as though the button did not work.
    const dialog = within(await screen.findByRole("dialog"))
    const refusal = await dialog.findByRole("alert")
    expect(refusal).toHaveTextContent("the database is locked")

    // And it is not the collision sentence. That one names a rack to go and
    // rename, which here would be a rack that does not exist and an admin sent
    // to do the wrong thing about a database that could not be written.
    expect(document.body).not.toHaveTextContent(/another rack/i)
    expect(document.body).not.toHaveTextContent(/rename/i)

    // Still up, still asking about the same claim.
    expect(dialog.getByRole("button", { name: "Approve this rack" })).toBeInTheDocument()
    expect(dialog.getByRole("heading", { name: `Approve ${SHED.hostname}?` })).toBeInTheDocument()
  })

  it("says so when the waiting list cannot be read, rather than looking empty", async () => {
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      http.get("/api/claims", () =>
        HttpResponse.json({ detail: "the claims table could not be read" }, { status: 500 }),
      ),
    )
    renderApp({ at: "/daemons" })

    // A failed read drawn as nothing is a page that looks exactly like "nothing
    // is waiting" while the rack somebody is standing next to is invisible.
    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent("the claims table could not be read")
    // And it says what the consequence is, rather than only that something broke.
    expect(refusal).toHaveTextContent(/would not be shown/i)

    // Without promising a section behind it: an `Alert` is a `role="alert"`
    // div, not a landmark, so there is no "Waiting to join" region to enter.
    expect(screen.queryByRole("region", { name: /waiting to join/i })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: /waiting to join/i })).not.toBeInTheDocument()

    // And the rest of the page is unharmed: the racks that did join are drawn.
    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
  })

  it("leaves a refused denial on screen, in the server's own words", async () => {
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      claims([SHED, BARN]),
      http.post("/api/claims/:fingerprint/deny", () =>
        HttpResponse.json({ detail: "the claim could not be removed" }, { status: 500 }),
      ),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: /waiting to join/i })).toBeInTheDocument()
    await userEvent.click(waiting(BARN.hostname).getByRole("button", { name: "Deny" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Deny this rack" }),
    )

    // A dialog that dismissed itself here would leave the claim on the page
    // with nothing saying why it is still there.
    const dialog = within(await screen.findByRole("dialog"))
    expect(await dialog.findByRole("alert")).toHaveTextContent("the claim could not be removed")
    expect(dialog.getByRole("button", { name: "Deny this rack" })).toBeInTheDocument()
    expect(dialog.getByRole("heading", { name: `Deny ${BARN.hostname}?` })).toBeInTheDocument()
    // And the claim is exactly as pending as it was. `hidden: true` because
    // Radix marks everything behind an open modal `aria-hidden`, so the card is
    // on the page but out of the accessibility tree until this dialog closes.
    expect(
      screen.getByRole("region", { name: BARN.hostname, hidden: true }),
    ).toBeInTheDocument()
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

  it("shows a claim filed while the page is open, with nothing on the socket", async () => {
    let filed = false
    let asked = 0
    server.use(
      SIGNED_IN,
      RACKS,
      NO_EVENTS,
      http.get("/api/claims", () => {
        asked += 1
        return HttpResponse.json(filed ? [BARN] : [])
      }),
    )
    // The demonstration this section exists for: an admin with the page open, a
    // network where nothing else is moving, and somebody plugging a Pi in. The
    // server has no message for that -- `POST /api/racks/claims` is
    // unauthenticated, touches no hub and wakes no browser, and `ws_ui.py`
    // encodes only `frame` and `daemons` -- so **no socket is accepted and none
    // is delivered to anywhere in this test**, and what has to put the entry on
    // the page is the query's own interval.
    //
    // Fake timers, installed before the render so the interval React Query arms
    // on subscribe is a fake one. `shouldAdvanceTime` keeps the clock moving
    // with the real one, which is what lets MSW answer and `findBy*` resolve at
    // all while the timers are frozen.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const sockets: FakeSocket[] = collectSockets()
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    await waitFor(() => expect(asked).toBe(1))

    // The rack files its claim. Nothing on this page has been told.
    filed = true
    expect(screen.queryByRole("region", { name: BARN.hostname })).not.toBeInTheDocument()
    expect(asked).toBe(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CLAIMS_POLL_MS + 500)
    })

    expect(await screen.findByRole("region", { name: BARN.hostname })).toBeInTheDocument()
    expect(screen.getByText(BARN.short_code)).toBeInTheDocument()
    expect(asked).toBeGreaterThan(1)
    // And nothing arrived on a socket to do it. Every connection the interface
    // dialled is still unopened, so no message can have been read from one.
    expect(sockets.length).toBeGreaterThan(0)
    expect(sockets.every((socket) => socket.state !== "open")).toBe(true)
  })

  it("re-reads the waiting list when the socket says the connected racks changed", async () => {
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
    // The other half of "without a reload", and the one that matters at the end
    // of this flow: an approved rack collects its key and dials in, the server
    // says which racks are connected, and the list is re-read at once rather
    // than up to a tick later.
    //
    // Fake timers because what this steps over is `CLAIMS_FRESH_MS`: the
    // socket's invalidation is filtered on staleness, on purpose, so that the
    // `daemons` message this socket sends the instant it opens does not make
    // every load of this page pay for two reads of a route that writes.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // Stubbed before the render, because `AppShell` dials on mount.
    const sockets: FakeSocket[] = collectSockets()
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    await waitFor(() => expect(asked).toBe(1))

    // The rack files its claim, and the page is still right about what it last
    // read. Well short of the interval, so nothing below can be the interval.
    filed = true
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CLAIMS_FRESH_MS + 500)
    })
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
    // contains. Exactly one further read, and the clock is still nowhere near
    // the interval, so the message is what caused it.
    expect(asked).toBe(2)
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
