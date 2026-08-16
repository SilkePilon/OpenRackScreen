/**
 * The whole thing, once, in order: a blank rack becomes a rack drawing a panel.
 *
 * Seven specs, one browser page, one server and one daemon. They are a
 * narrative rather than seven independent tests, and they are declared serial
 * because that is what they are -- a password can only be set on a database
 * that has none, a token can only be spent once, and the last two do things to
 * the server that a spec running beside them would see.
 *
 * **What this layer is for.** The base64 trap that shipped in M3a passed a
 * green Python test and a green component test at the same time: pydantic emits
 * the URL-safe alphabet, `atob` throws on it, and `base64.b64decode` accepts
 * either -- so both ends agreed with each other and neither agreed with a
 * browser. Nothing below this file could have caught it. Spec 4 is where it
 * would have died: it reads the canvas's own pixels, and a page that showed
 * nothing would have counted zero of them.
 */

import { expect, test, PASSWORD, RACK, SPARE_RACK } from "./fixture"
import type { Page } from "@playwright/test"

test.describe.configure({ mode: "serial" })

/** The panel the wizard creates, and the numbers it must be created with. */
const SCREEN = "Cellar CPU"
const DEVICE = { bus: 3, cs: 1 }
const DC = 25
const RST = 27

/**
 * What is written on the document in spec 6 and read back in spec 7.
 *
 * On `window` and not on `window.name`, which survives a reload and would make
 * the check pass for the thing it exists to refuse. This is the whole of "it
 * recovered *without a reload*": a new document has no such property.
 */
const MARK = "the document the outage happened in"

declare global {
  interface Window {
    orsDocumentMark?: string
  }
}

/** Carried between specs, because they are steps of one story. */
let token = ""
let screenId = 0
let decoyIds: number[] = []

/** One panel's canvas, counted rather than looked at. */
type Pixels = {
  /** Pixels bright enough for a person to see against the black glass. */
  lit: number
  /** Pixels where red clearly dominates blue: the `red` palette. */
  red: number
  /** And the other way round: the `cyan` palette this template starts on. */
  blue: number
}

/**
 * Read what is actually on the glass in the browser.
 *
 * `getImageData` and not a screenshot comparison: the question these specs ask
 * is "did any pixels arrive", which a reference image answers with a diff that
 * fails on a font hint or an anti-aliased edge. It is also the only assertion
 * that a decoded frame can fail -- an element that exists, a canvas of the
 * right size and a `<img>` with a src are all things a broken decode leaves
 * behind untouched.
 *
 * The canvas is same-origin all the way down (a `Blob` from bytes this tab
 * decoded, through `createImageBitmap`), so it is not tainted and this is
 * allowed to read it.
 */
async function readPanel(ui: Page, id: number): Promise<Pixels> {
  return await ui
    .locator(`[data-testid="panel-${id}"] canvas`)
    .evaluate((element): Pixels => {
      const canvas = element as HTMLCanvasElement
      const context = canvas.getContext("2d")
      if (context === null) throw new Error("this browser gave the panel no 2D context")
      const { data } = context.getImageData(0, 0, canvas.width, canvas.height)
      let lit = 0
      let red = 0
      let blue = 0
      for (let at = 0; at < data.length; at += 4) {
        const r = data[at]
        const g = data[at + 1]
        const b = data[at + 2]
        // Outside the circular clip nothing was ever drawn, so the surface is
        // still transparent black. Counting it would make every panel "lit".
        if (data[at + 3] === 0) continue
        if (Math.max(r, g, b) > 60) lit += 1
        if (r > b + 60 && r > 90) red += 1
        if (b > r + 60 && b > 90) blue += 1
      }
      return { lit, red, blue }
    })
}

// A failure here is usually something the server or the rack said, and neither
// of them writes to the terminal this reporter owns.
test.afterEach(async ({ rack }, info) => {
  if (info.status === info.expectedStatus) return
  process.stdout.write(`\n--- ors-server ---\n${rack.serverLog()}\n`)
  process.stdout.write(`\n--- ors-daemon ---\n${rack.daemonLog()}\n`)
})

test("1: a rack with no password asks for one, and takes it", async ({ rack, ui }) => {
  await ui.goto(rack.origin)

  // Not the login page: the server distinguishes "no password has ever been
  // set" from "there is one and this browser has no session", and a fresh rack
  // sent to a login form whose password does not exist yet is unreachable.
  await expect(ui.getByRole("heading", { name: "Set a password" })).toBeVisible()
  await ui.getByLabel("Password").fill(PASSWORD)
  await ui.getByRole("button", { name: "Set password" }).click()

  // Setting it does not sign anyone in -- the session comes from /login and
  // nowhere else -- so this is a second form and not a redirect into the app.
  await expect(ui.getByRole("heading", { name: "Sign in" })).toBeVisible()
  await ui.getByLabel("Password").fill(PASSWORD)
  await ui.getByRole("button", { name: "Sign in" }).click()

  await expect(ui.getByRole("heading", { name: "Daemons", level: 1 })).toBeVisible()
  await expect(ui.getByText("No racks yet. Pair one, and its screens can be added afterwards.")).toBeVisible()
})

test("2: pairing mints a token, shows it once, and the rack dials in", async ({ rack, ui }) => {
  // The second rack and the two screens on it, plus the night window off. See
  // `arrange`: this is what stops any two numbers in this file coinciding, and
  // it has to happen before the rack below is paired for the ids to come out
  // that way.
  await rack.arrange()

  await ui.getByRole("button", { name: "Pair a rack" }).click()
  const dialog = ui.getByRole("dialog")
  await dialog.getByLabel("Name").fill(RACK)
  await dialog.getByRole("button", { name: "Mint the pairing token" }).click()

  await expect(dialog.getByText(`${RACK} is waiting for its token`)).toBeVisible()
  token = (await dialog.locator("code").first().innerText()).trim()
  // A credential, so: long, and nothing a shell would split.
  expect(token).toMatch(/^\S{20,}$/)
  // The line an operator runs on the Pi names this browser's own origin, which
  // is the ephemeral port this run was given -- not a number written down
  // anywhere.
  await expect(dialog.locator("code").nth(1)).toHaveText(
    `ors-daemon connect --server ${rack.origin} --token ${token}`,
  )

  await dialog.getByRole("button", { name: "Done" }).click()
  await expect(dialog).toBeHidden()

  // **Once.** The server keeps a hash and nothing else, so this render was the
  // only copy that will ever exist: it is gone from the page, and reopening the
  // dialog offers to mint a new one rather than showing the old one again.
  await expect(ui.getByText(token)).toHaveCount(0)
  await ui.getByRole("button", { name: "Pair a rack" }).click()
  await expect(ui.getByRole("dialog").getByRole("button", { name: "Mint the pairing token" })).toBeVisible()
  await expect(ui.getByText(token)).toHaveCount(0)
  await ui.keyboard.press("Escape")
  await expect(ui.getByRole("dialog")).toBeHidden()

  await expect(ui.getByRole("region", { name: RACK })).toContainText("Offline")

  // Now spend it on a real daemon, which pairs itself and dials back.
  await rack.startDaemon(token)

  // The strip is fed by the `daemons` message and by nothing else, so this
  // going green is the socket working end to end -- no refetch says it.
  await expect(ui.getByTestId("rack-strip").getByLabel(`${RACK} is online`)).toBeVisible()
  await expect(ui.getByTestId("rack-strip").getByLabel(`${SPARE_RACK} is offline`)).toBeVisible()
})

test("3: the wizard detects a device, proves it, and adds the screen", async ({ rack, ui }) => {
  await ui.getByRole("link", { name: "Screens" }).click()
  await expect(ui.getByRole("heading", { name: "Screens", level: 1 })).toBeVisible()

  await ui.getByRole("button", { name: `Add a screen to ${RACK}` }).click()
  const dialog = ui.getByRole("dialog")

  // **Detect.** The rack lists its own devices; nothing else can.
  await dialog.getByRole("button", { name: "Detect panels" }).click()
  await expect(dialog.getByRole("list", { name: `SPI devices on ${RACK}` })).toBeVisible()
  // Both devices are offered, so choosing one is a choice.
  await expect(dialog.getByRole("button", { name: "Use SPI2.0" })).toBeVisible()
  await dialog.getByRole("button", { name: `Use SPI${DEVICE.bus}.${DEVICE.cs}` }).click()

  // **Confirm the wiring.** This rack has no panel to copy a pinout from, so
  // the pin boxes start empty -- there is no GPIO 0 by default -- and the clock
  // is filled in with the number the screen would be created with.
  await expect(dialog.getByLabel("DC pin")).toHaveValue("")
  await expect(dialog.getByLabel("RST pin")).toHaveValue("")
  await expect(dialog.getByLabel("Clock speed")).toHaveValue("40000000")
  await dialog.getByLabel("DC pin").fill(String(DC))
  await dialog.getByLabel("RST pin").fill(String(RST))
  await dialog.getByRole("button", { name: "Continue to the probe" }).click()

  // **Probe.** A real panel init on the rack, held for five seconds, answered
  // when the bus is let go -- so this waits on the answer rather than on a
  // clock, and the wizard's `hold_s` is the daemon's own budget and not more.
  await expect(dialog.getByText(`This lights SPI${DEVICE.bus}.${DEVICE.cs} on ${RACK}`)).toBeVisible()
  await dialog.getByRole("button", { name: "Light the panel" }).click()
  await expect(dialog.getByText(`Did the panel on SPI${DEVICE.bus}.${DEVICE.cs} light up?`)).toBeVisible({
    timeout: 30_000,
  })
  // The rack really painted one: the probe opened a display of its own and put
  // a frame through it. `ok: true` is the daemon's word; this is the artefact.
  expect(rack.panelWrittenAt(`probe SPI${DEVICE.bus}.${DEVICE.cs}`)).not.toBeNull()

  // `ok: true` is not a confirmation. Only the person in front of the rack can
  // say the glass lit, and this is the only path to the last step.
  await dialog.getByRole("button", { name: "Yes, that panel lit up" }).click()

  // **Add.**
  await dialog.getByLabel("Name").fill(SCREEN)
  await dialog.getByLabel("Template").click()
  await ui.getByRole("option", { name: "ring-gauge" }).click()
  await dialog.getByRole("button", { name: "Add the screen" }).click()
  await expect(dialog).toBeHidden()

  await expect(ui.getByRole("button", { name: SCREEN, exact: true })).toBeVisible()

  // What was written is what was proved, read back from the server rather than
  // from the form that sent it.
  const client = await rack.api()
  const screens = (await (await client.get("/api/screens")).json()) as {
    id: number
    name: string
    position: number
    daemon_id: number
    template: string
    display: { backend: string; spi_bus: number; spi_cs: number; dc: number; rst: number }
  }[]
  const mine = screens.find((each) => each.name === SCREEN)
  expect(mine).toBeDefined()
  if (mine === undefined) return
  screenId = mine.id
  decoyIds = screens.filter((each) => each.daemon_id !== mine.daemon_id).map((each) => each.id)
  expect(decoyIds).toHaveLength(2)
  expect(mine.template).toBe("ring-gauge")
  expect(mine.display).toMatchObject({
    backend: "gc9a01",
    spi_bus: DEVICE.bus,
    spi_cs: DEVICE.cs,
    dc: DC,
    rst: RST,
  })

  // **No two of these numbers may be the same.** A screen with id 1 at position
  // 1 has hidden a real bug in this project once, and contiguous ids counted
  // from 1 hid two more: every one of them was a component reading the wrong
  // number and being right by coincidence. The panel below is screen 3 at
  // position 1, on rack 2, at index 0 of its rack's list.
  const index = screens.filter((each) => each.daemon_id === mine.daemon_id).indexOf(mine)
  expect(new Set([mine.id, mine.position, mine.daemon_id, index]).size).toBe(4)
})

test("4: the panel renders -- the canvas has pixels on it", async ({ rack, ui }) => {
  const panel = ui.getByTestId(`panel-${screenId}`)
  await expect(panel).toBeVisible()

  // **The spec this whole layer exists for.** Not "a canvas is on the page" and
  // not "a frame message arrived": the bytes went from the rack's renderer
  // through WebP, through the daemon link, through `/ws/ui`'s own base64,
  // through `atob`, through `createImageBitmap` and onto a 2D context, and this
  // counts what came out the far end. A URL-safe alphabet anywhere on that path
  // throws in `atob` and leaves exactly zero.
  await expect
    .poll(async () => (await readPanel(ui, screenId)).lit, { timeout: 60_000 })
    .toBeGreaterThan(200)

  await expect(panel).toHaveAttribute("data-state", "live")

  // The negative control, and it is what stops this spec passing against a page
  // that draws something -- anything -- everywhere. These two panels belong to
  // the rack nothing is running, and they must still be blank.
  for (const id of decoyIds) {
    expect((await readPanel(ui, id)).lit).toBe(0)
    await expect(ui.getByTestId(`panel-${id}`)).toHaveAttribute("data-state", "offline")
  }

  // And the rack put the same frame on its own glass.
  expect(rack.panelWrittenAt(SCREEN)).not.toBeNull()
})

test("5: an edit made here reaches the rendered panel", async ({ ui }) => {
  const before = await readPanel(ui, screenId)
  // The template starts on the `cyan` palette with the ring at zero, so there
  // is nothing red on the glass to begin with. Asserted rather than assumed:
  // without this the spec below would pass on a panel that was already red.
  expect(before.red).toBeLessThan(50)
  expect(before.blue).toBeGreaterThan(50)

  await ui.getByRole("button", { name: SCREEN, exact: true }).click()
  await expect(ui.getByRole("region", { name: SCREEN })).toBeVisible()
  await ui.getByRole("tab", { name: "Data" }).click()

  // A full ring in a colour nothing on this panel is currently drawn in, so
  // "the change arrived" is a fact about several thousand pixels rather than
  // about a difference somebody has to squint at.
  await ui.getByLabel("Value 0-100", { exact: true }).fill("100")
  await ui.getByLabel("Palette", { exact: true }).fill("red")
  await ui.getByRole("button", { name: "Save changes" }).click()
  await expect(ui.getByText("Saved, and every rack was given it.")).toBeVisible()

  // Saved on the server is not the claim. The claim is that it went out as a
  // snapshot, the rack applied it, drew it, encoded it and sent it back -- and
  // that the browser is now showing the result.
  await expect
    .poll(async () => (await readPanel(ui, screenId)).red, { timeout: 60_000 })
    .toBeGreaterThan(500)
  expect((await readPanel(ui, screenId)).blue).toBeLessThan(50)
})

test("6: the server going away is said honestly, and blamed on nobody", async ({ rack, ui }) => {
  await ui.evaluate((mark) => {
    window.orsDocumentMark = mark
  }, MARK)

  const drawnBefore = rack.panelWrittenAt(SCREEN)
  expect(drawnBefore).not.toBeNull()

  await rack.stopServer()

  const panel = ui.getByTestId(`panel-${screenId}`)

  // **It stops claiming to be live.** Frames have stopped arriving, and a panel
  // that went on presenting a still image as a live one would be the failure
  // the staleness rule exists for.
  await expect(panel).toHaveAttribute("data-state", "stale", { timeout: 30_000 })
  await expect(panel.getByText("Stale")).toBeVisible()

  // **And it does not say the rack has gone**, because nothing has said so. The
  // `daemons` message is the only thing that reports a Pi has gone, and no such
  // message arrived -- the server did. Inferring a dead rack from silence would
  // send somebody to the cellar with a torch.
  await expect(panel.getByText("Offline")).toHaveCount(0)

  // Which is the honest reading, and here is the proof: the rack went on
  // drawing the whole time. No failure of the server may darken the glass.
  await expect
    .poll(() => rack.panelWrittenAt(SCREEN), { timeout: 30_000 })
    .toBeGreaterThan(drawnBefore ?? 0)
})

/**
 * **The spec this file was written to fail, now passing for real.**
 *
 * It carried `test.fail()` when it was written, and the diagnosis with it:
 * `ors_server.auth.Sessions` keeps its tokens in a `set` in memory and says so
 * -- "a restart logs everyone out, which is acceptable here" -- so a restarted
 * server answers **401** to every `/ws/ui` handshake this tab will ever make
 * again, and a browser is given **no status code** for a rejected handshake.
 * `onclose` fires 1006, which is what a server that is not running gives too, so
 * the client could not tell a refused session from a dead server and did the
 * only safe thing, which was to retry. Nothing else asked -- nothing in this
 * interface polls -- so no `fetch` ever met that 401 and the tab sat on a stale
 * panel, dialling a refusal every thirty seconds, with nothing anywhere saying
 * why.
 *
 * **What was chosen, of the two.** Not persisting sessions, which would reverse
 * an M3a decision argued on purpose. The other one: *notice the refusal and
 * route to /login*, because that is the rule this interface already has -- §5.2
 * and §7, "any 401 from any request returns to /login once, without a redirect
 * loop" -- and a handshake refused with 401 **is** a 401. So the client asks one
 * guarded route (`GET /api/settings`) what a refused dial meant, and the answer
 * arrives at the same `setUnauthorizedHandler` every page's queries use, with
 * the same once-only latch. No second rule was built and `ws_ui.py` did not
 * change.
 *
 * **So "recovers" means what it can honestly mean**: the session really did end,
 * and no interface can invent one. This spec asserts the whole of the recovery
 * -- the interface says so on its own, in the same document, and signing in
 * again brings back the very panel this story has been about, still drawing the
 * rack's own pixels. What it refuses to accept is the old behaviour: a silent
 * loop, and a picture that is no longer being updated presented as if it were.
 *
 * Two failures it can still catch, and they are the interesting ones: an
 * interface that says nothing (it never reaches /login), and one that treats
 * every dropped socket as an ended session (spec 6, which stops the server and
 * requires that nothing of the kind is said).
 */
test("7: the server comes back and the interface recovers, without a reload", async ({ rack, ui }) => {
  await rack.startServer()

  // Nothing is clicked and nothing is reloaded. The socket dials again on its
  // own backoff; the handshake is refused because this browser's session died
  // with the old process; the client asks a guarded route what that meant and
  // gets the 401 the WebSocket could not carry.
  //
  // Forty-five seconds because the client's schedule is 1, 2, 4, 8, 16 and then
  // 30 seconds with jitter, so the first dial after the server is back lands
  // inside it -- and the question that follows is one HTTP request.
  await expect(ui.getByRole("heading", { name: "Sign in" })).toBeVisible({ timeout: 45_000 })
  expect(new URL(ui.url()).pathname).toBe("/login")

  // **Without a reload**, said as something a spec can fail: this property was
  // written on the document before the server was stopped, and a reload of any
  // kind -- including the `location.reload()` a cruder fix would have used to
  // get here -- would have thrown it away.
  expect(await ui.evaluate(() => window.orsDocumentMark)).toBe(MARK)

  await ui.getByLabel("Password").fill(PASSWORD)
  await ui.getByRole("button", { name: "Sign in" }).click()
  await expect(ui.getByRole("heading", { name: "Daemons", level: 1 })).toBeVisible()
  await ui.getByRole("link", { name: "Screens" }).click()

  // And the panel comes back to life: a fresh socket, this tab's subscriptions
  // replayed on the handshake, the rack's frames arriving again. The red is
  // spec 5's edit, which the rack has been drawing on its own glass throughout
  // -- so this is the whole path, restored, and not merely a page that renders.
  const panel = ui.getByTestId(`panel-${screenId}`)
  await expect(panel).toHaveAttribute("data-state", "live", { timeout: 60_000 })
  await expect
    .poll(async () => (await readPanel(ui, screenId)).red, { timeout: 30_000 })
    .toBeGreaterThan(500)

  // Still the same document, at the end as at the beginning.
  expect(await ui.evaluate(() => window.orsDocumentMark)).toBe(MARK)
})
