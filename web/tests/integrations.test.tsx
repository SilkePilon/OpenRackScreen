import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Daemon, Integration } from "../src/api/queries"
import { changesFrom, draftFrom, withoutUserinfo } from "../src/routes/integrations/draft"
import { server } from "./msw"
import { renderApp } from "./render"

/**
 * The fixture, and why every number and every name in it is what it is.
 *
 * This page is the one that handles secrets, so the identity trap this project
 * has been bitten by four times is worth spelling out again here:
 *
 *   * **Integration ids, rack ids and intervals are all disjoint.** Ids 4, 17
 *     and 29; racks 8 and 42; poll intervals 12, 30 and 5; timeouts 2.5 and 4,
 *     and one row with no timeout at all. No id is a rack, no interval is an id,
 *     and no id is its own index in the list -- `GET /api/integrations` is
 *     `ORDER BY id`, so the loft's rows arrive 4 then 17 and index 0 holds id 4.
 *   * **No name is a number and no name is a substring of another.** A body
 *     that sent `{name: "4"}` instead of `{name: "vault-scrape"}` is only
 *     catchable if the two are never the same string, and a field name
 *     (`sealed`, `cpu`, `disk`, `temp`) is never an integration name.
 *   * **The unservable header names the one rack these routes can name.** Every
 *     integrations route calls `edit.affects(<that row's daemon_id>)` and
 *     nothing else -- `create_integration:186`, `patch_integration`,
 *     `delete_integration` -- and `_assemble` only ever writes
 *     `edit.unservable[daemon_id]` for a daemon in `edit.daemons()`. So the
 *     unservable set for a write about a rack-8 row **can only be a subset of
 *     {8}**, and a fixture naming 42 or 63 here is a response the server cannot
 *     produce. The header is still the only thing that may be read -- what pins
 *     that is not a rack the body could not have named, it is the *other* create
 *     below, which sets no header at all and must therefore name nobody. A page
 *     reading the body would name pi-loft on both.
 *   * **The two secrets are different strings.** One is typed into the
 *     credential box, the other is buried in a URL's userinfo. A page that
 *     rendered "the credential" back would otherwise be indistinguishable from
 *     one that echoed the URL's password.
 */
const LOFT = 8
const CELLAR = 42

/** Typed into the credential box. Never travels back, and must never be drawn. */
const NEW_TOKEN = "s3cr3t-bearer-9f2"
/** Buried in a URL's userinfo. The server's refusal does not echo it; nor may this page. */
const URL_PASSWORD = "hunter2-loft"
const URL_WITH_CREDENTIAL = `https://admin:${URL_PASSWORD}@prom.loft:9090`
/**
 * The same password in an address `new URL()` cannot read: the IPv6 literal is
 * never closed. The server refuses it too, with its "looks like a URL and cannot
 * be parsed" 422, so it is a state this page reaches by being used -- and a
 * scrub built on the parser hands this one straight back with the password in
 * it.
 */
const UNPARSEABLE_WITH_CREDENTIAL = `https://admin:${URL_PASSWORD}@[fd00::1:9090`

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

/**
 * On the other rack, so "which rack asked" is a question the page has to answer
 * -- and **with no `timeout` in its config at all**.
 *
 * That is not a degenerate document. `PrometheusConfig.timeout` is
 * `Field(default=4.0, gt=0)`, so a config that never names it is one the rack
 * polls with a four-second timeout, and `POST /api/integrations` will store
 * exactly what it was handed. Read into a form as `""` it fails `positive()`,
 * which makes `configFrom` answer `null`, which blocks *every* edit to this row
 * behind a line about what the rack refuses -- of a value the rack would have
 * supplied itself.
 */
const CELLAR_NODE: Integration = {
  id: 29,
  daemon_id: CELLAR,
  type: "prometheus",
  name: "cellar-node",
  config: {
    url: "http://node.cellar:9100",
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
        // 201 **and** a header saying the rack did not get it. The status code
        // cannot stand in for the header: a create answers 201 even when nothing
        // was pushed, because the row does exist.
        //
        // The header names 8 and can name nothing else. `create_integration`
        // calls `edit.affects(body.daemon_id)` and `_assemble` fills
        // `edit.unservable` only from `edit.daemons()`, so the unservable set
        // for this route is a subset of the one rack it was added to. A fixture
        // naming another rack would be asserting against a response that cannot
        // arrive.
        return HttpResponse.json(created, {
          status: 201,
          headers: { "X-Unservable-Daemons": String(LOFT) },
        })
      }),
      http.patch("/api/integrations/:integration_id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        patched.push({ id: params.integration_id, body })
        // Over the row as it is now, so the second edit below follows the first
        // instead of quietly reverting it.
        const current = rows.find((row) => String(row.id) === String(params.integration_id))
        const edited = { ...(current ?? METRICS), ...body } as Integration
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

    // `fields` is a mapping with at least one entry in it: `PrometheusConfig`
    // declares it `min_length=1`, because an integration that queries nothing
    // polls a server for no reason and can feed no screen. Neither of these is
    // that, and `JSON.parse` reads both as an `object`.
    const fields = add.getByRole("textbox", { name: "Fields (JSON)" })
    await retype(fields, "{}")
    expect(add.getByRole("button", { name: "Add the integration" })).toBeDisabled()
    await retype(fields, '["cpu"]')
    expect(add.getByRole("button", { name: "Add the integration" })).toBeDisabled()

    await retype(fields, '{"lines": {"query": "sum(rate(log_lines[5m]))"}}')

    const interval = add.getByRole("spinbutton", { name: "Poll interval (seconds)" })
    await userEvent.clear(interval)
    // Zero seconds is not a rate. `poll_interval` and `timeout` are both `gt=0`
    // on the far end, so a body carrying it buys a 422 for a request that never
    // needed to be made.
    await userEvent.type(interval, "0")
    expect(add.getByRole("button", { name: "Add the integration" })).toBeDisabled()
    expect(add.getByText(/must be above zero/i)).toBeInTheDocument()
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

    // The rack that did not get it, by name. A create on rack 8 that reached
    // nobody is the only unservable answer this route can give, so this is the
    // whole realistic contract: the row exists, the glass does not show it, and
    // the sentence names pi-loft.
    const notice = await screen.findByText(/was saved, but nothing was sent/i)
    expect(notice).toHaveTextContent("pi-loft")
    // What separates "read the header" from "read the rack it was written
    // about" is not this fixture -- both name pi-loft here -- it is the create
    // in the credential test below, which sets no header and must therefore say
    // nothing at all.
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

    // And the other half of "only what moved": an edit that touches nothing but
    // the field map still sends `config`, because `config` is one column and the
    // field map lives inside it. It goes as the whole document, built over the
    // one that was there -- so the tunnel this form cannot edit is still in it,
    // and `poll_interval`, which was not touched this time, is not.
    await userEvent.click(screen.getByRole("button", { name: "Edit metrics-prom" }))
    const again = within(await screen.findByRole("dialog"))
    await retype(
      again.getByRole("textbox", { name: "Fields (JSON)" }),
      '{"cpu": {"query": "node_cpu_ratio", "reduce": "top"}}',
    )
    await userEvent.click(again.getByRole("button", { name: "Save the integration" }))

    await waitFor(() => expect(patched).toHaveLength(2))
    expect(patched[1]).toEqual({
      id: "17",
      body: {
        config: {
          ...METRICS.config,
          fields: { cpu: { query: "node_cpu_ratio", reduce: "top" } },
        },
      },
    })
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
    // Nothing typed yet, so nothing would be stored: the server's own test is
    // `bool(body.credential)` and an empty string is false there, so a warning
    // now would be this form predicting a refusal that would not happen.
    expect(add.queryByText(/enabling it will be refused/i)).not.toBeInTheDocument()

    await userEvent.type(add.getByLabelText("Credential to store"), NEW_TOKEN)
    // And now it would. The remedy it names is a choice that is **on this
    // screen**: a create holds no credential, so there is no "Remove the stored
    // credential" option to point at -- the select offers only "No credential"
    // and "Store a credential" on a row with nothing on file.
    const warning = add.getByText(/enabling it will be refused/i)
    expect(warning).toHaveTextContent("choose No credential")
    expect(warning).not.toHaveTextContent(/remove the stored credential/i)
    expect(add.queryByRole("option", { name: /remove the stored credential/i })).not.toBeInTheDocument()

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
    expect(screen.getByText("loki-tail was added.")).toBeInTheDocument()
    // This create set **no** `X-Unservable-Daemons`, so no rack is named -- and
    // that is what says the notice is read out of the header rather than out of
    // the rack the write was about. A page that named `daemon_id` would say
    // pi-loft here and in the create test above alike, and only one of the two
    // would catch it.
    expect(screen.queryByText(/was saved, but nothing was sent/i)).not.toBeInTheDocument()
    appearsNowhere(NEW_TOKEN, said)

    // Opening the form again starts from an empty one, and here that is a rule
    // about the credential before it is one about convenience: an abandoned or
    // finished attempt must not leave a plaintext secret sitting in a box for
    // whoever is next at this browser. The last create's answer goes with it --
    // it is an answer about a write that has already happened, and left standing
    // over a fresh form it reads as this one's.
    await userEvent.click(screen.getByRole("button", { name: "Add an integration to pi-loft" }))
    const again = within(await screen.findByRole("dialog"))
    expect(again.getByRole("textbox", { name: "Name" })).toHaveValue("")
    expect(again.queryByLabelText("Credential to store")).not.toBeInTheDocument()
    expect(screen.queryByText("loki-tail was added.")).not.toBeInTheDocument()
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
    expect(cardOf("vault-probe on pi-loft").getByText("vault-probe was saved.")).toBeInTheDocument()

    // Then: the clear, which is a different request and says so explicitly.
    await userEvent.click(screen.getByRole("button", { name: "Edit vault-probe" }))
    const clearing = within(await screen.findByRole("dialog"))
    // Setting up the next edit clears what the last one answered, for the reason
    // the delete and the create do: it is about a write that has happened.
    expect(screen.queryByText("vault-probe was saved.")).not.toBeInTheDocument()

    // The half-typed replacement, which is the same wipe wearing a different
    // hat: "replace it" with an empty box builds `credential: ""`, which is the
    // instruction to remove the stored secret. It is refused here rather than
    // sent, because nothing downstream could tell the two apart.
    await userEvent.click(clearing.getByRole("combobox", { name: "Credential" }))
    await userEvent.click(screen.getByRole("option", { name: "Replace the stored credential" }))
    expect(clearing.getByRole("button", { name: "Save the integration" })).toBeDisabled()
    expect(clearing.getByText(/an empty one is the instruction to remove/i)).toBeInTheDocument()

    await userEvent.click(clearing.getByRole("combobox", { name: "Credential" }))
    await userEvent.click(screen.getByRole("option", { name: "Remove the stored credential" }))
    // Once it is being removed, this edit leaves no credential behind, so
    // enabling it is no longer the thing that would be refused. The warning is
    // about the state the edit leaves and it goes when that state does; a
    // warning that stayed would be telling somebody a legal edit will fail.
    expect(clearing.queryByText(/enabling it will be refused/i)).not.toBeInTheDocument()
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
    // The server's own sentence, which is the one that carries the remedy --
    // not the generic apology `useMutate` falls back to when there is no detail
    // -- and *only* that sentence: `toBe` rather than `toHaveTextContent`,
    // because anything this page appended would be this page explaining a
    // refusal it did not make.
    expect(refusal.textContent).toBe(REFUSAL)
    expect(refusal).toHaveTextContent("Store it on a disabled integration, or leave it out.")

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
        // `TestReport.ok` is `all(report.ok for report in reports)`, so this one
        // is false. The third field is the shape `FieldReport` permits and this
        // server does not currently produce: `ok: true` with a null `value`.
        // `value` is `str | None` on the wire and this page is written against
        // the wire, and `${field.value ?? ""}` rendered it as a sentence that
        // stops mid-word -- "answered, first sample " -- which reads as a sample
        // that came back blank rather than as one that was not reported.
        return HttpResponse.json({
          ok: false,
          fields: [
            { name: "cpu", ok: true, value: "0.41", error: null },
            { name: "disk", ok: false, value: null, error: "the query matched no series" },
            { name: "temp", ok: true, value: null, error: null },
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
    ).toEqual([
      "cpu — answered, first sample 0.41",
      "disk — the query matched no series",
      // Not "answered, first sample " with nothing after it.
      "temp — answered, and reported no sample",
    ])
    // The server's own summary, drawn rather than discarded. `TestReport.ok` is
    // the answer to the question the button asks -- is this thing reachable --
    // and the per-field list is that answer only after somebody has counted it.
    expect(card.getByText(/Not every field answered/i)).toBeInTheDocument()
    expect(card.queryByText("Every field answered.")).not.toBeInTheDocument()
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

    // Exactly the server's sentence and nothing else. `toBe` and not
    // `toHaveTextContent`: what a page appends to a refusal about an address is
    // most naturally the address, and the address is the thing holding the
    // password.
    const refusal = await screen.findByRole("alert")
    expect(refusal.textContent).toBe(REFUSAL)

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

    // The address is kept **exactly as typed** minus the part that must not be
    // in it -- no trailing slash appears and no port is normalised away, because
    // nothing re-serialises it -- and the form says what happened rather than
    // leaving the change to be noticed.
    await waitFor(() => expect(dialog.getByRole("textbox", { name: "URL" })).toHaveValue(
      "https://prom.loft:9090",
    ))
    expect(dialog.getByText(/taken out of the address/i)).toBeInTheDocument()

    // The whole point: the password is in no message, in no retained value, in
    // no console line, and in no attribute of the document.
    appearsNowhere(URL_PASSWORD, said)
    appearsNowhere(URL_WITH_CREDENTIAL, said)

    // The note is about *that* address, so editing the address ends it: left
    // standing it replaces the ordinary "no user or password in it" hint over an
    // address it was never about, which is the hint most worth having in front
    // of somebody who is typing a new one.
    await userEvent.type(dialog.getByRole("textbox", { name: "URL" }), "/")
    expect(dialog.queryByText(/taken out of the address/i)).not.toBeInTheDocument()
    expect(dialog.getByText(/no user or password in it/i)).toBeInTheDocument()

    // And again for the shape with no user in it, which is how a bearer token
    // ends up in an address: `https://:token@host`. It is userinfo all the same,
    // the server refuses it the same way, and a check written as "there is a
    // username *and* a password" would let this one straight through.
    const second = dialog.getByRole("textbox", { name: "URL" })
    await userEvent.clear(second)
    await userEvent.type(second, `https://:${URL_PASSWORD}@prom.loft:9090`)
    await userEvent.click(dialog.getByRole("button", { name: "Save the integration" }))

    await waitFor(() => expect(patched).toHaveLength(2))
    await waitFor(() =>
      expect(dialog.getByRole("textbox", { name: "URL" })).toHaveValue("https://prom.loft:9090"),
    )
    appearsNowhere(URL_PASSWORD, said)

    // **And the shape no parser will read.** `new URL("https://admin:pw@[fd00::1:9090")`
    // throws -- the IPv6 literal is never closed -- and the server refuses it
    // too, with its "looks like a URL and cannot be parsed" 422. A scrub written
    // on the parser returns this one *unchanged*, the caller sees nothing to do
    // and leaves the note off, and the password then sits in the box as a
    // `value` attribute under a hint that reads "no user or password in it".
    // That is the one branch where this page's own guarantee did not hold, so it
    // is asserted rather than reasoned about.
    await retype(dialog.getByRole("textbox", { name: "URL" }), UNPARSEABLE_WITH_CREDENTIAL)
    await userEvent.click(dialog.getByRole("button", { name: "Save the integration" }))

    await waitFor(() => expect(patched).toHaveLength(3))
    await waitFor(() =>
      expect(dialog.getByRole("textbox", { name: "URL" })).toHaveValue("https://[fd00::1:9090"),
    )
    // Said, not silently done: the same note fires here as on an address that
    // parsed, because one rule covers both.
    expect(dialog.getByText(/taken out of the address/i)).toBeInTheDocument()
    appearsNowhere(URL_PASSWORD, said)
    appearsNowhere(UNPARSEABLE_WITH_CREDENTIAL, said)
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
        // `delete_integration` calls `edit.affects(row["daemon_id"])` and
        // nothing else, so 8 is the only id this header can carry here, for the
        // same reason the create's can carry only 8.
        return HttpResponse.json(
          { deleted: Number(params.integration_id) },
          { headers: { "X-Unservable-Daemons": String(LOFT) } },
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
    expect(notice).toHaveTextContent("pi-loft")
    expect(notice).not.toHaveTextContent("pi-cellar")

    // Setting up the next delete clears what the last one said. "Not every rack
    // was given that change" names racks against a delete that has happened, and
    // left standing over the dialog for the next one it reads as an answer about
    // that one instead.
    await userEvent.click(screen.getByRole("button", { name: "Delete metrics-prom" }))
    await screen.findByRole("dialog")
    expect(screen.queryByText(/was saved, but nothing was sent/i)).not.toBeInTheDocument()
  })

  it("edits a row whose stored config never named a timeout", async () => {
    // `PrometheusConfig.timeout` is `Field(default=4.0, gt=0)`, so a stored
    // config is allowed not to have the key -- and `POST /api/integrations`
    // stores the document it was handed, so this is a row anything but this form
    // can create. Read into the form as `""` it fails `positive()`, which makes
    // `configFrom` answer `null`, which disables Save for **any** edit to the
    // row -- a URL, a query, a rename -- under "both must be above zero; the
    // rack refuses anything else". The rack refuses no such thing: it would have
    // polled with four seconds.
    const patched: unknown[] = []
    let rows = INTEGRATIONS
    server.use(
      ...reading(() => rows),
      http.patch("/api/integrations/:integration_id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        patched.push({ id: params.integration_id, body })
        const current = rows.find((row) => String(row.id) === String(params.integration_id))
        const edited = { ...(current ?? CELLAR_NODE), ...body } as Integration
        rows = rows.map((row) => (row.id === CELLAR_NODE.id ? edited : row))
        return HttpResponse.json(edited)
      }),
    )
    renderApp({ at: "/integrations" })

    await screen.findByRole("region", { name: "cellar-node on pi-cellar" })
    await userEvent.click(screen.getByRole("button", { name: "Edit cellar-node" }))
    const dialog = within(await screen.findByRole("dialog"))

    // The box opens on the number the rack would have used, which is the only
    // defensible thing to put in it: any other value would be this interface
    // quietly disagreeing with the model it is filling in.
    expect(dialog.getByRole("spinbutton", { name: "Query timeout (seconds)" })).toHaveValue(4)
    expect(dialog.queryByText(/must be above zero/i)).not.toBeInTheDocument()

    const url = dialog.getByRole("textbox", { name: "URL" })
    await userEvent.clear(url)
    await userEvent.type(url, "http://node.cellar:9101")
    // The edit that was blocked. Nothing about the timeout was touched.
    expect(dialog.getByRole("button", { name: "Save the integration" })).toBeEnabled()
    await userEvent.click(dialog.getByRole("button", { name: "Save the integration" }))

    // `config` is one column, so the whole document goes -- and it names the
    // timeout the rack was already using, which leaves the document meaning what
    // it meant.
    await waitFor(() => expect(patched).toEqual([
      {
        id: "29",
        body: {
          config: {
            url: "http://node.cellar:9101",
            timeout: 4,
            fields: { temp: { query: "node_hwmon_temp" } },
          },
        },
      },
    ]))
  })

  it("does not say a server has no racks while it is still asking", async () => {
    // The page-level half of the same rule. "No racks are paired yet" is a claim
    // about `GET /api/daemons`, and this page consumes that list as
    // `racks.data ?? []` -- which is also what a fetch in flight and a fetch
    // that failed both look like. Left ungated it is the *only* thing on the
    // page for a server whose daemon table 500s, sitting under an alert saying
    // the list could not be read.
    server.use(
      SIGNED_IN,
      http.get("/api/daemons", () =>
        HttpResponse.json({ detail: "the daemon table could not be read" }, { status: 500 }),
      ),
    )
    renderApp({ at: "/integrations" })

    expect(await screen.findByText("The racks could not be read")).toBeInTheDocument()
    expect(screen.getByText("the daemon table could not be read")).toBeInTheDocument()
    expect(screen.queryByText(/No racks are paired yet/i)).not.toBeInTheDocument()
    // And no section asked for a rack's integrations, which is why there is no
    // handler for that route here: `onUnhandledRequest: "error"` would say so.
    expect(screen.queryByRole("region")).not.toBeInTheDocument()
  })

  it("says a server has no racks once it has been told so", async () => {
    server.use(SIGNED_IN, http.get("/api/daemons", () => HttpResponse.json([])))
    renderApp({ at: "/integrations" })

    expect(await screen.findByText(/No racks are paired yet/i)).toBeInTheDocument()
    // The remedy names a page that exists. There is no pairing anywhere on this
    // one, so "add a rack" would send the reader hunting for a button nobody
    // has built here.
    expect(screen.getByText(/Pair one on the Daemons page/i)).toBeInTheDocument()
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

/**
 * The two decisions that cannot be driven through the page.
 *
 * `changesFrom` is asked about a row that moved *while a dialog was open*, and
 * nothing in this interface can make that happen on demand: a Radix dialog is
 * modal, so no button behind it can be clicked to provoke a refetch, and the
 * query client these tests build has no window-focus refetch to fire. Left as a
 * component test the divergence would be reasoned about and not asserted.
 *
 * `withoutUserinfo` is exercised through the page as well -- three shapes, in
 * the refusal test above -- and it is also the one function on this page whose
 * job is a security property, so its own table is worth the lines.
 */
describe("what the form decides before it sends anything", () => {
  it("compares against the draft it opened with, not the row as it now stands", () => {
    // The state that differs: a refetch lands under an open dialog and moves the
    // row. `initial` is the snapshot this form was filled from and every other
    // comparison in `changesFrom` is against it; `poll_interval` and `enabled`
    // were against the live prop, so the moment the two disagree an interval
    // nobody typed and a switch nobody touched go on the wire -- and the wire is
    // where they win, because a PATCH names what it writes.
    const initial = draftFrom(METRICS)
    const moved: Integration = { ...METRICS, poll_interval: 300, enabled: false }
    const typed = { ...initial, name: "metrics-relay" }

    expect(changesFrom(moved, initial, typed)).toEqual({ name: "metrics-relay" })
    // And it still sends one that *was* typed, so this is not "never send them".
    expect(changesFrom(moved, initial, { ...initial, pollInterval: "90" })).toEqual({
      poll_interval: 90,
    })
  })

  it("takes userinfo out of an address whether or not a parser will read it", () => {
    // One textual rule and no parser branch. The third row is the one the
    // parser-based version got wrong: `new URL()` throws on the unclosed IPv6
    // literal, so it handed the string back with the password still in it.
    expect(withoutUserinfo("http://prom.loft:9090")).toBe("http://prom.loft:9090")
    expect(withoutUserinfo(`https://admin:${URL_PASSWORD}@prom.loft:9090`)).toBe(
      "https://prom.loft:9090",
    )
    expect(withoutUserinfo(UNPARSEABLE_WITH_CREDENTIAL)).toBe("https://[fd00::1:9090")
    // No username, which is how a bearer token ends up in an address.
    expect(withoutUserinfo(`https://:${URL_PASSWORD}@prom.loft:9090`)).toBe(
      "https://prom.loft:9090",
    )
    // Not the whole authority: an `@` after the authority ends is not userinfo,
    // and eating up to it would delete the host. `[^/?#]*` is what stops it, and
    // `/`, `?` and `#` are the three delimiters RFC 3986 ends an authority on.
    expect(withoutUserinfo("https://prom.loft:9090/api/v1/@scope")).toBe(
      "https://prom.loft:9090/api/v1/@scope",
    )
    expect(withoutUserinfo("https://prom.loft:9090/q?to=ops@loft")).toBe(
      "https://prom.loft:9090/q?to=ops@loft",
    )
    // And a string with no authority at all is not an address this can improve.
    expect(withoutUserinfo("prom.loft:9090")).toBe("prom.loft:9090")
  })
})
