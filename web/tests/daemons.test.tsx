import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { describe, expect, it } from "vitest"

import type { Daemon } from "../src/api/queries"
import { server } from "./msw"
import { renderApp } from "./render"

// Racks 5 (pi-attic), 17 (pi-loft) and 42 (pi-cellar), in that order because
// `GET /api/daemons` is ordered by id. Three properties are deliberate and each
// one has caught something in this project before:
//
//   * no id equals its own index in the list (0, 1, 2), so a card that read the
//     wrong rack by position cannot pass by coincidence;
//   * no id equals any `config_version` or `applied_version` used below (4, 8,
//     9, 12, 31), so a component that rendered an id where a version belongs --
//     or the other way round -- is visible;
//   * the rack a push is *about* (17) and the rack the unservable header
//     *names* (42) are different in the test that separates them, which is the
//     only way "read the header" and "name the rack you just pushed" can be
//     told apart at all.
//
// Names are as unalike as the ids: no name is a substring of another, so a
// `not.toHaveTextContent` on one of them cannot be satisfied by the absence of
// a different one.

const SIGNED_IN = http.get("/api/auth/me", () =>
  HttpResponse.json({ authenticated: true, password_set: true }),
)

/** One rack as `GET /api/daemons` reports it. Every field the page reads. */
function rack(over: Partial<Daemon> & Pick<Daemon, "id" | "name">): Daemon {
  return {
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
    ...over,
  }
}

function listing(racks: Daemon[]) {
  return http.get("/api/daemons", () => HttpResponse.json(racks))
}

/** The events route, stubbed empty. Every rack card asks it for its own rack. */
const NO_EVENTS = http.get("/api/events", () => HttpResponse.json([]))

/** The card for one rack, found by the accessible name of its region. */
function card(name: string) {
  return within(screen.getByRole("region", { name }))
}

describe("the racks", () => {
  it("shows the pairing token once, with the command that carries it", async () => {
    const TOKEN = "tok-9f3ac1e5d2b74806"
    // Recorded rather than asserted inside the handler. An `expect` that throws
    // in an MSW resolver becomes a failed *request*, which `useMutate` catches
    // and the page renders as a refusal -- a green test against a page that
    // never showed a token. The assertion belongs out here, where a failure is
    // a failure.
    let posted: unknown = null
    server.use(
      SIGNED_IN,
      listing([]),
      NO_EVENTS,
      http.post("/api/daemons", async ({ request }) => {
        posted = await request.json()
        return HttpResponse.json(
          { id: 42, name: "pi-cellar", token: TOKEN },
          { status: 201 },
        )
      }),
    )
    renderApp({ at: "/daemons" })

    await userEvent.click(await screen.findByRole("button", { name: "Pair a rack" }))
    await userEvent.type(
      await screen.findByRole("textbox", { name: /name/i }),
      "pi-cellar",
    )
    await userEvent.click(screen.getByRole("button", { name: "Mint the pairing token" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(posted).toEqual({ name: "pi-cellar" })

    // The token itself, on its own, as something a person can select and copy.
    expect(dialog.getByText(TOKEN)).toBeInTheDocument()
    // And the line that carries it to the Pi. Both flags are required by
    // `ors-daemon connect`, so a line missing either is not runnable.
    expect(
      dialog.getByText(
        `ors-daemon connect --server ${window.location.origin} --token ${TOKEN}`,
      ),
    ).toBeInTheDocument()
    // At the same moment, per the spec: "losing it means rotating again; the
    // interface says so at the moment it shows it". Not on a help page, not
    // after the fact -- here, beside the only copy of the token there is.
    expect(dialog.getByText(/only time it is shown/i)).toBeInTheDocument()
    expect(dialog.getByText(/rotate this rack's key/i)).toBeInTheDocument()
  })

  it("never shows a token again from any other route", async () => {
    const TOKEN = "tok-4be07c19a8d6f253"
    const racks: Daemon[] = []
    let listings = 0
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => {
        listings += 1
        return HttpResponse.json(racks)
      }),
      NO_EVENTS,
      http.post("/api/daemons", () => {
        // What the server really does: the row exists from now on, and
        // `DaemonView` has no `token` field to answer with. The list below is
        // the list every later render of this page is drawn from.
        racks.push(rack({ id: 42, name: "pi-cellar", status: "unpaired" }))
        return HttpResponse.json(
          { id: 42, name: "pi-cellar", token: TOKEN },
          { status: 201 },
        )
      }),
    )
    renderApp({ at: "/daemons" })

    await userEvent.click(await screen.findByRole("button", { name: "Pair a rack" }))
    await userEvent.type(
      await screen.findByRole("textbox", { name: /name/i }),
      "pi-cellar",
    )
    await userEvent.click(screen.getByRole("button", { name: "Mint the pairing token" }))
    expect(await screen.findByText(TOKEN)).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Done" }))

    // The list really was re-read -- otherwise "the token is gone" would only
    // mean "nothing has been drawn again since".
    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    expect(listings).toBeGreaterThan(1)

    // Asserted on the rendered page, not on the fixture. That the response has
    // no `token` field proves something about the server; this is the claim
    // about the interface.
    expect(screen.queryByText(TOKEN)).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(TOKEN)
    // Including anywhere a form is still holding it.
    expect(screen.queryByDisplayValue(TOKEN)).not.toBeInTheDocument()

    // And not from the dialog's own memory. Opening it again asks for a name;
    // it does not re-show what it minted a moment ago. Closing it is what ends
    // the "once", and a dialog that kept its last answer would show a token for
    // a rack that has since been paired -- from a page reload away from being
    // gone, which is not the same as gone.
    await userEvent.click(screen.getByRole("button", { name: "Pair a rack" }))
    const again = within(await screen.findByRole("dialog"))
    expect(again.getByRole("textbox", { name: /name/i })).toBeInTheDocument()
    expect(again.queryByText(TOKEN)).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(TOKEN)
  })

  it("keeps the typed name when the server refuses it as a duplicate", async () => {
    server.use(
      SIGNED_IN,
      // The name really is taken, which is the case the route refuses.
      listing([rack({ id: 42, name: "pi-rack" })]),
      NO_EVENTS,
      http.post("/api/daemons", () =>
        HttpResponse.json({ detail: "a daemon named 'pi-rack' exists" }, { status: 409 }),
      ),
    )
    renderApp({ at: "/daemons" })

    await userEvent.click(await screen.findByRole("button", { name: "Pair a rack" }))
    const named = await screen.findByRole("textbox", { name: /name/i })
    await userEvent.type(named, "pi-rack")
    await userEvent.click(screen.getByRole("button", { name: "Mint the pairing token" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(await dialog.findByRole("alert")).toHaveTextContent(
      "a daemon named 'pi-rack' exists",
    )
    // The promise this dialog makes about a refusal: what was typed is still
    // where it was, so the fix is one edit away rather than a re-type. The name
    // lives in form state for exactly this -- `useMutate` invalidates on settle
    // and no refetch can reach it.
    expect(dialog.getByRole("textbox", { name: /name/i })).toHaveValue("pi-rack")
    expect(named).toHaveValue("pi-rack")
    // And nothing was minted, so nothing is claiming to be a token.
    expect(dialog.queryByText(/only time it is shown/i)).not.toBeInTheDocument()
    expect(dialog.getByRole("button", { name: "Mint the pairing token" })).toBeInTheDocument()
  })

  it("reports delivered honestly when a push reached nobody", async () => {
    const pushed: number[] = []
    server.use(
      SIGNED_IN,
      // The push is recorded against the rack by the server, so the rack's
      // events are stale the moment the button is pressed. This answers as the
      // server would: nothing before the push, the recorded line after it.
      http.get("/api/events", ({ request }) => {
        const asked = new URL(request.url).searchParams
        if (asked.get("daemon_id") !== "17" || pushed.length === 0) {
          return HttpResponse.json([])
        }
        return HttpResponse.json([
          {
            id: 907,
            daemon_id: 17,
            at: "2026-08-15T11:09:04Z",
            level: "info",
            kind: "push",
            message: "the configuration was pushed by hand",
          },
        ])
      }),
      listing([
        rack({ id: 5, name: "pi-attic" }),
        // Connected, and its committed configuration cannot be built. The hub
        // holds a socket for it, so `online` is true -- and there is no
        // snapshot to send down it.
        rack({
          id: 17,
          name: "pi-loft",
          online: true,
          status: "paired",
          config_error: "screen 61 names template 14, which does not exist",
        }),
      ]),
      http.post("/api/daemons/:daemon_id/push", ({ params }) => {
        pushed.push(Number(params.daemon_id))
        return HttpResponse.json(
          { version: 31, delivered: false },
          { status: 202, headers: { "X-Unservable-Daemons": "17" } },
        )
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    const loft = card("pi-loft")
    // The premise: this rack is up. If it were not, "not delivered" would say
    // nothing about the interface refusing to translate one into the other.
    expect(loft.getByText(/online/i)).toBeInTheDocument()

    await userEvent.click(loft.getByRole("button", { name: "Push configuration now" }))

    expect(pushed).toEqual([17])
    expect(
      await screen.findByText("Version 31 was minted, but nothing was sent to pi-loft."),
    ).toBeInTheDocument()
    // The assertion that matters, and it is the negative one: the success
    // wording is ABSENT, not merely the failure wording present. A page that
    // rendered both would pass the positive check alone.
    expect(screen.queryByText(/was delivered to/i)).not.toBeInTheDocument()

    // And the rack's own account of it, re-read because the push made it stale.
    // A list left as it was would be missing the line explaining the version
    // this rack is now behind by.
    expect(
      await loft.findByText("the configuration was pushed by hand"),
    ).toBeInTheDocument()
  })

  it("says which rack did not get an edit, from the header", async () => {
    server.use(
      SIGNED_IN,
      listing([
        rack({ id: 5, name: "pi-attic" }),
        rack({ id: 17, name: "pi-loft", online: true }),
        rack({ id: 42, name: "pi-cellar", online: true }),
      ]),
      NO_EVENTS,
      // The push is about rack 17; the header names 42 and 63. No answer the
      // server really gives separates those -- `push` affects exactly the rack
      // it was called on -- and that is the whole reason the fixture does: a
      // page that named "the rack I just pushed" instead of reading
      // `X-Unservable-Daemons` is indistinguishable from a correct one against
      // any realistic response. 63 is in no listing, which is a rack another
      // tab deleted between this page's fetch and this write.
      http.post("/api/daemons/:daemon_id/push", () =>
        HttpResponse.json(
          { version: 31, delivered: false },
          { status: 202, headers: { "X-Unservable-Daemons": "42,63" } },
        ),
      ),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    await userEvent.click(
      card("pi-loft").getByRole("button", { name: "Push configuration now" }),
    )

    const notice = await screen.findByText(/no configuration that can be sent/i)
    expect(notice).toHaveTextContent("pi-cellar")
    // An id with no row is still a rack somebody has to go and look at.
    expect(notice).toHaveTextContent("rack 63")
    expect(notice).not.toHaveTextContent("pi-loft")
    expect(notice).not.toHaveTextContent("pi-attic")
  })

  it("shows applied_version beside config_version when they differ", async () => {
    server.use(
      SIGNED_IN,
      listing([
        // Saved at 12, the glass still at 9. The pair the whole page exists for.
        rack({ id: 5, name: "pi-attic", config_version: 12, applied_version: 9 }),
        // Null is "no rack has told me", not "old". It must not be drawn as a
        // mismatch, and 4 is not "the version it is behind on".
        rack({ id: 17, name: "pi-loft", config_version: 4, applied_version: null }),
        // Agreed on 8, and still unservable. `config_error` changes without
        // anybody editing anything, so it is its own reason and not the
        // version pair's.
        rack({
          id: 42,
          name: "pi-cellar",
          config_version: 8,
          applied_version: 8,
          config_error: "screen 61 names template 14, which does not exist",
        }),
      ]),
      NO_EVENTS,
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-attic" })).toBeInTheDocument()

    const attic = card("pi-attic")
    expect(attic.getByText("Configuration version 12")).toBeInTheDocument()
    expect(attic.getByText("Applied version 9")).toBeInTheDocument()
    // What is wrong, in a sentence, and carrying both numbers.
    expect(
      attic.getByText(
        "This rack last applied version 9; the saved configuration is version 12, so its panels are showing something older.",
      ),
    ).toBeInTheDocument()

    const loft = card("pi-loft")
    expect(loft.getByText("Configuration version 4")).toBeInTheDocument()
    expect(loft.getByText("Applied version unknown")).toBeInTheDocument()
    expect(loft.queryByText(/showing something older/i)).not.toBeInTheDocument()
    expect(loft.queryByText(/last applied version/i)).not.toBeInTheDocument()

    // And `config_error`, which the spec puts on the same footing: when it is
    // set, the rack says what is wrong, in a sentence -- the server's own.
    const cellar = card("pi-cellar")
    expect(
      cellar.getByText("screen 61 names template 14, which does not exist"),
    ).toBeInTheDocument()
    expect(cellar.queryByText(/showing something older/i)).not.toBeInTheDocument()
  })

  it("names what a delete takes with it", async () => {
    server.use(
      SIGNED_IN,
      listing([rack({ id: 17, name: "pi-loft" }), rack({ id: 42, name: "pi-cellar" })]),
      NO_EVENTS,
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Delete" }))

    const dialog = within(await screen.findByRole("dialog"))
    // The rack it is about, so a confirmation cannot be read against the wrong
    // card.
    expect(dialog.getByRole("heading", { name: "Delete pi-cellar?" })).toBeInTheDocument()
    // What cascades. `screen`, `integration` and `daemon_event` all reference
    // `daemon(id) ON DELETE CASCADE`; the screens are what a person loses.
    expect(dialog.getByText(/screens/i)).toBeInTheDocument()
    expect(dialog.getByText(/cannot be undone/i)).toBeInTheDocument()
  })

  it("deletes the rack it named, and the card goes with it", async () => {
    // Two racks, so "it deleted a rack" and "it deleted *this* rack" are
    // different outcomes. 42 is the one confirmed; 17 must survive it.
    const racks = [rack({ id: 17, name: "pi-loft" }), rack({ id: 42, name: "pi-cellar" })]
    const deleted: number[] = []
    server.use(
      SIGNED_IN,
      // The row really goes, so the list after the delete is the list the
      // invalidation asks for and not a fixture that pretends.
      http.get("/api/daemons", () =>
        HttpResponse.json(racks.filter((each) => !deleted.includes(each.id))),
      ),
      NO_EVENTS,
      http.delete("/api/daemons/:daemon_id", ({ params }) => {
        deleted.push(Number(params.daemon_id))
        // What the route really answers: the default 200 carrying the id it
        // removed. No route in M3a answers 204, this one included.
        return HttpResponse.json({ deleted: Number(params.daemon_id) })
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Delete" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete the rack",
      }),
    )

    // Gone from the page, because `invalidates: [daemonsKey]` asked the server
    // again rather than patching the list here.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "pi-cellar" })).not.toBeInTheDocument(),
    )
    // The id that was on the wire, and it is the rack the confirmation named --
    // not 17, the other row, and not 1, which is what an array index or a
    // count-shaped body would have said.
    expect(deleted).toEqual([42])
    expect(screen.getByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("closes a confirmed delete even when the list cannot be re-read", async () => {
    // The delete lands and the refetch behind it does not. `invalidateQueries`
    // swallows a query's rejection, so the mutation still succeeds, the list
    // keeps the rows it last read -- and the deleted rack's card is therefore
    // still on the page. It is the one success in which nothing unmounts the
    // dialog, so it is the only state that can tell `onSuccess: () =>
    // setOpen(false)` from its absence: without it the confirmation stays up,
    // asking again about a rack that is already gone, and pressing it a second
    // time earns a 404.
    const deleted: number[] = []
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => {
        if (deleted.length > 0) {
          return HttpResponse.json({ detail: "the database is locked" }, { status: 500 })
        }
        return HttpResponse.json([
          rack({ id: 17, name: "pi-loft" }),
          rack({ id: 42, name: "pi-cellar" }),
        ])
      }),
      NO_EVENTS,
      http.delete("/api/daemons/:daemon_id", ({ params }) => {
        deleted.push(Number(params.daemon_id))
        return HttpResponse.json({ deleted: Number(params.daemon_id) })
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Delete" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete the rack",
      }),
    )

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    expect(deleted).toEqual([42])
    // The card the dialog belonged to is still drawn, from the last list that
    // could be read -- so what closed the dialog was the answer, not an unmount.
    expect(screen.getByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    expect(await screen.findByText("the database is locked")).toBeInTheDocument()
  })

  it("leaves a refused delete on screen, in the server's own words", async () => {
    server.use(
      SIGNED_IN,
      listing([rack({ id: 17, name: "pi-loft" }), rack({ id: 42, name: "pi-cellar" })]),
      NO_EVENTS,
      // What another tab having already deleted it looks like from here. The
      // sentence is the route's own (`missing=f"no daemon {daemon_id}"`), and
      // the interface has none of its own to invent.
      http.delete("/api/daemons/:daemon_id", () =>
        HttpResponse.json({ detail: "no daemon 42" }, { status: 404 }),
      ),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Delete" }))
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete the rack",
      }),
    )

    // Still up, still asking, with the reason in it. A dialog that dismissed
    // itself here would leave the rack on the page and nothing saying why.
    const dialog = within(await screen.findByRole("dialog"))
    expect(await dialog.findByRole("alert")).toHaveTextContent("no daemon 42")
    expect(dialog.getByRole("button", { name: "Delete the rack" })).toBeInTheDocument()
    expect(dialog.getByRole("heading", { name: "Delete pi-cellar?" })).toBeInTheDocument()
  })

  it("labels the event list as recent, not as history", async () => {
    const asks: string[] = []
    server.use(
      SIGNED_IN,
      listing([rack({ id: 17, name: "pi-loft" })]),
      http.get("/api/events", ({ request }) => {
        asks.push(request.url)
        const asked = new URL(request.url).searchParams
        if (asked.get("daemon_id") !== "17") return HttpResponse.json([])
        return HttpResponse.json([
          {
            id: 906,
            daemon_id: 17,
            at: "2026-08-15T11:02:41Z",
            level: "warning",
            kind: "disconnect",
            message: "the rack's socket closed",
          },
          {
            id: 905,
            daemon_id: 17,
            at: "2026-08-15T11:02:39Z",
            level: "info",
            kind: "connect",
            message: "the rack connected",
          },
        ])
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-loft" })).toBeInTheDocument()
    const loft = card("pi-loft")

    expect(await loft.findByText("the rack's socket closed")).toBeInTheDocument()
    expect(loft.getByRole("heading", { name: "Recent events" })).toBeInTheDocument()
    // And the reason it is only ever "recent": the ring holds 200 per rack and
    // a flapping rack spends two of them per reconnect, so an event that was
    // here a minute ago may be gone. A panel labelled "history" would be
    // promising something this list cannot keep.
    const why = loft.getByText(/200/)
    expect(why).toHaveTextContent("200")
    expect(why).toHaveTextContent(/reconnect/i)
    expect(loft.queryByRole("heading", { name: /history/i })).not.toBeInTheDocument()
    expect(loft.queryByText(/history/i)).not.toBeInTheDocument()

    // And it asks for a recent slice, of this rack. Both bounds are the label's
    // own promise rather than the server's: `limit` is accepted anywhere in
    // 1..500, and "a glance at what just happened" lives in a much narrower
    // band than that.
    //
    //   * At most a quarter of the ring. 200 is everything a rack can hold, so
    //     asking for it is drawing the whole ring into a card and calling it
    //     recent -- which is the panel this label exists to not be.
    //   * At least a few rows. A flapping rack spends two events per reconnect,
    //     so a limit of one or two is a panel that shows a disconnect with the
    //     connect that explains it already pushed off the end.
    const asked = new URL(asks[0]).searchParams
    expect(asked.get("daemon_id")).toBe("17")
    const limit = Number(asked.get("limit"))
    expect(limit).toBeGreaterThanOrEqual(5)
    expect(limit).toBeLessThanOrEqual(50)
  })

  it("says a rotated rack is unpaired until the new token reaches it", async () => {
    const TOKEN = "tok-1d82fa640b39ce75"
    const rotated: number[] = []
    server.use(
      SIGNED_IN,
      listing([rack({ id: 17, name: "pi-loft" }), rack({ id: 42, name: "pi-cellar" })]),
      NO_EVENTS,
      http.post("/api/daemons/:daemon_id/rotate-key", ({ params }) => {
        rotated.push(Number(params.daemon_id))
        return HttpResponse.json({ id: 42, name: "pi-cellar", token: TOKEN })
      }),
    )
    renderApp({ at: "/daemons" })

    expect(await screen.findByRole("heading", { name: "pi-cellar" })).toBeInTheDocument()
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Rotate key" }))

    // Before it happens, because afterwards is too late to decide not to: a
    // rack that is offline right now cannot be given the new token remotely.
    const asked = within(await screen.findByRole("dialog"))
    expect(asked.getByText(/unpaired until the new token reaches it/i)).toBeInTheDocument()
    await userEvent.click(asked.getByRole("button", { name: "Rotate the key" }))

    expect(rotated).toEqual([42])
    const shown = within(await screen.findByRole("dialog"))
    // The same once-only display as pairing, from the other route that mints.
    expect(shown.getByText(TOKEN)).toBeInTheDocument()
    expect(
      shown.getByText(
        `ors-daemon connect --server ${window.location.origin} --token ${TOKEN}`,
      ),
    ).toBeInTheDocument()
    expect(shown.getByText(/only time it is shown/i)).toBeInTheDocument()

    await userEvent.click(shown.getByRole("button", { name: "Done" }))
    expect(document.body).not.toHaveTextContent(TOKEN)

    // Once, from this route too: opening it again asks the question again
    // rather than re-showing the answer.
    await userEvent.click(card("pi-cellar").getByRole("button", { name: "Rotate key" }))
    const reopened = within(await screen.findByRole("dialog"))
    expect(reopened.getByRole("button", { name: "Rotate the key" })).toBeInTheDocument()
    expect(reopened.queryByText(TOKEN)).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(TOKEN)
  })
})
