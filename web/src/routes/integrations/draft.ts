import type { Integration, IntegrationBody, NewIntegration } from "@/api/queries"

/**
 * `PrometheusConfig.timeout`'s own default, and `create_integration`'s fallback
 * poll interval (`body.poll_interval or 5.0`).
 *
 * Written here because a new integration's form has to start somewhere, and the
 * two numbers the server would have chosen are the only defensible answer: any
 * other value would be this interface quietly disagreeing with the model it is
 * filling in.
 */
export const DEFAULT_TIMEOUT = 4
export const DEFAULT_POLL_INTERVAL = 5

/**
 * The three states `credential` has on the wire, as three things a person can choose.
 *
 * This is the whole reason the form does not simply hold a text box. `credential`
 * is write-only and **absent, `""` and a value mean three different things**:
 * leave the stored secret alone, clear it, replace it. A single input cannot
 * express the first two -- an empty box is indistinguishable from an untouched
 * one -- so a form built on one would either never be able to clear a secret, or
 * would send `credential: ""` every time and wipe a stored secret on an
 * unrelated rename. That is the defect this page exists not to have, so the
 * choice is made explicit and named in words the user reads before saving.
 */
export type CredentialChoice = "keep" | "clear" | "set"

/** What the form holds while it is open. Text, because a box holds text. */
export type Draft = {
  name: string
  url: string
  /** `PrometheusConfig.timeout`, in seconds, as typed. */
  timeout: string
  /** `PrometheusConfig.fields`, as a JSON document. See `IntegrationForm`. */
  fields: string
  pollInterval: string
  enabled: boolean
  credential: CredentialChoice
  /** The plaintext, held only while this form is open and never sent anywhere else. */
  secret: string
}

/** The three `config` keys this form edits. Everything else is carried through. */
const EDITED_CONFIG_KEYS = ["url", "timeout", "fields"]

/**
 * The `config` keys a row carries that this form does not edit.
 *
 * Named on screen rather than hidden, because they are written back untouched
 * and a form that silently round-trips a `tunnel` block is a form whose user
 * does not know the tunnel is what decides where the rack polls.
 */
export function carriedKeys(config: Record<string, unknown>): string[] {
  return Object.keys(config).filter((key) => !EDITED_CONFIG_KEYS.includes(key))
}

function textOf(value: unknown): string {
  return value === undefined || value === null ? "" : String(value)
}

/**
 * The form as it stands when it opens: this row, or an empty one.
 *
 * `credential` always starts at `keep`, on a row that holds one and on a row
 * that does not, because "keep" is the state that changes nothing -- opening a
 * form must not be an instruction.
 */
export function draftFrom(integration?: Integration): Draft {
  const config = (integration?.config ?? {}) as Record<string, unknown>
  return {
    name: integration?.name ?? "",
    url: textOf(config.url),
    timeout: integration === undefined ? String(DEFAULT_TIMEOUT) : textOf(config.timeout),
    fields: config.fields === undefined ? "" : JSON.stringify(config.fields, null, 2),
    pollInterval: String(integration?.poll_interval ?? DEFAULT_POLL_INTERVAL),
    // `create_integration` reads a missing `enabled` as true, so a new row starts
    // where the server would put it.
    enabled: integration?.enabled ?? true,
    credential: "keep",
    secret: "",
  }
}

/**
 * The field map, or `null` because what was typed is not one.
 *
 * An empty object is refused as well as unparseable text: `PrometheusConfig`
 * declares `fields` with `min_length=1`, because "an integration that queries
 * nothing polls a server for no reason and can feed no screen". A list is
 * refused because `fields` is a mapping of name to spec, and `JSON.parse` reads
 * both a list and `null` as an `object`.
 */
export function parseFields(text: string): Record<string, unknown> | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null
  if (Object.keys(parsed).length === 0) return null
  return parsed as Record<string, unknown>
}

/**
 * A positive number, or `null`.
 *
 * The emptiness test is not redundant with the comparison: `Number("")` is `0`
 * and `Number("  ")` is `0`, so a box somebody cleared would otherwise arrive as
 * a real zero -- which `poll_interval` (`gt=0`) and `timeout` (`gt=0`) both
 * refuse, from the far end of a request that did not need to be made.
 */
export function positive(text: string): number | null {
  if (text.trim() === "") return null
  const value = Number(text)
  if (!Number.isFinite(value) || value <= 0) return null
  return value
}

/**
 * The whole `config` document to write, built **over the one that is there**.
 *
 * `config` is one column and a PATCH replaces all of it, so a document assembled
 * from the three controls this form offers would silently delete every other key
 * -- `tunnel` today, and whatever M4's integrations carry. A tunnelled
 * integration takes its base URL from the tunnel at poll time, so dropping that
 * block does not merely lose a setting: it moves where the rack polls.
 */
export function configFrom(
  base: Record<string, unknown>,
  draft: Draft,
): Record<string, unknown> | null {
  const fields = parseFields(draft.fields)
  const timeout = positive(draft.timeout)
  if (fields === null || timeout === null || draft.url.trim() === "") return null
  return { ...base, url: draft.url, timeout, fields }
}

/**
 * What `credential` this body carries, if it carries one at all.
 *
 * Three branches and no fourth, and the absent one is the default: a body built
 * without going through here has no `credential` key, which is the state that
 * leaves a stored secret alone.
 */
function credentialFor(draft: Draft): { credential?: string } {
  if (draft.credential === "clear") return { credential: "" }
  if (draft.credential === "set") return { credential: draft.secret }
  return {}
}

/** A secret was asked for and not typed: `""` here would mean *clear*, not "no change". */
function halfTyped(draft: Draft): boolean {
  return draft.credential === "set" && draft.secret === ""
}

/**
 * Exactly the fields that differ, and never one more -- or `null` if this draft
 * cannot be sent at all.
 *
 * `IntegrationBody` is `extra="forbid"` and the route's PATCH semantics come
 * from `exclude_unset`, so what is *named* is what is written. Two things follow
 * and both have teeth: a body echoing the whole `IntegrationView` back would be
 * a 422 on `id`, `type` and `has_credential`, and -- the one that loses data
 * rather than failing loudly -- a `credential` key in every body means the empty
 * string in every body, which clears the stored secret on every unrelated edit.
 *
 * `config` is compared as the three strings the form holds rather than as
 * documents: a re-serialised `fields` map differs from the server's by key order
 * and whitespace alone, and a deep comparison written here would be a second
 * definition of "changed" that this form's own controls could disagree with.
 */
export function changesFrom(
  integration: Integration,
  initial: Draft,
  draft: Draft,
): IntegrationBody | null {
  if (draft.name.trim() === "" || halfTyped(draft)) return null
  const interval = positive(draft.pollInterval)
  if (interval === null) return null

  const body: IntegrationBody = {}
  if (draft.name !== initial.name) body.name = draft.name
  if (
    draft.url !== initial.url ||
    draft.timeout !== initial.timeout ||
    draft.fields !== initial.fields
  ) {
    const config = configFrom(integration.config as Record<string, unknown>, draft)
    if (config === null) return null
    body.config = config
  }
  if (interval !== integration.poll_interval) body.poll_interval = interval
  if (draft.enabled !== integration.enabled) body.enabled = draft.enabled
  return { ...body, ...credentialFor(draft) }
}

/**
 * A new row, or `null` because the form is not filled in.
 *
 * `type` is sent explicitly: the generated `NewIntegration` requires it --
 * openapi-typescript makes a field with a default required -- and there is
 * exactly one integration type today, so naming it costs nothing and the day a
 * second arrives this stops compiling rather than silently creating the wrong
 * kind.
 *
 * The `credential` key is absent unless one was actually asked for, on a create
 * as much as on an edit. Nothing on the server distinguishes an absent
 * credential from an empty one *here* -- `if body.credential` reads both as no
 * secret -- but a create that always sent the key would be this form practising
 * the habit that wipes a secret one route over.
 */
export function newBodyFrom(daemonId: number, draft: Draft): NewIntegration | null {
  if (draft.name.trim() === "" || halfTyped(draft)) return null
  const interval = positive(draft.pollInterval)
  const config = configFrom({}, draft)
  if (interval === null || config === null) return null
  return {
    daemon_id: daemonId,
    type: "prometheus",
    name: draft.name,
    config,
    poll_interval: interval,
    enabled: draft.enabled,
    ...credentialFor(draft),
  }
}

/**
 * The same address with any userinfo taken out of it, or the string unchanged.
 *
 * Used on a refusal and not before one: the server is what refuses a credential
 * in a URL -- at any depth, in a config this form only partly understands -- and
 * a check here that ran first would either duplicate that rule or, worse,
 * quietly disagree with it. What this is for is the state *after* the refusal.
 * The address the operator typed is worth keeping; the password inside it is not
 * keepable at all, because a retained form value is rendered into the document
 * as an attribute and would then sit in the page for as long as the dialog is
 * open.
 *
 * A string the parser refuses is returned untouched. There is nothing safe to be
 * done with it here, and the server refuses that shape too, with its own
 * sentence.
 */
export function withoutUserinfo(value: string): string {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return value
  }
  if (parsed.username === "" && parsed.password === "") return value
  parsed.password = ""
  parsed.username = ""
  return parsed.toString()
}
