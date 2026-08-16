import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, Screen as ScreenRow, Template } from "../src/api/queries"
import { server } from "./msw"
import { recordCanvas, stubDecoder } from "./paint"
import { renderApp } from "./render"

/**
 * The fixture, and why every number in it is the number it is.
 *
 * This wizard's whole job is to carry four numbers -- a bus, a chip select, a DC
 * pin and an RST pin -- from three different places to two different requests,
 * so a fixture where any two of them coincide is a fixture where a mix-up is
 * invisible. They are therefore drawn from disjoint ranges:
 *
 *   * rack ids 8 and 42; screen ids 11, 12, 13; positions 1, 2 and 6 (so the
 *     next free position is **7** and not the panel count plus one, which is 4);
 *   * SPI buses 0, 1, 2 and chip selects 1, 3, 5 on the configured panels, 3, 4
 *     and 9 on the devices the rack reports;
 *   * DC pins 25, 22, 17 and RST pins 27, 23, 18 -- three distinct pairs, so
 *     "pre-filled from the rack's existing screens" can only be satisfied by
 *     pre-filling from **the right one**;
 *   * clock speeds 32 MHz and 40 MHz, where 40 MHz is `DisplayConfig`'s own
 *     default and 32 MHz is a rack that is not running at it;
 *   * rotations 90, 180 and 270 and one `hflip` that is **true**, on the last
 *     panel and on neither of the others. A real rack is uniformly mounted --
 *     the spec's is all 270 -- but a fixture that were would be an identity
 *     fixture for the mount: copying from the first panel, from a constant, or
 *     from `create_screen`'s own `rotation or 0` would all look the same. Three
 *     distinct rotations and one distinguishing flip make "from the panel the
 *     pinout came from" the only rule that satisfies it.
 *
 * **`(0, 0)` appears exactly once, on the empty rack, because there it is real:**
 * a Pi with one panel wired to `/dev/spidev0.0` is the ordinary case, and it is
 * also `DisplayConfig`'s default for `spi_bus` and `spi_cs`. The strong
 * assertions about which device was probed are therefore made on pi-loft, where
 * the chosen device is SPI1.4 and nothing defaults to it.
 */
const LOFT = 8
const CELLAR = 42

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

// pi-cellar is paired and has **no screens**, which is the state task 8 drew no
// row at all for -- and so the state with nowhere to put an add-screen
// affordance. It is also the only rack in this file with no wiring to pre-fill
// from.
const RACKS = [rack(LOFT, "pi-loft"), rack(CELLAR, "pi-cellar")]

function panel(
  over: Partial<ScreenRow> & Pick<ScreenRow, "id" | "name" | "position" | "display">,
): ScreenRow {
  return {
    daemon_id: LOFT,
    rotation: 270,
    hflip: false,
    enabled: true,
    template: "big-number",
    params: {},
    sleep_override: null,
    ...over,
  }
}

const WEATHER = panel({
  id: 12,
  name: "Weather",
  position: 1,
  rotation: 90,
  display: { backend: "gc9a01", spi_bus: 0, spi_cs: 1, dc: 25, rst: 27, hz: 40_000_000 },
})
const TRAINS = panel({
  id: 13,
  name: "Trains",
  position: 2,
  rotation: 180,
  display: { backend: "gc9a01", spi_bus: 0, spi_cs: 3, dc: 22, rst: 23, hz: 40_000_000 },
})
/**
 * The last panel on pi-loft, and therefore the one its wiring and its mount are
 * both copied from.
 *
 * Its pins, its clock, its rotation and its flip all differ from both of the
 * others', so a form that copied from the *first* screen, or from the schema's
 * defaults, shows different numbers than this one and the assertions below say
 * which.
 */
const KITCHEN = panel({
  id: 11,
  name: "Kitchen",
  position: 6,
  template: "ring-gauge",
  rotation: 270,
  hflip: true,
  display: { backend: "gc9a01", spi_bus: 0, spi_cs: 5, dc: 17, rst: 18, hz: 32_000_000 },
})

// The server answers `ORDER BY position, id`, so this is the order the interface
// really receives: 12, 13, 11.
const LOFT_SCREENS = [WEATHER, TRAINS, KITCHEN]

const BIG_NUMBER: Template = {
  id: 7,
  name: "big-number",
  category: "gauge",
  builtin: true,
  scenes: [],
  params_schema: { big: { type: "binding", label: "Centre text", default: "0" } },
}
const RING_GAUGE: Template = {
  id: 3,
  name: "ring-gauge",
  category: "gauge",
  builtin: true,
  scenes: [],
  params_schema: { title: { type: "string", label: "Title", default: "CPU" } },
}
const TEMPLATES = http.get("/api/templates", () =>
  HttpResponse.json([BIG_NUMBER, RING_GAUGE]),
)

/** One `/dev/spidev<bus>.<cs>` as `Detected` reports it. */
function device(bus: number, cs: number, claimedBy: string | null = null) {
  return { bus, cs, claimed_by: claimedBy }
}

/**
 * What pi-loft's SPI bus looks like: one device it is already driving and two
 * free ones.
 *
 * Two free devices rather than one, so "the wizard sent the wiring for the
 * device that was chosen" is a claim only a fixture with something else to
 * choose can test.
 */
const LOFT_DEVICES = [device(0, 3, "Trains"), device(1, 4), device(2, 9)]

/** A request held open, so a test can stand in the middle of one. */
function held() {
  let release = () => {}
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release: () => release() }
}

/**
 * Render the whole interface at `/screens`, with jsdom's two missing pieces
 * stood in for.
 *
 * The canvas recorder and the decoder are the environment and nothing this task
 * wrote: pi-loft has three live panels on it, and jsdom implements neither a 2D
 * context nor `createImageBitmap`. The WebSocket is `setup.ts`'s inert one, so
 * no frame ever arrives -- which is right for these tests, whose subject is a
 * form and not a picture.
 */
function mountScreens() {
  recordCanvas()
  stubDecoder()
  return renderApp({ at: "/screens" })
}

/** The card one rack is drawn in, so a query cannot reach the other rack's. */
function rackCard(name: string) {
  return within(screen.getByRole("region", { name }))
}

/** Open the wizard on that rack and hand back a query scope inside its dialog. */
async function openWizard(rackName: string) {
  await userEvent.click(
    await screen.findByRole("button", { name: `Add a screen to ${rackName}` }),
  )
  return within(await screen.findByRole("dialog"))
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("the add-screen wizard", () => {
  it("offers only the devices the rack is not already driving", async () => {
    // `claimed_by` is the screen's name or `null`, and never the empty string --
    // the server's model forbids `""` precisely because it is falsy. A device a
    // live worker is mid-frame on must not be offered: probing it is a second
    // panel init on a bus another one is already using, which is the failure the
    // daemon's bus guard exists to prevent.
    //
    // It must still be *listed*, with the screen that has it. An operator who
    // counted three devices with a torch and is shown two has been told nothing
    // about the third, and "the wizard is broken" is a reasonable thing to
    // conclude from that.
    const asked: string[] = []
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", ({ params }) => {
        asked.push(String(params.daemon_id))
        return HttpResponse.json({ panels: LOFT_DEVICES })
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    // Nothing has been asked of the rack merely by opening the dialog.
    expect(asked).toEqual([])
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))

    // The rack that was asked is the rack whose button was pressed, and not the
    // other one in the listing.
    await waitFor(() => expect(asked).toEqual([String(LOFT)]))

    const devices = within(await wizard.findByRole("list", { name: "SPI devices on pi-loft" }))
    // All three are accounted for.
    expect(devices.getAllByRole("listitem")).toHaveLength(3)
    // Two are offered.
    expect(devices.getByRole("button", { name: "Use SPI1.4" })).toBeInTheDocument()
    expect(devices.getByRole("button", { name: "Use SPI2.9" })).toBeInTheDocument()
    // And the third is not, because a screen is already driving it.
    expect(devices.queryByRole("button", { name: "Use SPI0.3" })).not.toBeInTheDocument()
    expect(devices.getByText(/SPI0\.3/)).toHaveTextContent("Trains")
  })

  it("pre-fills DC and RST from the rack's existing screens", async () => {
    // They are wiring choices; nothing on the bus reports them.
    //
    // A GC9A01 has no readable id over 4-wire SPI, so the daemon can say which
    // `/dev/spidev*` exist and nothing whatever about which GPIO lines drive
    // them. The only thing this interface knows about a rack's soldering is the
    // rack's own configured panels -- so the boxes start from the last panel on
    // it, whole, and the operator corrects them.
    const probes: unknown[] = []
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", async ({ request }) => {
        probes.push(await request.json())
        return HttpResponse.json({ ok: true, error: null })
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))

    // Kitchen's, which is the panel at the right-hand end of this rack -- not
    // Weather's 25 and 27, which is what reading the list from the front gives,
    // and not empty, which is what not looking at all gives.
    expect(await wizard.findByRole("spinbutton", { name: "DC pin" })).toHaveValue(17)
    expect(wizard.getByRole("spinbutton", { name: "RST pin" })).toHaveValue(18)
    // The clock comes from the same panel and for the same reason: a rack
    // running at 32 MHz is a rack whose ribbon will not take 40, and proving a
    // panel at a speed the rack does not use proves the wrong thing.
    expect(wizard.getByRole("spinbutton", { name: "Clock speed" })).toHaveValue(32_000_000)

    await userEvent.click(wizard.getByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))

    // The exact document, and every field of it is load-bearing. `ProbeBody`
    // forbids extra fields and defaults none of its own, so this is the whole of
    // what the rack is asked to prove: the device that was chosen (SPI1.4, not
    // SPI2.9 and not SPI0.3), the wiring that is in the boxes, and a hold of
    // **5 seconds** -- `PROBE_HOLD_BUDGET`, which the daemon applies silently
    // and `Probed` says nothing about, so a longer number here would leave an
    // operator answering "no, nothing lit" about a panel that went dark while
    // they were still being counted down at.
    await waitFor(() =>
      expect(probes).toEqual([
        { bus: 1, cs: 4, dc: 17, rst: 18, hz: 32_000_000, hold_s: 5 },
      ]),
    )
  })

  it("bolts the new panel in the way the rest of the rack is bolted in", async () => {
    // `rotation` and `hflip` are not decoration and not a view setting: they say
    // how the glass is screwed into the rack, the daemon applies them to a frame
    // *before* it streams it, and nothing in this interface rotates a picture. So
    // they are the same kind of fact as DC and RST -- chosen with a screwdriver,
    // unreportable by anything on the bus, and knowable here only from the rack's
    // own panels.
    //
    // Which is why they are copied from the panel the pinout is copied from.
    // `create_screen` writes `body.rotation or 0` for a body that names none, so a
    // wizard that sends none puts the first frame on a rack of 270s the wrong way
    // up -- on the one page whose entire premise is that somebody just stood in
    // front of the rack and confirmed with their eyes that this panel is right.
    const created: unknown[] = []
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", () =>
        HttpResponse.json({ ok: true, error: null }),
      ),
      http.post("/api/screens", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        created.push(body)
        return HttpResponse.json(
          panel({
            id: 29,
            name: String(body.name),
            position: Number(body.position),
            display: body.display as ScreenRow["display"],
          }),
          { status: 201 },
        )
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Yes, that panel lit up" }))

    // Said out loud on the step before the 201, and it names the panel it was
    // taken from -- Kitchen, at 270 and flipped, and not Weather's 90 or Trains'
    // 180. A mount nobody was shown is a mount nobody can disagree with while
    // they are still standing in front of the rack.
    expect(await wizard.findByText(/bolted in like Kitchen/i)).toHaveTextContent(
      /rotated 270°.*flipped horizontally/i,
    )

    await userEvent.type(await wizard.findByRole("textbox", { name: "Name" }), "Doorbell")
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))

    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({ rotation: 270, hflip: true })
  })

  it("will not add a screen until the probe was confirmed", async () => {
    // `Probed.ok` means "the device opened and the pattern was written". It does
    // not mean anybody saw it, and only the person in front of the rack can say
    // that -- a panel wired to the wrong DC line can open, take a frame and show
    // nothing at all. So the screen is created from a *human* confirmation and
    // never from `ok` alone.
    const created: unknown[] = []
    const lit = held()
    let rows = LOFT_SCREENS
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", async () => {
        await lit.promise
        return HttpResponse.json({ ok: true, error: null })
      }),
      http.post("/api/screens", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        created.push(body)
        const added = panel({
          id: 29,
          name: String(body.name),
          position: Number(body.position),
          template: String(body.template),
          display: body.display as ScreenRow["display"],
        })
        rows = [...rows, added]
        return HttpResponse.json(added, { status: 201 })
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Continue to the probe" }))

    // **Before** it lights anything, the step says what it is about to do. A
    // probe is a real panel init -- roughly 160 ms of resets and register writes
    // and then a frame -- and it takes the rack's bus for the whole hold, so
    // every other panel on that bus stops. Somebody pressing a button labelled
    // only "Probe" has not been told that.
    const warning = await wizard.findByRole("alert")
    expect(warning).toHaveTextContent(/SPI1\.4/)
    expect(warning).toHaveTextContent(/5 seconds/)
    expect(warning).toHaveTextContent(/bus/i)
    expect(wizard.queryByRole("button", { name: "Add the screen" })).not.toBeInTheDocument()

    await userEvent.click(wizard.getByRole("button", { name: "Light the panel" }))
    // While the rack is holding the panel lit there is still nothing to add,
    // and the step says the panel is lit now.
    expect(await wizard.findByText(/is lit now/i)).toBeInTheDocument()
    expect(wizard.queryByRole("button", { name: "Add the screen" })).not.toBeInTheDocument()

    lit.release()

    // The rack answered yes. That is still not an answer about what a person
    // saw, so there is no way to add a screen yet and nothing has been created.
    const asked = await wizard.findByText(/did the panel on SPI1\.4 light up/i)
    expect(asked).toBeInTheDocument()
    expect(wizard.queryByRole("button", { name: "Add the screen" })).not.toBeInTheDocument()
    expect(created).toEqual([])

    // Saying no goes back to the wiring rather than forward, and still creates
    // nothing.
    await userEvent.click(wizard.getByRole("button", { name: "No, nothing lit up" }))
    expect(await wizard.findByRole("spinbutton", { name: "DC pin" })).toHaveValue(17)
    expect(created).toEqual([])

    await userEvent.click(wizard.getByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Yes, that panel lit up" }))

    // The template select is opened *before* the name is typed: a Radix select
    // opened immediately after an input was edited produces `act` warnings from
    // its focus scope in this harness, which task 8's report writes up.
    await userEvent.click(await wizard.findByRole("combobox", { name: "Template" }))
    await userEvent.click(screen.getByRole("option", { name: "ring-gauge" }))
    await userEvent.type(wizard.getByRole("textbox", { name: "Name" }), "Doorbell")
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))

    // The wiring that was proved, and nothing invented beside it. `position` is
    // the next free ordinal on this rack -- 7, one past Kitchen's 6 -- and not
    // the panel count plus one, which would be 4 and would land on top of a
    // panel that is already there. The mount comes from Kitchen too; the test
    // below is the one that argues about it.
    await waitFor(() =>
      expect(created).toEqual([
        {
          daemon_id: LOFT,
          name: "Doorbell",
          position: 7,
          template: "ring-gauge",
          rotation: 270,
          hflip: true,
          display: {
            backend: "gc9a01",
            spi_bus: 1,
            spi_cs: 4,
            dc: 17,
            rst: 18,
            hz: 32_000_000,
          },
        },
      ]),
    )

    // And the rack has it: the dialog closes on the 201 and the list is asked
    // for again, so the new panel is on the canvas rather than appearing after
    // the next reload.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    expect(
      await rackCard("pi-loft").findByRole("button", { name: "Doorbell" }),
    ).toBeInTheDocument()
  })

  it("says what went wrong when a probe fails, and lets you change the wiring", async () => {
    // A failed probe is a **200** carrying `ok: false`. The probe ran, the rack
    // answered, and the answer is no -- "SPI1.4 opened but would not take a
    // frame" is a fact about a ribbon, and it is the most useful sentence
    // anybody is going to get about it. Reading that as a success is the whole
    // trap; replacing it with an apology of this interface's own is the other
    // half.
    //
    // And what somebody does about it is change a pin and try again, which means
    // going back one step and not starting the wizard over: the detect they ran
    // is still true, and the device they chose is still the device.
    const probes: unknown[] = []
    let answer: () => Response = () =>
      HttpResponse.json({
        ok: false,
        error: "SPI1.4 opened but would not take a frame",
      })
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", async ({ request }) => {
        probes.push(await request.json())
        return answer()
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))

    // The rack's own sentence, not this interface's summary of it.
    expect(
      await wizard.findByText("SPI1.4 opened but would not take a frame"),
    ).toBeInTheDocument()
    // And it is not being reported as a panel that lit.
    expect(wizard.queryByText(/did the panel on SPI1\.4 light up/i)).not.toBeInTheDocument()
    expect(wizard.queryByRole("button", { name: "Add the screen" })).not.toBeInTheDocument()

    // A rack that says no and gives no reason. `Probed.error` is `str | None`
    // and task 9 kept the pair unforced on purpose: making `ok=False` require a
    // reason would turn a daemon that forgot to give one into a *timeout* on
    // this server, which reads as a rack that never answered rather than one
    // that answered no. So it is a state a conformant rack really produces, and
    // the sentence in its place has to be this interface's own.
    answer = () => HttpResponse.json({ ok: false, error: null })
    await userEvent.click(wizard.getByRole("button", { name: "Light the panel" }))
    expect(
      await wizard.findByText("The rack refused the wiring and said nothing more about it."),
    ).toBeInTheDocument()
    expect(wizard.getByText(/could not drive that wiring/i)).toBeInTheDocument()
    expect(wizard.queryByRole("button", { name: "Add the screen" })).not.toBeInTheDocument()

    // Back to the wiring, with the device still chosen and the boxes still
    // holding what was tried, so the fix is one keystroke rather than four
    // clicks and a re-detect.
    await userEvent.click(wizard.getByRole("button", { name: "Change the wiring" }))
    const rst = await wizard.findByRole("spinbutton", { name: "RST pin" })
    expect(rst).toHaveValue(18)
    await userEvent.clear(rst)
    await userEvent.type(rst, "19")

    // The rack is busy with somebody else's probe. This is a **409**, and it is
    // the one refusal that says nothing whatever about the wiring in the boxes:
    // the bus is held, for up to five seconds, and the thing to do is wait.
    answer = () =>
      HttpResponse.json(
        {
          detail:
            "daemon 8 is already probing a panel, and a probe holds that rack's SPI bus " +
            "for up to 5s. Wait for it to finish and try again",
        },
        { status: 409 },
      )
    await userEvent.click(wizard.getByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))

    // One alert, and it is the waiting kind rather than the failing kind -- said
    // in the markup and not only in the prose, because "the bus is busy" and
    // "your wiring is wrong" are the two readings this status has and only one
    // of them is true.
    const busyAlert = await wizard.findByRole("alert")
    expect(busyAlert).toHaveAttribute("data-refusal", "busy")
    expect(busyAlert).toHaveTextContent("That rack is already probing a panel")
    // The server's own sentence, which names the wait and the retry.
    expect(busyAlert).toHaveTextContent("Wait for it to finish and try again")
    expect(busyAlert).toHaveTextContent(/the bus is busy/i)
    // Not a verdict on the wiring, which is the reading that sends somebody back
    // to the rack with a screwdriver for no reason.
    expect(busyAlert).not.toHaveTextContent(/would not take a frame/i)
    expect(busyAlert).not.toHaveTextContent(/could not drive that wiring/i)

    // A refusal that never reached the rack at all reads as what it is, and
    // names the field the server named.
    answer = () =>
      HttpResponse.json(
        {
          detail: [
            {
              type: "greater_than_equal",
              loc: ["body", "hold_s"],
              msg: "Input should be greater than or equal to 0",
            },
          ],
        },
        { status: 422 },
      )
    await userEvent.click(wizard.getByRole("button", { name: "Light the panel" }))
    expect(
      await wizard.findByText("hold_s: Input should be greater than or equal to 0"),
    ).toBeInTheDocument()
    expect(
      wizard.getByText(/refused before it reached the rack/i),
    ).toBeInTheDocument()

    // And the wiring survived all of it: the same button lights the same panel
    // with the pin that was corrected.
    answer = () => HttpResponse.json({ ok: true, error: null })
    await userEvent.click(wizard.getByRole("button", { name: "Light the panel" }))
    expect(
      await wizard.findByText(/did the panel on SPI1\.4 light up/i),
    ).toBeInTheDocument()

    expect(probes).toEqual([
      { bus: 1, cs: 4, dc: 17, rst: 18, hz: 32_000_000, hold_s: 5 },
      { bus: 1, cs: 4, dc: 17, rst: 18, hz: 32_000_000, hold_s: 5 },
      { bus: 1, cs: 4, dc: 17, rst: 19, hz: 32_000_000, hold_s: 5 },
      { bus: 1, cs: 4, dc: 17, rst: 19, hz: 32_000_000, hold_s: 5 },
      { bus: 1, cs: 4, dc: 17, rst: 19, hz: 32_000_000, hold_s: 5 },
    ])
  })

  it("tells you plainly that a rack must be online to detect", async () => {
    // Four ways there is no list of devices, and they send somebody to four
    // different places. The server says which in a sentence that already names a
    // working next step, so what this interface adds is the heading that makes
    // it findable -- and never a replacement for the sentence.
    let answer: () => Response = () =>
      HttpResponse.json(
        {
          detail:
            "daemon 8 is not connected, so nothing could ask it to detect. Only the rack " +
            "knows its own hardware: start the daemon on the Pi and watch GET /api/daemons " +
            "for it to come online",
        },
        { status: 503 },
      )
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () => answer()),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))

    const offline = await wizard.findByRole("alert")
    expect(offline).toHaveTextContent("That rack is not connected")
    expect(offline).toHaveTextContent("start the daemon on the Pi")
    // No device list appeared out of a refusal.
    expect(wizard.queryByRole("list", { name: "SPI devices on pi-loft" })).not.toBeInTheDocument()

    // Connected and silent is a different room: the rack is up, and something on
    // it is busy.
    answer = () =>
      HttpResponse.json(
        {
          detail:
            "daemon 8 is connected but did not answer the detect within 5s. It may be " +
            "applying a configuration and holding its bus -- see " +
            "GET /api/events?daemon_id=8, then try again",
        },
        { status: 504 },
      )
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await waitFor(() =>
      expect(wizard.getByRole("alert")).toHaveTextContent("The rack did not answer"),
    )
    expect(wizard.getByRole("alert")).toHaveTextContent("applying a configuration")
    expect(wizard.getByRole("alert")).not.toHaveTextContent("not connected")

    // A rack that answered the *other* question is the rack's confusion, not
    // this server's, and the thing to check is the build on the Pi.
    answer = () =>
      HttpResponse.json(
        {
          detail:
            "daemon 8 answered the detect with 'probe_result', which is not a reply to it. " +
            "Check that the rack is running a build of ors-daemon this server knows",
        },
        { status: 502 },
      )
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await waitFor(() =>
      expect(wizard.getByRole("alert")).toHaveTextContent("The rack answered something else"),
    )
    expect(wizard.getByRole("alert")).toHaveTextContent("running a build of ors-daemon")

    // And a rack that is not there at all. The server's `no daemon 8` names no
    // next step, so this is the one status where the interface has to supply
    // one.
    answer = () => HttpResponse.json({ detail: "no daemon 8" }, { status: 404 })
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await waitFor(() =>
      expect(wizard.getByRole("alert")).toHaveTextContent("There is no such rack"),
    )
    expect(wizard.getByRole("alert")).toHaveTextContent("no daemon 8")
    expect(wizard.getByRole("alert")).toHaveTextContent(/reload/i)
  })

  it("gives a rack with no screens somewhere to add the first one", async () => {
    // Task 8 grouped the canvas by the screens it was given, so a paired rack
    // with none got no row -- and an empty rack is exactly the rack a wizard is
    // for. It is also the only rack there is nothing to pre-fill from, and the
    // pins must then be **empty** rather than 0: `DisplayConfig` defaults `dc`
    // and `rst` to nothing at all for that reason, because GPIO 0 is a real pin
    // and a form that offered it would be proposing a board nobody built.
    const probes: unknown[] = []
    const created: unknown[] = []
    let rows: ScreenRow[] = LOFT_SCREENS
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(rows)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        // On a rack with one panel wired to /dev/spidev0.0 -- the ordinary case,
        // and the one place in this file where (0, 0) is the real answer.
        HttpResponse.json({ panels: [device(0, 0)] }),
      ),
      http.post("/api/daemons/:daemon_id/probe", async ({ request }) => {
        probes.push(await request.json())
        return HttpResponse.json({ ok: true, error: null })
      }),
      http.post("/api/screens", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        created.push(body)
        const added = panel({
          id: 31,
          daemon_id: CELLAR,
          name: String(body.name),
          position: Number(body.position),
          template: String(body.template),
          display: body.display as ScreenRow["display"],
        })
        rows = [...rows, added]
        return HttpResponse.json(added, { status: 201 })
      }),
    )
    mountScreens()

    // The row exists at all, which it did not before.
    const empty = await screen.findByRole("region", { name: "pi-cellar" })
    expect(within(empty).getByText(/no panels on this rack yet/i)).toBeInTheDocument()

    const wizard = await openWizard("pi-cellar")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI0.0" }))

    // Nothing to copy, so nothing is offered -- and emphatically not zero.
    const dc = await wizard.findByRole("spinbutton", { name: "DC pin" })
    const rst = wizard.getByRole("spinbutton", { name: "RST pin" })
    expect(dc).toHaveValue(null)
    expect(rst).toHaveValue(null)
    // A clock speed does have a default in the model, and this is it.
    expect(wizard.getByRole("spinbutton", { name: "Clock speed" })).toHaveValue(40_000_000)
    // And there is nothing to prove until both pins are named: `ProbeBody`
    // requires them and gives them no defaults, so an empty box is not a probe.
    expect(wizard.getByRole("button", { name: "Continue to the probe" })).toBeDisabled()

    await userEvent.type(dc, "26")
    await userEvent.type(rst, "19")
    await userEvent.click(wizard.getByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Yes, that panel lit up" }))
    // And nothing to copy a mount from either, which is said rather than
    // silently defaulted: the server's `rotation or 0` is right here precisely
    // because this interface has been told nothing about how this one went in.
    expect(await wizard.findByText(/no other panel on this rack to copy a mount from/i))
      .toBeInTheDocument()
    await userEvent.type(await wizard.findByRole("textbox", { name: "Name" }), "Doorbell")
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))

    await waitFor(() => expect(created).toHaveLength(1))
    // Neither field is sent at all -- not `rotation: 0`, which would be this
    // interface asserting a mount it has no source for and would be
    // indistinguishable from one it had copied.
    expect(created[0]).not.toHaveProperty("rotation")
    expect(created[0]).not.toHaveProperty("hflip")
    await waitFor(() =>
      expect(created).toEqual([
        {
          daemon_id: CELLAR,
          name: "Doorbell",
          position: 1,
          // Nobody chose one, so it is the first the server offered -- and a
          // screen has to name a template, so there is no "none".
          template: "big-number",
          display: {
            backend: "gc9a01",
            spi_bus: 0,
            spi_cs: 0,
            dc: 26,
            rst: 19,
            hz: 40_000_000,
          },
        },
      ]),
    )
    expect(probes).toEqual([
      { bus: 0, cs: 0, dc: 26, rst: 19, hz: 40_000_000, hold_s: 5 },
    ])
    // The first panel on a rack sits at position 1. `Number("")` is 0 and
    // `Math.max()` of nothing is -Infinity; `ScreenBody.position` is `ge=1`, so
    // either would be a 422 on the one request the whole wizard exists to make.
    await waitFor(() =>
      expect(
        within(screen.getByRole("region", { name: "pi-cellar" })).getByRole("button", {
          name: "Doorbell",
        }),
      ).toBeInTheDocument(),
    )
  })

  it("says a refused create for what that route can really mean", async () => {
    // `POST /api/screens` is not a question put to a rack, and it does not
    // answer the list the detect and probe routes answer. It reaches no daemon
    // at all: it writes a row, mints a config version and hands the push to
    // whoever is connected. So it has no 409 ("already probing a panel"), no 503
    // ("start the daemon on the Pi") and no 504 -- none of those are raised
    // anywhere on that path -- and reading its refusals through the mapping that
    // has them sends somebody to a rack over a row a database would not take.
    const answers: (() => Response)[] = []
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", () =>
        HttpResponse.json({ ok: true, error: null }),
      ),
      http.post("/api/screens", () => (answers.shift() ?? (() => HttpResponse.error()))()),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Continue to the probe" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Yes, that panel lit up" }))
    await userEvent.type(await wizard.findByRole("textbox", { name: "Name" }), "Doorbell")

    // A template deleted in another tab between opening this dialog and pressing
    // the button. `create_screen` looks up no template -- the `screen` table has
    // no foreign key on it -- so this is not a 404: it is the snapshot builder
    // refusing to hand a rack a configuration naming a template that is not
    // there, which is a **422** with a sentence naming the screen and the
    // template.
    answers.push(() =>
      HttpResponse.json(
        {
          detail:
            "screen 'Doorbell' names template 'big-number', which is not defined",
        },
        { status: 422 },
      ),
    )
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))

    const refused = await wizard.findByRole("alert")
    expect(refused).toHaveTextContent("The screen was not created")
    expect(refused).toHaveTextContent("which is not defined")
    expect(refused).toHaveTextContent(/reload the Screens page/i)
    // Not the probe routes' 422, which promises something this route cannot: a
    // create never went near the rack in the first place, so "nothing was sent
    // to the rack" is not the reassurance that is wanted here.
    expect(refused).not.toHaveTextContent(/before it reached the rack/i)
    // And the dialog is still open on the wiring that was proved, because
    // nothing about the rack changed.
    expect(wizard.getByRole("button", { name: "Add the screen" })).toBeInTheDocument()

    // A status this route does not raise at all -- a proxy's, or a gateway's.
    // The detect mapping would head it "That rack is not connected" and tell
    // somebody to go and start a daemon that is running perfectly well.
    answers.push(() => HttpResponse.json({ detail: "upstream is unavailable" }, { status: 503 }))
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))
    await waitFor(() =>
      expect(wizard.getByRole("alert")).toHaveTextContent("The screen could not be created"),
    )
    expect(wizard.getByRole("alert")).toHaveTextContent("upstream is unavailable")
    expect(wizard.getByRole("alert")).not.toHaveTextContent(/not connected/i)
    expect(wizard.getByRole("alert")).not.toHaveTextContent(/start the daemon/i)

    // The one status this route and the other two agree about, because there is
    // only one thing `create_screen` looks up: the daemon row.
    answers.push(() => HttpResponse.json({ detail: "no daemon 8" }, { status: 404 }))
    await userEvent.click(wizard.getByRole("button", { name: "Add the screen" }))
    await waitFor(() =>
      expect(wizard.getByRole("alert")).toHaveTextContent("There is no such rack"),
    )
    expect(wizard.getByRole("alert")).toHaveTextContent("no daemon 8")
    expect(wizard.getByRole("alert")).toHaveTextContent(/nothing was created/i)
  })

  it("takes focus to the step that replaced the one you were looking at", async () => {
    // Every step's content replaces the last inside one `DialogContent`, so the
    // button that was pressed is unmounted by the press itself and focus falls
    // back to Radix's container. Somebody driving this from the keyboard is then
    // at the top of a dialog with no word about which step they are now on.
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", () =>
        HttpResponse.json({ ok: true, error: null }),
      ),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    // **Not** on the way in. Radix places focus when the dialog opens and reads
    // its title and description there; taking it back would be a second move
    // nobody asked for.
    expect(wizard.getByRole("group", { name: /^Step 1 of 4/ })).not.toHaveFocus()

    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    expect(wizard.getByRole("group", { name: "Step 2 of 4: Confirm the wiring" })).toHaveFocus()

    await userEvent.click(wizard.getByRole("button", { name: "Continue to the probe" }))
    expect(wizard.getByRole("group", { name: "Step 3 of 4: Prove the panel" })).toHaveFocus()

    await userEvent.click(await wizard.findByRole("button", { name: "Light the panel" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Yes, that panel lit up" }))
    expect(wizard.getByRole("group", { name: "Step 4 of 4: Add the screen" })).toHaveFocus()

    // And going back is a step change too.
    await userEvent.click(wizard.getByRole("button", { name: "Change the wiring" }))
    expect(wizard.getByRole("group", { name: "Step 2 of 4: Confirm the wiring" })).toHaveFocus()
  })

  it("announces the panel going dark to somebody who cannot see it", async () => {
    // This is the one thing on the page that changes with nobody's hand on it.
    // The click is five seconds before the answer -- the daemon holds the panel
    // lit and replies when it lets the bus go -- and the step does not change
    // when it arrives, so no focus moves and nothing is announced by anything
    // else. `DialogDescription` will not do it: `aria-describedby` is read when
    // the dialog is described and not re-read when its text changes.
    const lit = held()
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/screens", () => HttpResponse.json(LOFT_SCREENS)),
      TEMPLATES,
      http.post("/api/daemons/:daemon_id/detect", () =>
        HttpResponse.json({ panels: LOFT_DEVICES }),
      ),
      http.post("/api/daemons/:daemon_id/probe", async () => {
        await lit.promise
        return HttpResponse.json({ ok: true, error: null })
      }),
    )
    mountScreens()

    const wizard = await openWizard("pi-loft")
    await userEvent.click(wizard.getByRole("button", { name: "Detect panels" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Use SPI1.4" }))
    await userEvent.click(await wizard.findByRole("button", { name: "Continue to the probe" }))

    // The region is in the tree *before* anything is put in it, and it is the
    // same node throughout. A live region mounted along with its first content
    // is a region a screen reader has no previous state for, and the change it
    // exists to announce is then the change of it appearing.
    const verdict = wizard.getByRole("status")
    expect(verdict).toHaveAttribute("aria-live", "polite")
    expect(verdict).toBeEmptyDOMElement()

    await userEvent.click(wizard.getByRole("button", { name: "Light the panel" }))
    expect(await within(verdict).findByText(/is lit now/i)).toBeInTheDocument()

    lit.release()
    expect(
      await within(verdict).findByText(/did the panel on SPI1\.4 light up/i),
    ).toBeInTheDocument()
    // The two buttons the announcement is about are inside it, so what is read
    // out is the question and the answers to it.
    expect(within(verdict).getByRole("button", { name: "Yes, that panel lit up" })).toBeInTheDocument()
  })
})

describe("the screens page's claim about pairing", () => {
  // "No racks are paired yet" is a statement about `GET /api/daemons`, and the
  // page reads that list as `racks.data ?? []` -- which is what a fetch still in
  // flight and a fetch that failed both look like. The copy this task replaced
  // ("No panels yet") was true of all three; this one is more useful and is only
  // true of one, so it is the one that has to wait until the page has looked.
  it("waits for the rack list before saying nothing is paired", async () => {
    const listed = held()
    server.use(
      SIGNED_IN,
      http.get("/api/screens", () => HttpResponse.json([])),
      http.get("/api/daemons", async () => {
        await listed.promise
        return HttpResponse.json([])
      }),
    )
    mountScreens()

    // The screens came back first and they are empty; the racks have not been
    // heard from. A wall of four paired racks looks exactly like this from here.
    await screen.findByRole("heading", { name: "Screens" })
    expect(screen.queryByText(/no racks are paired yet/i)).not.toBeInTheDocument()

    listed.release()
    expect(await screen.findByText(/no racks are paired yet/i)).toBeInTheDocument()
  })

  it("says the rack list could not be read rather than that none are paired", async () => {
    server.use(
      SIGNED_IN,
      http.get("/api/screens", () => HttpResponse.json([])),
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "the daemon table could not be read" }, { status: 500 }),
      ),
    )
    mountScreens()

    // Otherwise this one is permanent: the fetch never resolves to a list, so
    // the page would go on asserting something it never managed to check, under
    // an alert saying it could not check it.
    const failed = await screen.findByRole("alert")
    expect(failed).toHaveTextContent("The racks could not be read")
    expect(failed).toHaveTextContent("the daemon table could not be read")
    expect(screen.queryByText(/no racks are paired yet/i)).not.toBeInTheDocument()
  })
})
