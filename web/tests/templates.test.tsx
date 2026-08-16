import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { describe, expect, it } from "vitest"

import type { Daemon, Screen as ScreenRow, Template } from "../src/api/queries"
import { server } from "./msw"
import { renderApp } from "./render"

/**
 * The fixture, and why every number in it is the number it is.
 *
 * This page routes on **names** -- a screen's `template` column is a name, not a
 * foreign key -- and it draws a list the server ordered by name while the rows
 * carry ids. So the identity trap this project has been bitten by three times
 * has a specific shape here, and every one of these is a defence against it:
 *
 *   * **Template ids descend through the server's order.** `GET /api/templates`
 *     is `ORDER BY name`, so the list arrives aurora-clock (21), big-number (7),
 *     ring-gauge (3). A page that sorted by id would draw them backwards, and a
 *     page that sorted by name itself would be indistinguishable from one that
 *     took the server's order -- which is why the ids are what disagree.
 *   * **No template name is a number and no name is a substring of another.**
 *     An assign that sent `{template: "3"}` instead of `{template: "ring-gauge"}`
 *     is a body a test can catch only if the two are never the same string, and
 *     `not.toHaveTextContent` on one name cannot be satisfied by another's
 *     absence.
 *   * **No screen id is its own position and none is its own index.** Ids 11, 12
 *     and 13 sit at positions 3, 1 and 2, and the server answers `ORDER BY
 *     position, id`, so the list arrives 12, 13, 11. The two panels that draw
 *     ring-gauge are therefore 13 *then* 11: previewing "the first panel that
 *     names it" picks 13, and a lowest-id rule picks 11.
 *   * **The rack a screen is on is not the rack the unservable header names.**
 *     Every panel edited below is on rack 8; the header names 42 and 63. No
 *     answer the server really gives separates "the rack this row belongs to"
 *     from "the racks that did not get this edit", so only a fixture where they
 *     differ can tell the header being read from the body being guessed. 63 is
 *     in no listing -- a rack another tab deleted between the fetch and the
 *     write -- and is still a rack somebody has to go and look at.
 *   * **Scene counts differ** (3, 1, 2) and are not the ids, so "3 scenes" on
 *     the wrong card is visible.
 */
const LOFT = 8
const CELLAR = 42
const UNLISTED_RACK = 63

const SIGNED_IN = http.get("/api/auth/me", () =>
  HttpResponse.json({ authenticated: true, password_set: true }),
)

function rack(id: number, name: string): Daemon {
  return {
    id,
    name,
    online: true,
    status: "connected",
    config_version: 9,
    applied_version: 9,
    config_error: null,
    version: "0.3.1",
    capabilities: {},
    last_seen: null,
    paired_at: "2026-08-01T09:15:00Z",
    created_at: "2026-08-01T09:00:00Z",
  }
}

const RACKS = [rack(LOFT, "pi-loft"), rack(CELLAR, "pi-cellar")]

/** A scene, as `TemplateView.scenes` carries them: `list[dict[str, Any]]`. */
const scene = (id: string) => ({ id, when: null, elements: [] })

/** The editor's own row: the only one in this fixture that may be deleted. */
const AURORA: Template = {
  id: 21,
  name: "aurora-clock",
  category: "clock",
  builtin: false,
  scenes: [scene("day"), scene("dusk"), scene("night")],
  params_schema: { tint: { type: "color", label: "Tint", default: "#3366ff" } },
}

const BIG_NUMBER: Template = {
  id: 7,
  name: "big-number",
  category: "gauge",
  builtin: true,
  scenes: [scene("main")],
  params_schema: { big: { type: "binding", label: "Centre text", default: "0" } },
}

const RING_GAUGE: Template = {
  id: 3,
  name: "ring-gauge",
  category: "gauge",
  builtin: true,
  scenes: [scene("main"), scene("stale")],
  params_schema: { title: { type: "string", label: "Title", default: "CPU" } },
}

/** As `GET /api/templates` answers them: `ORDER BY name`, so 21, 7, 3. */
const TEMPLATES = [AURORA, BIG_NUMBER, RING_GAUGE]

function panel(over: Partial<ScreenRow> & Pick<ScreenRow, "id" | "name" | "position">): ScreenRow {
  return {
    daemon_id: LOFT,
    display: { backend: "gc9a01", spi_bus: 0, spi_cs: 0, dc: 25, rst: 27, hz: 40_000_000 },
    rotation: 270,
    hflip: false,
    enabled: true,
    template: "big-number",
    params: {},
    sleep_override: null,
    ...over,
  }
}

const WEATHER = panel({ id: 12, name: "Weather", position: 1 })
// Bolted in at 270 **and** flipped, so a page that applied the mount correction
// has two things to apply and the preview test below has both to catch.
const TRAINS = panel({ id: 13, name: "Trains", position: 2, template: "ring-gauge", hflip: true })
// On the other rack, so "which rack is this panel on" is a question the card has
// to answer from the row rather than from the page.
const KITCHEN = panel({
  id: 11,
  name: "Kitchen",
  position: 3,
  daemon_id: CELLAR,
  template: "ring-gauge",
})

/**
 * A panel naming a template that is not in the list, whose name **extends** one
 * that is.
 *
 * Two real things at once. A screen's `template` is a free string with no
 * foreign key, so a template deleted in another tab leaves exactly this row
 * behind -- `build_snapshot` is what refuses it, and the rack says so through
 * `config_error` on the Daemons page. And "ring-gauge-xl" starts with
 * "ring-gauge", which is the one shape a join by name can get wrong quietly:
 * matched loosely, this panel is listed under ring-gauge, previewed as
 * ring-gauge, and offered a Detach in a card it has nothing to do with.
 */
const SUNROOM = panel({ id: 34, name: "Sunroom", position: 4, template: "ring-gauge-xl" })

/**
 * A panel on a rack this page's listing does not have.
 *
 * `GET /api/daemons` and `GET /api/screens` are two fetches, and a rack deleted
 * in another tab between them leaves its panels in the second and not the first.
 * Drawing "Attic on " would quietly turn a rack somebody has to go and look at
 * into a blank.
 */
const ATTIC = panel({ id: 44, name: "Attic", position: 5, daemon_id: UNLISTED_RACK })

/** As `GET /api/screens` answers them: `ORDER BY position, id`, so 12, 13, 11. */
const SCREENS = [WEATHER, TRAINS, KITCHEN]

/** Every route this page reads on mount. Read through getters so a write can move them. */
function reading(templates: () => Template[], screens: () => ScreenRow[]) {
  return [
    SIGNED_IN,
    http.get("/api/daemons", () => HttpResponse.json(RACKS)),
    http.get("/api/templates", () => HttpResponse.json(templates())),
    http.get("/api/screens", () => HttpResponse.json(screens())),
  ]
}

/** One template's card, found by the region's accessible name. */
const cardOf = (name: string) => within(screen.getByRole("region", { name }))

describe("the templates page", () => {
  it("lists the templates in the server's order, and says which are built in", async () => {
    server.use(...reading(() => TEMPLATES, () => [...SCREENS, SUNROOM, ATTIC]))
    renderApp({ at: "/templates" })

    expect(await screen.findByRole("heading", { name: "Templates" })).toBeInTheDocument()
    await screen.findByRole("region", { name: "ring-gauge" })

    // `ORDER BY name`, taken as given. The ids descend through that order
    // (21, 7, 3), so a client sorting by id draws this list backwards.
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["aurora-clock", "big-number", "ring-gauge"])

    // A built-in is distinguishable from an editor's row, and the difference is
    // said in words rather than in an icon.
    expect(cardOf("ring-gauge").getByText("Built-in · gauge · 2 scenes")).toBeInTheDocument()
    // Singular for one, and a count that is not the id: template 7 has 1 scene.
    expect(cardOf("big-number").getByText("Built-in · gauge · 1 scene")).toBeInTheDocument()
    expect(cardOf("aurora-clock").getByText("Yours · clock · 3 scenes")).toBeInTheDocument()

    // Which panels draw each one, and on which rack -- from the screen list,
    // matched by name, because that is the only thing joining the two tables.
    expect(cardOf("ring-gauge").getByText("Trains on pi-loft")).toBeInTheDocument()
    expect(cardOf("ring-gauge").getByText("Kitchen on pi-cellar")).toBeInTheDocument()
    expect(cardOf("big-number").getByText("Weather on pi-loft")).toBeInTheDocument()
    // A rack with no row is named as a rack, not drawn as nothing.
    expect(cardOf("big-number").getByText("Attic on rack 63")).toBeInTheDocument()
    expect(cardOf("big-number").queryByText("Trains on pi-loft")).not.toBeInTheDocument()
    // And the panel naming a template this server does not have is listed under
    // none of them -- not even the one whose name its own is built out of.
    expect(cardOf("ring-gauge").queryByText("Sunroom on pi-loft")).not.toBeInTheDocument()
    expect(screen.queryByText("Sunroom on pi-loft")).not.toBeInTheDocument()
  })

  it("previews a template through a panel that draws it, with nothing turning the picture", async () => {
    // Spec §5.4, for the third time in this interface: `rotation` and `hflip`
    // describe how a panel is bolted into the rack, the render is made *before*
    // the mount correction -- `screens._render` says so in as many words -- and
    // the browser therefore already shows what a person standing at the rack
    // sees. Rotating again would be wrong twice.
    //
    // Asserted as the **absence of a transform**, the way task 6 and task 8 do,
    // rather than as the presence of an image: an `<img>` that is there is not
    // evidence of anything, and the failure this is about is a wrapper that
    // turns it.
    server.use(...reading(() => TEMPLATES, () => SCREENS))
    renderApp({ at: "/templates" })

    const region = await screen.findByRole("region", { name: "ring-gauge" })
    const picture = within(region).getByRole("img", { name: /ring-gauge/ })

    // The **screen** route, because that is the only route that renders: a
    // template has no preview of its own. Screen 13 and not 11 -- the first
    // panel that names this template in the server's own `position, id` order,
    // which is neither the lowest id nor the last row.
    expect(picture.getAttribute("src")).toBe("/api/screens/13/preview")
    expect(within(region).getByText(/^Rendered for Trains on pi-loft/)).toBeInTheDocument()

    // Trains is at 270 and flipped. Every element from the picture up to the
    // card it sits in, in both of the ways a transform can be written here.
    for (let node: HTMLElement | null = picture; node !== null; node = node.parentElement) {
      expect(node.style.transform).toBe("")
      expect(node.getAttribute("style") ?? "").not.toMatch(/transform|rotate|scale|matrix/)
      // And the class-shaped way of saying the same thing, which no stylesheet
      // is loaded to reveal: `rotate-90`, `-scale-x-100`.
      expect(node.className).not.toMatch(/rotate|scale/)
      if (node === region) break
    }
    // Nor the attribute that would do it without any style at all.
    expect(picture).not.toHaveAttribute("transform")

    // And a template no panel draws has nothing to render *against*, which the
    // card says rather than drawing a broken image at a route that would 404.
    expect(cardOf("aurora-clock").queryByRole("img")).not.toBeInTheDocument()
    expect(cardOf("aurora-clock").getByText(/no panel draws it/i)).toBeInTheDocument()
  })

  it("assigns a panel by name, and draws what the server answered", async () => {
    const patched: unknown[] = []
    let screens = SCREENS
    server.use(
      ...reading(() => TEMPLATES, () => screens),
      http.patch("/api/screens/:screen_id", async ({ request, params }) => {
        patched.push({ id: params.screen_id, body: await request.json() })
        // The write lands -- and between this answer and the refetch, another
        // tab moves Trains off ring-gauge. So the list the server hands back is
        // **not** the list this tab would have guessed, and an interface that
        // patched its own cache instead of re-asking still shows Trains under
        // ring-gauge.
        const assigned = { ...WEATHER, template: "aurora-clock" }
        screens = [assigned, { ...TRAINS, template: "big-number" }, KITCHEN]
        return HttpResponse.json(assigned, {
          headers: { "X-Unservable-Daemons": `${CELLAR},${UNLISTED_RACK}` },
        })
      }),
    )
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "aurora-clock" })

    // A panel that already draws a template is not offered it again. Not a
    // nicety: the PATCH would be accepted, and an edit that changes nothing
    // still bumps the rack's config_version and pushes a fresh snapshot.
    await userEvent.click(screen.getByRole("button", { name: "Assign a panel to ring-gauge" }))
    const already = within(await screen.findByRole("dialog"))
    await userEvent.click(already.getByRole("combobox", { name: "Panel" }))
    expect(
      within(screen.getByRole("listbox"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Weather on pi-loft"])
    await userEvent.keyboard("{Escape}")
    await userEvent.click(already.getByRole("button", { name: "Cancel" }))
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())

    await userEvent.click(screen.getByRole("button", { name: "Assign a panel to aurora-clock" }))

    const dialog = within(await screen.findByRole("dialog"))
    // Nothing is chosen yet, so there is nothing to assign. An empty confirm
    // would PATCH nothing, or worse, the first panel in the list.
    expect(dialog.getByRole("button", { name: "Assign the panel" })).toBeDisabled()
    await userEvent.click(dialog.getByRole("combobox", { name: "Panel" }))
    await userEvent.click(screen.getByRole("option", { name: "Weather on pi-loft" }))
    await userEvent.click(dialog.getByRole("button", { name: "Assign the panel" }))

    // The screen's `template` column is a **name**, and the body carries that
    // and nothing else: `ScreenBody` is `extra="forbid"`, and a body repeating
    // every field this page happens to hold would overwrite whatever another
    // tab changed in the meantime.
    await waitFor(() => expect(patched).toEqual([{ id: "12", body: { template: "aurora-clock" } }]))

    // The racks that did not get it, from the header and from nothing else.
    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-cellar")
    expect(notice).toHaveTextContent("daemon 63")
    // Not the rack the edited panel is on, which is what a page reading the
    // body instead of the header would say.
    expect(notice).not.toHaveTextContent("pi-loft")

    // And what is drawn afterwards is the server's list, not this tab's guess.
    await waitFor(() =>
      expect(cardOf("aurora-clock").getByText("Weather on pi-loft")).toBeInTheDocument(),
    )
    expect(cardOf("big-number").queryByText("Weather on pi-loft")).not.toBeInTheDocument()
    // The other tab's edit, which only a refetch could have shown.
    expect(cardOf("big-number").getByText("Trains on pi-loft")).toBeInTheDocument()
    expect(cardOf("ring-gauge").queryByText("Trains on pi-loft")).not.toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()

    // Setting up the next write clears the last one's answer. "Not every rack
    // was given that change" names racks against an edit that has happened, and
    // left standing over the dialog for the next one it reads as an answer about
    // that one instead.
    await userEvent.click(screen.getByRole("button", { name: "Assign a panel to aurora-clock" }))
    await screen.findByRole("dialog")
    expect(screen.queryByText(/was saved, but nothing was sent/i)).not.toBeInTheDocument()
  })

  it("detaches a panel by pointing it at another template", async () => {
    // What "detach" can honestly mean here. `screen.template` is a name, it is
    // NOT NULL, and it has no foreign key -- a panel must name something, and
    // there is no value the server understands as "nothing". So detaching from
    // this template is moving the panel to another one, and the interface says
    // exactly that rather than inventing an empty template to park it on.
    const patched: unknown[] = []
    let screens = SCREENS
    server.use(
      ...reading(() => TEMPLATES, () => screens),
      http.patch("/api/screens/:screen_id", async ({ request, params }) => {
        patched.push({ id: params.screen_id, body: await request.json() })
        const moved = { ...KITCHEN, template: "aurora-clock" }
        screens = [WEATHER, TRAINS, moved]
        return HttpResponse.json(moved)
      }),
    )
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "ring-gauge" })
    await userEvent.click(screen.getByRole("button", { name: "Detach Kitchen from ring-gauge" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByText(/must always name a template/i)).toBeInTheDocument()
    expect(dialog.getByRole("button", { name: "Detach the panel" })).toBeDisabled()

    await userEvent.click(dialog.getByRole("combobox", { name: "Draws instead" }))
    // The template it is being detached from is not one of the answers.
    expect(
      within(screen.getByRole("listbox"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["aurora-clock", "big-number"])
    await userEvent.click(screen.getByRole("option", { name: "aurora-clock" }))
    await userEvent.click(dialog.getByRole("button", { name: "Detach the panel" }))

    await waitFor(() => expect(patched).toEqual([{ id: "11", body: { template: "aurora-clock" } }]))
    // What the write is reported as comes out of what the server answered, not
    // out of what was sent -- and this response carried no unservable header,
    // which is not the same as an empty one and must not be drawn as a failure.
    expect(await screen.findByText("Kitchen now draws aurora-clock.")).toBeInTheDocument()
    expect(screen.queryByText(/nothing was sent/i)).not.toBeInTheDocument()
    await waitFor(() =>
      expect(cardOf("aurora-clock").getByText("Kitchen on pi-cellar")).toBeInTheDocument(),
    )
    expect(cardOf("ring-gauge").queryByText("Kitchen on pi-cellar")).not.toBeInTheDocument()
    // The other panel on this template is untouched: one PATCH, naming one row.
    expect(cardOf("ring-gauge").getByText("Trains on pi-loft")).toBeInTheDocument()

    // And setting up the next detach clears what the last one said, for the
    // same reason the assign does: it is an answer about a write that has
    // already happened.
    await userEvent.click(screen.getByRole("button", { name: "Detach Trains from ring-gauge" }))
    await screen.findByRole("dialog")
    expect(screen.queryByText("Kitchen now draws aurora-clock.")).not.toBeInTheDocument()
  })

  it("does not offer to delete a built-in, and deletes the editor's row it named", async () => {
    // The server refuses a built-in with 409 and says why. A button that always
    // fails is the "button that lies" this project already refused once, so the
    // control is **absent** -- and the reason is on the card, because a control
    // that is simply missing teaches nobody anything.
    const deleted: string[] = []
    let templates = TEMPLATES
    server.use(
      ...reading(() => templates, () => SCREENS),
      http.delete("/api/templates/:template_id", ({ params }) => {
        deleted.push(String(params.template_id))
        templates = templates.filter((each) => String(each.id) !== String(params.template_id))
        // The default 200 carrying the id it removed. No M3a route answers 204.
        return HttpResponse.json({ deleted: Number(params.template_id) })
      }),
    )
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "ring-gauge" })

    for (const builtin of ["ring-gauge", "big-number"]) {
      expect(cardOf(builtin).queryByRole("button", { name: `Delete ${builtin}` })).not.toBeInTheDocument()
      expect(cardOf(builtin).getByText(/re-seeded at every start/i)).toBeInTheDocument()
      // What cannot be deleted can still be amended, and the card shows that
      // difference rather than leaving a built-in looking frozen.
      expect(cardOf(builtin).getByRole("button", { name: `Amend ${builtin}` })).toBeInTheDocument()
    }
    expect(cardOf("aurora-clock").queryByText(/re-seeded at every start/i)).not.toBeInTheDocument()

    await userEvent.click(
      cardOf("aurora-clock").getByRole("button", { name: "Delete aurora-clock" }),
    )
    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByRole("heading", { name: "Delete aurora-clock?" })).toBeInTheDocument()
    await userEvent.click(dialog.getByRole("button", { name: "Delete the template" }))

    // The id that went on the wire is the template the confirmation named --
    // not 3 or 7, which are the built-ins, and not an index.
    await waitFor(() => expect(deleted).toEqual(["21"]))
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "aurora-clock" })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole("region", { name: "ring-gauge" })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    // And what the delete answered outlived the card it removed. Said above the
    // list precisely because the row it is about is gone: a notice mounted on
    // that card -- including the racks a rack-wide delete could not be given to
    // -- would be unmounted by the refetch before anybody could read it.
    expect(screen.getByText("aurora-clock was deleted.")).toBeInTheDocument()
  })

  it("amends a built-in with the field that changed, and names the racks that missed it", async () => {
    // `PATCH /api/templates/{id}` amends built-in rows too --
    // `seed_builtin_templates` inserts `ON CONFLICT DO NOTHING` precisely so
    // re-seeding cannot revert an edit -- so there is nothing to refuse here.
    //
    // And this is the edit that is **rack-wide**: the template routes never call
    // `affects`, so `Change` reads the affected set as every rack there is, and
    // they can never be narrowed to 202. The header is the only thing that names
    // a rack that missed one.
    const bodies: unknown[] = []
    let templates = TEMPLATES
    server.use(
      ...reading(() => templates, () => SCREENS),
      http.patch("/api/templates/:template_id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push({ id: params.template_id, body })
        const amended = { ...RING_GAUGE, ...body }
        templates = templates.map((each) => (each.id === RING_GAUGE.id ? amended : each))
        return HttpResponse.json(amended, {
          headers: { "X-Unservable-Daemons": `${CELLAR},${UNLISTED_RACK}` },
        })
      }),
    )
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "ring-gauge" })
    await userEvent.click(screen.getByRole("button", { name: "Amend ring-gauge" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByRole("textbox", { name: "Name" })).toHaveValue("ring-gauge")
    expect(dialog.getByRole("textbox", { name: "Category" })).toHaveValue("gauge")
    // Nothing has changed, so there is nothing to save. Not a nicety: an empty
    // PATCH is accepted, and this route affects **every** rack -- it would bump
    // every config_version there is and push a snapshot to each of them for an
    // edit nobody made.
    expect(dialog.getByRole("button", { name: "Save the template" })).toBeDisabled()
    // The thing a rename really does to a built-in, said before it is done.
    expect(dialog.getByText(/re-seeded as a fresh built-in/i)).toBeInTheDocument()

    await userEvent.clear(dialog.getByRole("textbox", { name: "Category" }))
    await userEvent.type(dialog.getByRole("textbox", { name: "Category" }), "instruments")
    await userEvent.click(dialog.getByRole("button", { name: "Save the template" }))

    // Only the field that moved. `TemplateBody` is `extra="forbid"`, so a body
    // echoing the whole `TemplateView` back is a 422 on `id` and `builtin`
    // alone -- and `scenes` in a body this page cannot edit would write back
    // whatever it last read.
    await waitFor(() =>
      expect(bodies).toEqual([{ id: "3", body: { category: "instruments" } }]),
    )

    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-cellar")
    expect(notice).toHaveTextContent("daemon 63")

    // And the list was re-asked, so the card shows what the server holds.
    await waitFor(() =>
      expect(cardOf("ring-gauge").getByText("Built-in · instruments · 2 scenes")).toBeInTheDocument(),
    )
  })

  it("keeps a refused amendment open, with the server's own sentence in it", async () => {
    // `template.name` is UNIQUE and it is what a screen resolves against, so a
    // rename onto an existing name is a 409 with a sentence naming it. The
    // interface renders that rather than inventing "the server refused the
    // change", and the dialog stays up: dismissing itself would leave the card
    // exactly as it was with nothing anywhere saying why.
    server.use(
      ...reading(() => TEMPLATES, () => SCREENS),
      http.patch("/api/templates/:template_id", () =>
        HttpResponse.json({ detail: "a template named 'big-number' exists" }, { status: 409 }),
      ),
    )
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "ring-gauge" })
    await userEvent.click(screen.getByRole("button", { name: "Amend ring-gauge" }))

    const dialog = within(await screen.findByRole("dialog"))
    const named = dialog.getByRole("textbox", { name: "Name" })
    await userEvent.clear(named)
    await userEvent.type(named, "big-number")
    await userEvent.click(dialog.getByRole("button", { name: "Save the template" }))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent("a template named 'big-number' exists")
    expect(refusal).not.toHaveTextContent(/the server refused the change/i)
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    // What was typed is still there, so the next attempt starts from it rather
    // than from the row.
    expect(dialog.getByRole("textbox", { name: "Name" })).toHaveValue("big-number")
    // The card behind it is not asserted here on purpose: an open Radix dialog
    // marks the rest of the page `aria-hidden`, so a query for the region would
    // be asking whether the dialog is modal rather than whether the row moved.
  })

  it("says why a panel cannot be detached when there is nowhere to move it", async () => {
    // The dead end, which is a real state and not a hypothetical: one template
    // on a fresh server, and a panel that has to name something. The dialog
    // refuses with the reason and the thing to do about it, rather than offering
    // an empty box.
    server.use(...reading(() => [RING_GAUGE], () => [TRAINS]))
    renderApp({ at: "/templates" })

    await screen.findByRole("region", { name: "ring-gauge" })
    await userEvent.click(screen.getByRole("button", { name: "Detach Trains from ring-gauge" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByText(/no other template for it to draw/i)).toBeInTheDocument()
    expect(dialog.queryByRole("combobox", { name: "Draws instead" })).not.toBeInTheDocument()
    expect(dialog.getByRole("button", { name: "Detach the panel" })).toBeDisabled()
  })
})
