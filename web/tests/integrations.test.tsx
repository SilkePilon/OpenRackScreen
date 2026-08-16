import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, Integration } from "../src/api/queries"
import { server } from "./msw"
import { renderApp } from "./render"

/**
 * The fixture, and why every number and every name in it is what it is.
 *
 * This page is the one that handles secrets, so the identity trap this project
 * has been bitten by four times is worth spelling out again here:
 *
 *   * **Integration ids, rack ids and intervals are all disjoint.** Ids 4, 17
 *     and 29; racks 8, 42 and 63; poll intervals 12, 30 and 5; timeouts 2.5 and
 *     4. No id is a rack, no interval is an id, and no id is its own index in
 *     the list -- `GET /api/integrations` is `ORDER BY id`, so the loft's rows
 *     arrive 4 then 17 and index 0 holds id 4.
 *   * **No name is a number and no name is a substring of another.** A body
 *     that sent `{name: "4"}` instead of `{name: "vault-scrape"}` is only
 *     catchable if the two are never the same string, and a field name
 *     (`sealed`, `cpu`, `disk`, `temp`) is never an integration name.
 *   * **The rack a row belongs to is not the rack the unservable header names.**
 *     Everything edited below is on rack 8; the header names 42 and 63. 63 is in
 *     no listing -- deleted in another tab between the two fetches -- and is
 *     still a rack somebody has to go and look at.
 *   * **The two secrets are different strings.** One is typed into the
 *     credential box, the other is buried in a URL's userinfo. A page that
 *     rendered "the credential" back would otherwise be indistinguishable from
 *     one that echoed the URL's password.
 */
const LOFT = 8
const CELLAR = 42
const UNLISTED_RACK = 63

/** Typed into the credential box. Never travels back, and must never be drawn. */
const NEW_TOKEN = "s3cr3t-bearer-9f2"
/** Buried in a URL's userinfo. The server's refusal does not echo it; nor may this page. */
const URL_PASSWORD = "hunter2-loft"
const URL_WITH_CREDENTIAL = `https://admin:${URL_PASSWORD}@prom.loft:9090`

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

/**
 * The disabled row that holds a credential: the only shape M3a lets one exist in.
 *
 * `build_snapshot` refuses an *enabled* integration holding a `secret_id`,
 * because no integration type can carry one on the wire yet, so this is the
 * state the whole credential half of this page is about.
 */
const VAULT: Integration = {
  id: 4,
  daemon_id: LOFT,
  type: "prometheus",
  name: "vault-scrape",
  config: {
    url: "http://vault.loft:8200",
    timeout: 2.5,
    fields: { sealed: { query: "vault_core_unsealed" } },
  },
  poll_interval: 12,
  enabled: false,
  has_credential: true,
}

/**
 * The enabled row, carrying a `tunnel` block this page cannot edit.
 *
 * `config` is one column and a PATCH replaces the whole document, so a form that
 * rebuilt it out of the three controls it offers would silently delete the
 * tunnel -- and a tunnelled integration takes its base URL from the tunnel, so
 * what is left would poll somewhere else entirely.
 */
const METRICS: Integration = {
  id: 17,
  daemon_id: LOFT,
  type: "prometheus",
  name: "metrics-prom",
  config: {
    url: "http://prom.loft:9090",
    timeout: 4,
    fields: {
      cpu: { query: "node_cpu_ratio" },
      disk: { query: "node_disk_used", reduce: "top", label: "instance" },
    },
    tunnel: {
      kubeconfig: "~/k8s-monitor.yaml",
      namespace: "monitoring",
      service: "prometheus",
      remote_port: 9090,
      local_port: 19090,
    },
  },
  poll_interval: 30,
  enabled: true,
  has_credential: false,
}

/** On the other rack, so "which rack asked" is a question the page has to answer. */
const CELLAR_NODE: Integration = {
  id: 29,
  daemon_id: CELLAR,
  type: "prometheus",
  name: "cellar-node",
  config: {
    url: "http://node.cellar:9100",
    timeout: 4,
    fields: { temp: { query: "node_hwmon_temp" } },
  },
  poll_interval: 5,
  enabled: true,
  has_credential: false,
}

const INTEGRATIONS = [VAULT, METRICS, CELLAR_NODE]

/**
 * Every route this page reads on mount, with the rows behind a getter so a write
 * can move them.
 *
 * The listing is filtered by the `daemon_id` the request carried, and by nothing
 * else: a page that asked without one would be handed an empty list here and
 * every assertion below would fail, which is the point. The rows come back in id
 * order, as `ORDER BY id` gives them.
 */
function reading(rows: () => Integration[]) {
  return [
    SIGNED_IN,
    http.get("/api/daemons", () => HttpResponse.json(RACKS)),
    http.get("/api/integrations", ({ request }) => {
      const asked = new URL(request.url).searchParams.get("daemon_id")
      return HttpResponse.json(
        rows()
          .filter((row) => String(row.daemon_id) === asked)
          .sort((left, right) => left.id - right.id),
      )
    }),
  ]
}

/** One integration's card, found by the region's accessible name. */
const cardOf = (name: string) => within(screen.getByRole("region", { name }))

/**
 * Everything the console was told, captured and passed straight through.
 *
 * Captured because "not in a `console.error`" is one of the places a plaintext
 * credential must not reach, and passed through because the pristine-output
 * check runs under `--reporter=verbose` precisely so a real MSW complaint is
 * visible: a watcher that swallowed it would hide the failures this project has
 * twice lost a day to.
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
 * A secret is in none of the four places a page can leak one.
 *
 * The rendered text is the obvious one and the weakest: React writes a
 * controlled input's value to the `value` *attribute* as well as the property,
 * so `innerHTML` catches a retained form value that `textContent` never would,
 * and the property is read directly as well for the case where it does not.
 * Portals are covered because Radix mounts its dialogs into `document.body`,
 * which is what is being read.
 */
function appearsNowhere(secret: string, said: string[]) {
  expect(document.body.innerHTML).not.toContain(secret)
  expect(document.body.textContent ?? "").not.toContain(secret)
  const boxes = document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(
    "input, textarea",
  )
  for (const box of boxes) expect(box.value).not.toContain(secret)
  expect(said.join("\n")).not.toContain(secret)
}

/** Replace a textarea's contents without userEvent reading `{` as a key sequence. */
async function retype(box: HTMLElement, text: string) {
  await userEvent.clear(box)
  await userEvent.click(box)
  await userEvent.paste(text)
}

afterEach(() => vi.restoreAllMocks())

describe("the integrations page", () => {
  it("creates an integration on the rack it was added from, and patches only what moved", async () => {
    const posted: unknown[] = []
    const patched: unknown[] = []
    let rows = INTEGRATIONS
    server.use(
      ...reading(() => rows),
      http.post("/api/integrations", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        posted.push(body)
        const created: Integration = {
          id: 51,
          daemon_id: LOFT,
          type: "prometheus",
          name: String(body.name),
          config: body.config as Integration["config"],
          poll_interval: Number(body.poll_interval),
          enabled: body.enabled === true,
          has_credential: false,
        }
        rows = [...rows, created]
        // 201 **and** a header naming racks that did not get it. The status code
        // cannot stand in for the header: a create answers 201 even when nothing
        // was pushed, because the row does exist.
        return HttpResponse.json(created, {
          status: 201,
          headers: { "X-Unservable-Daemons": `${CELLAR},${UNLISTED_RACK}` },
        })
      }),
      http.patch("/api/integrations/:integration_id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        patched.push({ id: params.integration_id, body })
        const edited = { ...METRICS, ...body } as Integration
        rows = rows.map((row) => (row.id === METRICS.id ? edited : row))
        return HttpResponse.json(edited)
      }),
    )
    renderApp({ at: "/integrations" })

    expect(await screen.findByRole("heading", { name: "Integrations" })).toBeInTheDocument()
    await screen.findByRole("region", { name: "vault-scrape on pi-loft" })

    // `ORDER BY id`, taken as given: 4 before 17. A page that sorted by name
    // would put metrics-prom first, and a page that took its own insertion order
    // would be indistinguishable from one that took the server's.
    expect(
      screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(["vault-scrape", "metrics-prom", "cellar-node"])
    // The other rack's row is on the other rack, from its own request.
    expect(screen.getByRole("region", { name: "cellar-node on pi-cellar" })).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Add an integration to pi-loft" }))
    const add = within(await screen.findByRole("dialog"))
    // Nothing has been typed, so there is nothing to create: an empty name and
    // an unparseable field map are both refused here rather than as a 422.
    expect(add.getByRole("button", { name: "Add the integration" })).toBeDisabled()

    await userEvent.type(add.getByRole("textbox", { name: "Name" }), "loki-tail")
    await userEvent.type(add.getByRole("textbox", { name: "URL" }), "https://loki.loft:3100")
    await retype(
      add.getByRole("textbox", { name: "Fields (JSON)" }),
      '{"lines": {"query": "sum(rate(log_lines[5m]))"}}',
    )
    const interval = add.getByRole("spinbutton", { name: "Poll interval (seconds)" })
    await userEvent.clear(interval)
    await userEvent.type(interval, "20")
    await userEvent.click(add.getByRole("button", { name: "Add the integration" }))

    // The whole body, exactly. `type` is sent because the generated
    // `NewIntegration` requires it -- openapi-typescript makes a field with a
    // default required, and there is exactly one type today -- and no
    // `credential` key is on the wire at all, because none was asked for.
    await waitFor(() =>
      expect(posted).toEqual([
        {
          daemon_id: LOFT,
          type: "prometheus",
          name: "loki-tail",
          config: {
            url: "https://loki.loft:3100",
            timeout: 4,
            fields: { lines: { query: "sum(rate(log_lines[5m]))" } },
          },
          poll_interval: 20,
          enabled: true,
        },
      ]),
    )
    expect(Object.hasOwn(posted[0] as object, "credential")).toBe(false)

    // The racks that did not get it, from the header and from nothing else --
    // not the rack it was added to, which is what a page reading the body would
    // have said.
    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-cellar")
    expect(notice).toHaveTextContent("rack 63")
    expect(notice).not.toHaveTextContent("pi-loft")
    // And the row on the page is the one the server answered with, after a
    // refetch: there are no optimistic updates here.
    expect(await screen.findByRole("region", { name: "loki-tail on pi-loft" })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Edit metrics-prom" }))
    const edit = within(await screen.findByRole("dialog"))
    expect(edit.getByRole("textbox", { name: "Name" })).toHaveValue("metrics-prom")
    expect(edit.getByRole("textbox", { name: "URL" })).toHaveValue("http://prom.loft:9090")
    // Nothing has moved, so there is nothing to save. An empty PATCH is accepted
    // by the server and still bumps this rack's config_version and pushes a
    // fresh snapshot for an edit nobody made.
    expect(edit.getByRole("button", { name: "Save the integration" })).toBeDisabled()

    const editInterval = edit.getByRole("spinbutton", { name: "Poll interval (seconds)" })
    await userEvent.clear(editInterval)
    await userEvent.type(editInterval, "45")
    await userEvent.click(edit.getByRole("button", { name: "Save the integration" }))

    // One field, and never one more. `IntegrationBody` is `extra="forbid"` with
    // PATCH semantics from `exclude_unset`, so a body echoing everything this
    // page holds would rewrite the config -- tunnel and all -- and, worse, would
    // carry a `credential` key that means "clear the stored secret".
    await waitFor(() => expect(patched).toEqual([{ id: "17", body: { poll_interval: 45 } }]))
    expect(Object.hasOwn((patched[0] as { body: object }).body, "credential")).toBe(false)
    await waitFor(() =>
      expect(cardOf("metrics-prom on pi-loft").getByText(/every 45 seconds/)).toBeInTheDocument(),
    )
  })

  it("sends a credential once and can never show it again", async () => {
    const said = watchConsole()
    const posted: unknown[] = []
    let rows = INTEGRATIONS
    server.use(
      ...reading(() => rows),
      http.post("/api/integrations", async ({ request }) => {
        posted.push(await request.json())
        // What a read gives back: `has_credential`, and no credential field at
        // all. `IntegrationView` has none, so there is nothing for the server to
        // send even by accident -- and this fixture is that view, exactly.
        const created: Integration = {
          id: 51,
          daemon_id: LOFT,
          type: "prometheus",
          name: "loki-tail",
          config: {
            url: "https://loki.loft:3100",
            timeout: 4,
            fields: { lines: { query: "up" } },
          },
          poll_interval: 5,
          enabled: false,
          has_credential: true,
        }
        rows = [...rows, created]
        return HttpResponse.json(created, { status: 201 })
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "vault-scrape on pi-loft" })
    // A stored credential is drawn as the fact that one is stored, from
    // `has_credential`, and the row that has none says so too.
    expect(cardOf("vault-scrape on pi-loft").getByText(/a credential is stored/i)).toBeInTheDocument()
    expect(cardOf("metrics-prom on pi-loft").getByText(/no credential stored/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Add an integration to pi-loft" }))
    const add = within(await screen.findByRole("dialog"))
    await userEvent.type(add.getByRole("textbox", { name: "Name" }), "loki-tail")
    await userEvent.type(add.getByRole("textbox", { name: "URL" }), "https://loki.loft:3100")
    await retype(add.getByRole("textbox", { name: "Fields (JSON)" }), '{"lines": {"query": "up"}}')

    // A credential can only be stored on a disabled row, so the form says so and
    // the switch is turned off before one is typed.
    await userEvent.click(add.getByRole("switch", { name: "Enabled" }))
    await userEvent.click(add.getByRole("combobox", { name: "Credential" }))
    await userEvent.click(screen.getByRole("option", { name: "Store a credential" }))
    await userEvent.type(add.getByLabelText("Credential to store"), NEW_TOKEN)
    await userEvent.click(add.getByRole("button", { name: "Add the integration" }))

    // It went in, once, in its own field -- not inside `config`, which is
    // exported in plain text beside the database.
    await waitFor(() => expect(posted).toHaveLength(1))
    expect((posted[0] as { credential: string }).credential).toBe(NEW_TOKEN)
    expect(JSON.stringify((posted[0] as { config: unknown }).config)).not.toContain(NEW_TOKEN)

    // And it never comes back. The dialog is gone, the list has been re-read,
    // and the row that holds it says only that one is stored.
    const created = await screen.findByRole("region", { name: "loki-tail on pi-loft" })
    expect(within(created).getByText(/a credential is stored/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    appearsNowhere(NEW_TOKEN, said)
  })

  it("clears a stored credential with an empty string, and leaves it alone otherwise", async () => {
    // The defect this whole page is arranged around. `credential` is write-only
    // and its three states are distinct: absent leaves the stored secret alone,
    // `""` clears it, a value replaces it. A form that sent every field every
    // time would put `credential: ""` in an unrelated rename and wipe a secret
    // nobody touched.
    const patched: unknown[] = []
    let rows = INTEGRATIONS
    server.use(
      ...reading(() => rows),
      http.patch("/api/integrations/:integration_id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        patched.push({ id: params.integration_id, body })
        // Built over the row **as it is now**, not over the fixture: the second
        // edit below follows the first, and a handler that rebuilt from VAULT
        // would quietly undo the rename and hide it as a missing card.
        const current = rows.find((row) => String(row.id) === String(params.integration_id))
        const edited = {
          ...(current ?? VAULT),
          ...body,
          has_credential: Object.hasOwn(body, "credential")
            ? body.credential !== ""
            : (current ?? VAULT).has_credential,
        } as Integration
        rows = rows.map((row) => (row.id === VAULT.id ? edited : row))
        return HttpResponse.json(edited)
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "vault-scrape on pi-loft" })

    // First: an edit that has nothing to do with the credential. The stored
    // secret is left alone, and the only way to say that on the wire is to send
    // no `credential` key at all.
    await userEvent.click(screen.getByRole("button", { name: "Edit vault-scrape" }))
    const rename = within(await screen.findByRole("dialog"))
    const named = rename.getByRole("textbox", { name: "Name" })
    await userEvent.clear(named)
    await userEvent.type(named, "vault-probe")
    // Renaming is not free: a panel's binding names this integration, so the
    // form says what a rename does before it is done.
    expect(rename.getByText(/renaming it breaks every binding that names it/i)).toBeInTheDocument()
    await userEvent.click(rename.getByRole("button", { name: "Save the integration" }))

    await waitFor(() => expect(patched).toEqual([{ id: "4", body: { name: "vault-probe" } }]))
    expect(Object.hasOwn((patched[0] as { body: object }).body, "credential")).toBe(false)
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "vault-probe on pi-loft" })).toBeInTheDocument(),
    )
    expect(cardOf("vault-probe on pi-loft").getByText(/a credential is stored/i)).toBeInTheDocument()

    // Then: the clear, which is a different request and says so explicitly.
    await userEvent.click(screen.getByRole("button", { name: "Edit vault-probe" }))
    const clearing = within(await screen.findByRole("dialog"))
    await userEvent.click(clearing.getByRole("combobox", { name: "Credential" }))
    await userEvent.click(screen.getByRole("option", { name: "Remove the stored credential" }))
    // Removing it is the only change, so there is something to save -- and what
    // goes on the wire is the empty string and nothing else.
    await userEvent.click(clearing.getByRole("button", { name: "Save the integration" }))

    await waitFor(() => expect(patched).toHaveLength(2))
    expect(patched[1]).toEqual({ id: "4", body: { credential: "" } })

    await waitFor(() =>
      expect(cardOf("vault-probe on pi-loft").getByText(/no credential stored/i)).toBeInTheDocument(),
    )
  })

  it("renders the refusal when an enabled integration would hold a credential", async () => {
    // Not a hypothetical and not a shape the form can prevent: the request that
    // provokes it carries no credential at all. `_credential_has_somewhere_to_go`
    // is about the state the edit *leaves behind*, so turning a row that already
    // holds one back on is exactly it. Left to `build_snapshot` instead, this
    // would be an edit that saves, blocks every later edit to the rack, and
    // reports itself as a snapshot error naming a table.
    const REFUSAL =
      "no integration type can carry a credential to a daemon yet, so an enabled integration" +
      " holding one would block this rack's configuration entirely. Store it on a disabled" +
      " integration, or leave it out."
    const patched: unknown[] = []
    server.use(
      ...reading(() => INTEGRATIONS),
      http.patch("/api/integrations/:integration_id", async ({ request }) => {
        patched.push(await request.json())
        // A plain-string detail, not the list shape a pydantic validation report
        // takes: this refusal is raised as an `HTTPException`, so `detailFrom`
        // reads the string branch.
        return HttpResponse.json({ detail: REFUSAL }, { status: 422 })
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "vault-scrape on pi-loft" })
    await userEvent.click(screen.getByRole("button", { name: "Edit vault-scrape" }))
    const dialog = within(await screen.findByRole("dialog"))

    // Said before it is tried, at the switch, because the row already holds one.
    expect(dialog.getByText(/holds a credential, so enabling it will be refused/i)).toBeInTheDocument()
    await userEvent.click(dialog.getByRole("switch", { name: "Enabled" }))
    await userEvent.click(dialog.getByRole("button", { name: "Save the integration" }))

    // One field on the wire, and the refusal is still about the credential.
    await waitFor(() => expect(patched).toEqual([{ enabled: true }]))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent(REFUSAL)
    // The server's own sentence, which is the one that carries the remedy --
    // not the generic apology `useMutate` falls back to when there is no detail.
    expect(refusal).toHaveTextContent("Store it on a disabled integration, or leave it out.")
    expect(refusal).not.toHaveTextContent(/the server refused the change/i)

    // The dialog stays up holding what was set, so the remedy is one click away
    // rather than a form to fill in again.
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(dialog.getByRole("switch", { name: "Enabled" })).toBeChecked()
  })

  it("labels the test button a reachability check rather than a preview", async () => {
    // `POST /api/integrations/{id}/test` dry-runs each field's query and reports
    // the **first sample** of the result vector. The daemon's reduce, label and
    // strip are deliberately not reimplemented server-side, so what comes back
    // is not what the panel will draw -- and an interface that called this a
    // preview would be claiming it is.
    const tested: string[] = []
    server.use(
      ...reading(() => INTEGRATIONS),
      http.post("/api/integrations/:integration_id/test", ({ params }) => {
        tested.push(String(params.integration_id))
        return HttpResponse.json({
          ok: false,
          fields: [
            { name: "cpu", ok: true, value: "0.41", error: null },
            { name: "disk", ok: false, value: null, error: "the query matched no series" },
          ],
        })
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "metrics-prom on pi-loft" })
    const card = cardOf("metrics-prom on pi-loft")
    // The label names what it does. Nothing anywhere offers a preview.
    expect(screen.queryByRole("button", { name: /preview/i })).not.toBeInTheDocument()
    await userEvent.click(card.getByRole("button", { name: "Check metrics-prom is reachable" }))

    // The integration the button named, and not the other row on the same rack.
    await waitFor(() => expect(tested).toEqual(["17"]))

    expect(await card.findByText("Reachability check")).toBeInTheDocument()
    // Each field, by name, with what came back said as what it is. Read out of
    // the report's own list rather than off the card, which already names the
    // same fields two lines above.
    const reported = within(card.getByRole("list", { name: "Reachability check" }))
    expect(
      reported.getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["cpu — answered, first sample 0.41", "disk — the query matched no series"])
    // And the sentence that stops it being read as a preview, naming the three
    // things the rack applies and this does not.
    const caveat = card.getByText(/not a preview/i)
    expect(caveat).toHaveTextContent(/reduce/)
    expect(caveat).toHaveTextContent(/label/)
    expect(caveat).toHaveTextContent(/strip/)
    // No picture of a panel here either: this route renders nothing.
    expect(card.queryByRole("img")).not.toBeInTheDocument()
  })

  it("refuses a URL carrying a credential without repeating the password", async () => {
    const said = watchConsole()
    const REFUSAL =
      "config.url carries a credential in a URL. `config` is exported in plain text beside the" +
      " database; send it as `credential` instead, which is encrypted."
    const patched: Record<string, unknown>[] = []
    server.use(
      ...reading(() => INTEGRATIONS),
      // The server's own refusal, which deliberately does not quote the value:
      // the reason for the refusal is the *shape* of the string, and the string
      // is the thing that may be a password.
      http.patch("/api/integrations/:integration_id", async ({ request }) => {
        patched.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ detail: REFUSAL }, { status: 422 })
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "metrics-prom on pi-loft" })
    await userEvent.click(screen.getByRole("button", { name: "Edit metrics-prom" }))
    const dialog = within(await screen.findByRole("dialog"))

    const url = dialog.getByRole("textbox", { name: "URL" })
    await userEvent.clear(url)
    await userEvent.type(url, URL_WITH_CREDENTIAL)
    await userEvent.click(dialog.getByRole("button", { name: "Save the integration" }))

    const refusal = await screen.findByRole("alert")
    expect(refusal).toHaveTextContent(REFUSAL)
    expect(refusal).not.toHaveTextContent(/the server refused the change/i)

    // `config` is one column and a PATCH replaces all of it, so the body carries
    // the whole document -- built over the one that was there, not out of the
    // three controls this form offers. Dropping the tunnel would not merely lose
    // a setting: a tunnelled integration takes its base URL from the tunnel, so
    // it would move where the rack polls.
    await waitFor(() => expect(patched).toHaveLength(1))
    expect(patched[0]).toEqual({
      config: {
        ...METRICS.config,
        url: URL_WITH_CREDENTIAL,
      },
    })

    // The address is kept, minus the part that must not be in it, and the form
    // says what happened rather than leaving the change to be noticed.
    await waitFor(() => expect(dialog.getByRole("textbox", { name: "URL" })).toHaveValue(
      "https://prom.loft:9090/",
    ))
    expect(dialog.getByText(/taken out of the address/i)).toBeInTheDocument()

    // The whole point: the password is in no message, in no retained value, in
    // no console line, and in no attribute of the document.
    appearsNowhere(URL_PASSWORD, said)
    appearsNowhere(URL_WITH_CREDENTIAL, said)
  })

  it("deletes the integration it named, and says which racks did not get the removal", async () => {
    const deleted: string[] = []
    let rows = INTEGRATIONS
    server.use(
      ...reading(() => rows),
      http.delete("/api/integrations/:integration_id", ({ params }) => {
        deleted.push(String(params.integration_id))
        rows = rows.filter((row) => String(row.id) !== String(params.integration_id))
        // The default 200 carrying the id it removed. No M3a route answers 204.
        return HttpResponse.json(
          { deleted: Number(params.integration_id) },
          { headers: { "X-Unservable-Daemons": String(UNLISTED_RACK) } },
        )
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "vault-scrape on pi-loft" })
    await userEvent.click(screen.getByRole("button", { name: "Delete vault-scrape" }))

    const dialog = within(await screen.findByRole("dialog"))
    expect(dialog.getByRole("heading", { name: "Delete vault-scrape?" })).toBeInTheDocument()
    // What goes with it, said before it goes: the encrypted secret is deleted
    // with the row, because nothing else references it and no route could reach
    // it afterwards.
    expect(dialog.getByText(/stored credential is deleted with it/i)).toBeInTheDocument()
    await userEvent.click(dialog.getByRole("button", { name: "Delete the integration" }))

    // Id 4, which is the row the confirmation named -- not 17, and not an index.
    await waitFor(() => expect(deleted).toEqual(["4"]))
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "vault-scrape on pi-loft" })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole("region", { name: "metrics-prom on pi-loft" })).toBeInTheDocument()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    // The answer outlives the card it removed, which is why it is drawn above
    // the list rather than on the row.
    const notice = screen.getByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("rack 63")
    expect(notice).not.toHaveTextContent("pi-loft")
  })

  it("says nothing about a rack's integrations until they have been read", async () => {
    // The defect this milestone has now fixed twice -- task 13 on ScreensPage,
    // task 14 on TemplatesPage -- and it is the same rule a third time: "this
    // rack polls nothing" is a definite claim about a request, and a fetch in
    // flight and a fetch that failed both look exactly like an empty list when
    // it is read as `data ?? []`.
    let release: (response: Response) => void = () => {}
    const held = new Promise<Response>((resolve) => {
      release = resolve
    })
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () => HttpResponse.json(RACKS)),
      http.get("/api/integrations", ({ request }) => {
        const asked = new URL(request.url).searchParams.get("daemon_id")
        if (asked === String(LOFT)) return held
        return HttpResponse.json([CELLAR_NODE])
      }),
    )
    renderApp({ at: "/integrations" })

    // The other rack's section is up, because its request answered.
    await screen.findByRole("region", { name: "cellar-node on pi-cellar" })
    expect(screen.getByText(/Reading the integrations on pi-loft/)).toBeInTheDocument()
    expect(screen.queryByText(/pi-loft polls nothing yet/i)).not.toBeInTheDocument()
    // The gate is exactly as wide as the claim: adding one needs no listing.
    expect(screen.getByRole("button", { name: "Add an integration to pi-loft" })).toBeInTheDocument()

    release(HttpResponse.json({ detail: "the integration table could not be read" }, { status: 500 }))

    expect(
      await screen.findByText("the integration table could not be read"),
    ).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText(/Reading the integrations on pi-loft/)).not.toBeInTheDocument(),
    )
    // And a request that failed is still not an empty rack.
    expect(screen.queryByText(/pi-loft polls nothing yet/i)).not.toBeInTheDocument()
  })
})
