import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, Settings } from "../src/api/queries"
import { server } from "./msw"
import { renderApp } from "./render"

/**
 * The fixture, and why every value in it is the value it is.
 *
 *   * **Four racks, and no id is an index, a position or another rack.** 3, 11,
 *     26 and 47, in the `ORDER BY id` the route answers with, so index 0 holds
 *     id 3. The fourth test turns one id into one name, and an id that happened
 *     to equal its own index would let a page that named `racks[n]` pass.
 *   * **No rack name is a substring of another.** "the fourth is named and the
 *     three that got it are not" is only assertable if `toHaveTextContent` on
 *     one name cannot be satisfied by another.
 *   * **The night window is deliberately not 23:00/07:00.** Those are
 *     `NightWindow`'s own defaults, so a form seeded from the schema instead of
 *     from the server would be indistinguishable from a correct one. 22:45 and
 *     06:20 are also different from `screens.test.tsx`'s 22:00/08:00, so a page
 *     reading the wrong fixture is visible too.
 *   * **And it wraps midnight**, because that is the normal case: "dark between
 *     22:45 and 06:20" is a night, and `start > end` is what a night looks like.
 *   * **The two passwords are different strings and neither contains the
 *     other.** A card that sent the current password where the new one belongs
 *     is only catchable if no two of them are the same text.
 */
const LOFT = 3
const CELLAR = 11
const ATTIC = 26
const SHED = 47

const CURRENT_PASSWORD = "correct-horse-8"
const NEXT_PASSWORD = "eight-red-lanterns"

const SIGNED_IN = http.get("/api/auth/me", () =>
  HttpResponse.json({ authenticated: true, password_set: true }),
)

function rack(id: number, name: string): Daemon {
  return {
    id,
    name,
    online: true,
    status: "connected",
    config_version: 4,
    applied_version: 4,
    config_error: null,
    version: "0.3.1",
    capabilities: {},
    last_seen: null,
    paired_at: "2026-08-01T09:15:00Z",
    created_at: "2026-08-01T09:00:00Z",
  }
}

const RACKS = [
  rack(LOFT, "pi-loft"),
  rack(CELLAR, "pi-cellar"),
  rack(ATTIC, "pi-attic"),
  rack(SHED, "pi-shed"),
]

const SETTINGS: Settings = {
  timezone: "Europe/Amsterdam",
  night: { enabled: true, start: "22:45", end: "06:20" },
}

function reading(settings: () => Settings = () => SETTINGS) {
  return [
    SIGNED_IN,
    http.get("/api/daemons", () => HttpResponse.json(RACKS)),
    http.get("/api/settings", () => HttpResponse.json(settings())),
  ]
}

/**
 * Everything the console said, so a secret that reached a log is a failure.
 *
 * Task 15's, verbatim in intent: "never log the plaintext" is a rule about the
 * browser as much as about the server, and a page that put a password into a
 * `console.error` alongside a refusal would satisfy every assertion about the
 * DOM.
 */
function watchConsole(): string[] {
  const said: string[] = []
  for (const method of ["log", "info", "warn", "error", "debug"] as const) {
    const original = console[method].bind(console)
    vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
      said.push(args.map((arg) => String(arg)).join(" "))
      original(...args)
    })
  }
  return said
}

/**
 * A password is in none of the four places this page could leave one.
 *
 * `innerHTML` as well as `textContent` because React writes a controlled
 * input's value to the `value` attribute, and the property is read directly for
 * the case where it does not -- a form that kept the plaintext after the change
 * landed is the failure this is really looking for, and neither of the first
 * two would see it.
 */
function appearsNowhere(secret: string, said: string[]) {
  expect(document.body.innerHTML).not.toContain(secret)
  notEchoed(secret, said)
  for (const box of document.querySelectorAll<HTMLInputElement>("input")) {
    expect(box.value).not.toContain(secret)
  }
}

/**
 * A password is nowhere a person or a log could read it, while the box it was
 * typed into may still hold it.
 *
 * The weaker half of `appearsNowhere`, for the refusal case, and the difference
 * is deliberate: a form that emptied itself when the server said no would throw
 * away the new password somebody had just composed along with the typo in the
 * old one. So the input keeps its value -- which is also in the `value`
 * attribute, so `innerHTML` cannot be asked here -- and what must not happen is
 * a refusal that repeats it back in text or drops it into a console.
 */
function notEchoed(secret: string, said: string[]) {
  expect(document.body.textContent ?? "").not.toContain(secret)
  expect(said.join("\n")).not.toContain(secret)
}

afterEach(() => vi.restoreAllMocks())

describe("the settings page", () => {
  it("changes the password, and will not do it without the current one", async () => {
    const said = watchConsole()
    const bodies: unknown[] = []
    server.use(
      ...reading(),
      http.post("/api/auth/password", async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ok: true, other_sessions_ended: 2 })
      }),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Admin password" }))
    const button = card.getByRole("button", { name: "Change the password" })
    // Nothing to send while a field is empty. The current password is not a
    // formality: the server refuses without it, and a form that posted a body
    // it knows will be refused only teaches people to ignore the refusal.
    expect(button).toBeDisabled()

    await userEvent.type(card.getByLabelText("New password"), NEXT_PASSWORD)
    await userEvent.type(card.getByLabelText("Confirm the new password"), NEXT_PASSWORD)
    expect(button).toBeDisabled()

    await userEvent.type(card.getByLabelText("Current password"), CURRENT_PASSWORD)
    await userEvent.click(button)

    // The wire body, exactly: the current password and the new one, and the
    // confirmation is this form's own business rather than a third field the
    // server would refuse for.
    await waitFor(() =>
      expect(bodies).toEqual([{ current: CURRENT_PASSWORD, new: NEXT_PASSWORD }]),
    )

    // What it did to the other browsers, from the count the route answered
    // with rather than from prose that would say it on a server where nothing
    // else was signed in.
    expect(await screen.findByText(/2 other sign-ins were ended/i)).toBeInTheDocument()
    // And neither plaintext is anywhere afterwards. The form is emptied: what
    // it holds is a password, and it has been used.
    appearsNowhere(CURRENT_PASSWORD, said)
    appearsNowhere(NEXT_PASSWORD, said)

    // The last write's answer is not an answer about a form somebody has since
    // started filling in again -- "2 other sign-ins were ended" sitting under a
    // half-typed second change reads as a report on the change being typed.
    await userEvent.type(card.getByLabelText("Current password"), "a")
    expect(screen.queryByText(/sign-ins were ended/i)).not.toBeInTheDocument()
  })

  it("says which password was wrong without signing the admin out", async () => {
    // 403, not 401. The interface sends every 401 to /login except the login
    // route's own, so a wrong *current* password answered 401 would throw the
    // admin out of this form -- taking the new password they had just typed
    // with it -- for a typo.
    const said = watchConsole()
    server.use(
      ...reading(),
      http.post("/api/auth/password", () =>
        HttpResponse.json({ detail: "the current password is wrong" }, { status: 403 }),
      ),
    )
    const { pushes, path } = renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Admin password" }))
    await userEvent.type(card.getByLabelText("Current password"), "not-the-password")
    await userEvent.type(card.getByLabelText("New password"), NEXT_PASSWORD)
    await userEvent.type(card.getByLabelText("Confirm the new password"), NEXT_PASSWORD)
    await userEvent.click(card.getByRole("button", { name: "Change the password" }))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent("the current password is wrong")
    expect(refusal).not.toHaveTextContent(/the server refused the change/i)
    expect(path()).toBe("/settings")
    expect(pushes()).toBe(0)
    // Nothing was changed, so nothing may claim it was.
    expect(screen.queryByText(/sign-ins were ended/i)).not.toBeInTheDocument()
    // The refusal names neither password and nothing was logged -- and the form
    // still holds what was typed, because emptying it would throw away the new
    // password somebody had just composed along with the typo in the old one.
    notEchoed("not-the-password", said)
    notEchoed(NEXT_PASSWORD, said)
    expect(card.getByLabelText("New password")).toHaveValue(NEXT_PASSWORD)
    expect(card.getByLabelText("Confirm the new password")).toHaveValue(NEXT_PASSWORD)
  })

  it("will not send a new password that was typed differently twice", async () => {
    // There is no password reset route on this server -- `setup` 409s once a
    // password is set -- so a mistyped new password is a rack nobody can sign
    // in to again. That is what the confirmation is for, and it must stop the
    // request rather than describe the problem afterwards.
    const bodies: unknown[] = []
    server.use(
      ...reading(),
      http.post("/api/auth/password", async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ok: true, other_sessions_ended: 0 })
      }),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Admin password" }))
    await userEvent.type(card.getByLabelText("Current password"), CURRENT_PASSWORD)
    await userEvent.type(card.getByLabelText("New password"), NEXT_PASSWORD)
    // Nothing to compare yet. A confirmation box that has not been typed into
    // is not a mismatch, and a warning that appears on the first keystroke of
    // every password is one people learn to read past.
    expect(card.queryByText(/typed differently twice/i)).not.toBeInTheDocument()

    await userEvent.type(card.getByLabelText("Confirm the new password"), "eight-red-lantern")

    expect(card.getByText(/typed differently twice/i)).toBeInTheDocument()
    const button = card.getByRole("button", { name: "Change the password" })
    expect(button).toBeDisabled()
    await userEvent.click(button)

    expect(bodies).toEqual([])
  })

  it("tells a rate-limited change from a wrong password", async () => {
    // The server refuses with 429 **before** it verifies anything, so reporting
    // it as a wrong password would send somebody off to remember a password
    // that was right all along.
    server.use(
      ...reading(),
      http.post("/api/auth/password", () =>
        HttpResponse.json({ detail: "too many attempts" }, { status: 429 }),
      ),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Admin password" }))
    await userEvent.type(card.getByLabelText("Current password"), CURRENT_PASSWORD)
    await userEvent.type(card.getByLabelText("New password"), NEXT_PASSWORD)
    await userEvent.type(card.getByLabelText("Confirm the new password"), NEXT_PASSWORD)
    await userEvent.click(card.getByRole("button", { name: "Change the password" }))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent(/too many attempts/i)
    expect(refusal).toHaveTextContent(/minute/i)
    expect(refusal).not.toHaveTextContent(/wrong/i)
  })

  it("sends the timezone on its own, and says what to do about one that is refused", async () => {
    const bodies: unknown[] = []
    let stored = SETTINGS
    // What this stand-in server can resolve, as `SettingsBody._resolvable`
    // decides on the real one: anything else is refused, including a name that
    // is only unresolvable because of a space somebody pasted with it.
    const RESOLVABLE = new Set(["Europe/Amsterdam", "Pacific/Auckland", "Pacific/Chatham"])
    server.use(
      ...reading(() => stored),
      http.patch("/api/settings", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        if (!RESOLVABLE.has(String(body.timezone))) {
          // The server's own 422, list-shaped, as `SettingsBody._resolvable`
          // raises it and `validation_error_without_the_body` strips it.
          return HttpResponse.json(
            {
              detail: [
                {
                  loc: ["body", "timezone"],
                  msg: `Value error, no such timezone: 'No time zone found with key ${String(body.timezone)}'`,
                  type: "value_error",
                },
              ],
            },
            { status: 422 },
          )
        }
        // The write lands -- and between this answer and the refetch, another
        // tab moves the zone again. So what comes back from `GET /api/settings`
        // is **not** what this tab sent, and a box that went on showing what
        // was typed still reads Pacific/Auckland.
        stored = { ...stored, timezone: "Pacific/Chatham" }
        return HttpResponse.json({ ...stored, timezone: String(body.timezone) })
      }),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Timezone" }))
    const box = card.getByLabelText("Timezone")
    expect(box).toHaveValue("Europe/Amsterdam")
    // The zone the server already holds is not an edit.
    expect(card.getByRole("button", { name: "Save the timezone" })).toBeDisabled()

    await userEvent.clear(box)
    // An empty box is not an edit either: `timezone` is `min_length=1` on the
    // far end, and a blank one would be refused for a reason that says nothing.
    expect(card.getByRole("button", { name: "Save the timezone" })).toBeDisabled()

    await userEvent.type(box, "Mars/Olympus_Mons")
    await userEvent.click(card.getByRole("button", { name: "Save the timezone" }))

    // The server's own sentence, and then what a person can do about it. The
    // check is against *this* host's tzdata, so "the Pi may not know it either"
    // is the other half of what they need to hear.
    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent(/no such timezone/i)
    expect(refusal).toHaveTextContent(/IANA/i)
    expect(refusal).toHaveTextContent("Europe/Amsterdam")

    // A zone with a space in front of it, which is what a paste out of a wiki
    // gives you. Nothing here trims -- the value is the server's to judge -- so
    // what has to be true is that the refusal shows the space. `no such
    // timezone: ' Europe/Amsterdam'` is a refusal nobody can act on: the name
    // reads as correct and the character that broke it is invisible.
    await userEvent.clear(box)
    await userEvent.type(box, " Europe/Amsterdam")
    await userEvent.click(card.getByRole("button", { name: "Save the timezone" }))

    await waitFor(() => expect(bodies).toHaveLength(2))
    // Sent exactly as typed, space and all.
    expect(bodies[1]).toEqual({ timezone: " Europe/Amsterdam" })
    // And drawn back inside quotes, where the space is something to see.
    // `textContent` rather than `toHaveTextContent`, which normalises runs of
    // whitespace away -- and the space is the whole assertion.
    const spaced = await screen.findByRole("alert")
    expect(spaced.textContent ?? "").toContain('" Europe/Amsterdam"')

    await userEvent.clear(box)
    await userEvent.type(box, "Pacific/Auckland")
    await userEvent.click(card.getByRole("button", { name: "Save the timezone" }))

    // Only the field that moved. `night` in a timezone edit would write this
    // page's idea of the night window over whatever another tab had just set,
    // and `PATCH /api/settings` takes its PATCH semantics from `exclude_unset`
    // precisely so it need not be sent.
    await waitFor(() =>
      expect(bodies).toEqual([
        { timezone: "Mars/Olympus_Mons" },
        { timezone: " Europe/Amsterdam" },
        { timezone: "Pacific/Auckland" },
      ]),
    )
    // And what is drawn afterwards came back from the server, not from the box.
    await waitFor(() => expect(card.getByLabelText("Timezone")).toHaveValue("Pacific/Chatham"))
    expect(screen.getByText("The timezone was saved.")).toBeInTheDocument()

    // The last write's notice is not an answer about a form that has moved on
    // from it: "The timezone was saved." under a box somebody is retyping reads
    // as a report on what is in the box now.
    await userEvent.type(card.getByLabelText("Timezone"), "x")
    expect(screen.queryByText("The timezone was saved.")).not.toBeInTheDocument()
  })

  it("sends the night window on its own, and draws a window that wraps midnight as normal", async () => {
    const bodies: unknown[] = []
    let stored = SETTINGS
    server.use(
      ...reading(() => stored),
      http.patch("/api/settings", async ({ request }) => {
        const body = (await request.json()) as { night: Settings["night"] }
        bodies.push(body)
        // The write lands -- and another tab moves the window again before this
        // one refetches, so what comes back is not what was sent. A form that
        // kept what was typed goes on showing 21:15 for a server that holds
        // 23:30, which is the defect `SleepTab` was fixed for one page over.
        stored = { ...stored, night: { enabled: true, start: "23:30", end: "05:05" } }
        return HttpResponse.json({ ...stored, night: body.night })
      }),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Night window" }))
    // Seeded from the server and not from `NightWindow`'s defaults, which are
    // 23:00 and 07:00 -- a form seeded from those would move every rack's night
    // by a quarter of an hour the first time anybody saved anything.
    expect(card.getByLabelText("Dark from")).toHaveValue("22:45")
    expect(card.getByLabelText("Light again at")).toHaveValue("06:20")
    expect(card.getByRole("button", { name: "Save the night window" })).toBeDisabled()

    // 22:45 is after 06:20, so this window crosses midnight -- which is what a
    // night is. It must be drawn as an ordinary window: no alert, nothing
    // refused, and the sentence in the order the two times were given.
    expect(card.getByText(/Dark between 22:45 and 06:20, Europe\/Amsterdam time/)).toBeInTheDocument()
    // Said in words, because two times in that order look like a mistake to
    // anybody who has not been told they are not one.
    expect(card.getByText(/crosses midnight, which is the usual way round/)).toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()

    // And *not* said when nothing sleeps. "Never sleeps. A start later than the
    // end means the window crosses midnight" is a rule about two boxes nothing
    // is reading -- prose that says nothing in the state it appears in, which is
    // how people learn to stop reading the prose that does.
    const sleeps = card.getByLabelText("Sleeps at all")
    await userEvent.click(sleeps)
    expect(card.getByText(/Never sleeps\./)).toBeInTheDocument()
    expect(card.queryByText(/crosses midnight/)).not.toBeInTheDocument()
    await userEvent.click(sleeps)
    expect(card.getByText(/crosses midnight, which is the usual way round/)).toBeInTheDocument()

    const from = card.getByLabelText("Dark from")
    await userEvent.clear(from)
    await userEvent.type(from, "21:15")
    expect(card.getByText(/Dark between 21:15 and 06:20/)).toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()

    await userEvent.click(card.getByRole("button", { name: "Save the night window" }))

    // The window as it was typed: still wrapping, and not swapped into
    // 06:20-21:15, which is the daylight and would leave four panels lit all
    // night. Interpreting the wrap is the daemon's job.
    await waitFor(() =>
      expect(bodies).toEqual([{ night: { enabled: true, start: "21:15", end: "06:20" } }]),
    )
    // And afterwards the boxes hold what the server holds, not what was typed.
    await waitFor(() => expect(card.getByLabelText("Dark from")).toHaveValue("23:30"))
    expect(card.getByLabelText("Light again at")).toHaveValue("05:05")
    expect(screen.getByText("The night window was saved.")).toBeInTheDocument()

    // The last write's notice is not an answer about a form that has moved on
    // from it: "The night window was saved." under a window somebody is in the
    // middle of retyping reads as a report on the one in front of them.
    await userEvent.clear(card.getByLabelText("Light again at"))
    await userEvent.type(card.getByLabelText("Light again at"), "04:40")
    expect(screen.queryByText("The night window was saved.")).not.toBeInTheDocument()
  })

  it("names the one rack of four that did not get the change, and not the three that did", async () => {
    // `PATCH /api/settings` is rack-wide: it never calls `affects`, so `Change`
    // reads the affected set as every rack there is and the response can never
    // be narrowed to 202. A change that reached three racks of four therefore
    // answers a plain 200, and `X-Unservable-Daemons` is the only thing in it
    // that names the fourth. There is no body field and no status code for it.
    let stored = SETTINGS
    server.use(
      ...reading(() => stored),
      http.patch("/api/settings", async ({ request }) => {
        const body = (await request.json()) as { timezone: string }
        stored = { ...stored, timezone: body.timezone }
        return HttpResponse.json(stored, { headers: { "X-Unservable-Daemons": String(ATTIC) } })
      }),
    )
    renderApp({ at: "/settings" })

    const card = within(await screen.findByRole("region", { name: "Timezone" }))
    const box = card.getByLabelText("Timezone")
    await userEvent.clear(box)
    await userEvent.type(box, "Pacific/Auckland")
    await userEvent.click(card.getByRole("button", { name: "Save the timezone" }))

    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-attic")
    for (const name of ["pi-loft", "pi-cellar", "pi-shed"]) {
      expect(notice).not.toHaveTextContent(name)
    }

    // And it goes when the form moves on. This is the notice it matters most
    // for: "Not every rack was given that change / pi-attic" left standing over
    // a zone somebody is now retyping names a rack about an edit that has not
    // been made yet, and the next thing they do is go looking at pi-attic.
    await userEvent.type(box, "x")
    expect(screen.queryByText(/was saved, but nothing was sent/i)).not.toBeInTheDocument()
  })

  it("draws no form until the settings have been read", async () => {
    // The trap this milestone has fallen into twice: a page that treats a fetch
    // in flight as an answer. A night window drawn from `NightWindow`'s
    // defaults while `GET /api/settings` is still out is a form that reads as
    // "this rack sleeps 23:00 to 07:00" -- and one Save away from making that
    // true on four panels that were set to something else.
    let release: (response: Response) => void = () => {}
    const held = new Promise<Response>((resolve) => {
      release = resolve
    })
    server.use(
      SIGNED_IN,
      // The racks fail outright, which is the same rule on the other fetch: an
      // unread listing must not be drawn as an empty one either, and this page
      // needs the racks to turn an unservable id into a name.
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "the daemon table could not be read" }, { status: 500 }),
      ),
      http.get("/api/settings", () => held),
    )
    renderApp({ at: "/settings" })

    // The password card needs nothing from that request and is up already.
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument()
    await screen.findByRole("region", { name: "Admin password" })
    expect(screen.getByText(/Reading the settings/)).toBeInTheDocument()
    expect(screen.queryByLabelText("Dark from")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Timezone")).not.toBeInTheDocument()
    expect(await screen.findByText("The racks could not be read")).toBeInTheDocument()
    expect(screen.getByText("the daemon table could not be read")).toBeInTheDocument()

    release(HttpResponse.json({ detail: "the setting table could not be read" }, { status: 500 }))

    // A fetch that failed is a fetch that failed, permanently: it says so, and
    // no form has quietly appeared holding values nobody read.
    expect(await screen.findByText("The settings could not be read")).toBeInTheDocument()
    expect(screen.getByText("the setting table could not be read")).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText(/Reading the settings/)).not.toBeInTheDocument(),
    )
    expect(screen.queryByLabelText("Dark from")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Timezone")).not.toBeInTheDocument()
  })
})
