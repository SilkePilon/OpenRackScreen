import { useId } from "react"

import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { parseFields, positive, type CredentialChoice, type Draft } from "@/routes/integrations/draft"

/**
 * The one form both dialogs render, and the three decisions inside it.
 *
 * **1. `config` is edited in part, and the rest is carried through.** A
 * Prometheus integration's config is `url`, `timeout`, `fields` and an optional
 * `tunnel` block, where `fields` is a map of name to `{query, reduce, label,
 * strip}`. The two scalars get a control each -- `url` above all, because that
 * is the key an operator reaches for and the key a credential gets smuggled into
 * -- and `fields` gets a JSON box. That is an honest trade and not a good one:
 * what a user can do here is add, remove and retune queries, including `reduce`,
 * `label` and `strip`, which no other control in this interface offers; what
 * they cannot do is be told which key they mistyped before the server tells
 * them, and a JSON box is a poor place to write a PromQL query with quotes in
 * it. A real field editor is a task of its own. Every key this form does not
 * name -- `tunnel` today -- is preserved verbatim and listed below, because a
 * PATCH replaces the whole document and a form that rebuilt it from its own
 * controls would delete the tunnel and thereby move where the rack polls.
 *
 * **2. The credential is a choice, not a box.** See `CredentialChoice`: absent,
 * `""` and a value are three different instructions on the wire, and a lone text
 * input can only express two of them. The options are worded as the actions they
 * are, and they differ depending on whether a secret is already stored, so
 * nobody has to work out what an empty box would do.
 *
 * **3. What cannot be stored is said where it would be stored.** No integration
 * type can carry a credential to a rack in M3a, so `build_snapshot` refuses an
 * *enabled* row that holds one and `_credential_has_somewhere_to_go` gives that
 * answer at the request instead. The warning is here, at the switch and at the
 * credential, but the save is **not** disabled: the rule belongs to the server,
 * it is about the state the edit leaves behind rather than about the fields this
 * form can see, and a client-side copy of it is a second rule to keep in step.
 */
export function IntegrationForm({
  draft,
  setDraft,
  /** The row being edited, or `undefined` because this is a create. */
  storedName,
  /** Whether the server says a credential is already on file for this row. */
  hasCredential,
  /** `config` keys this form does not edit and sends back untouched. */
  carried,
  /** Whether a userinfo-carrying address was just taken apart. See `withoutUserinfo`. */
  scrubbed,
  /**
   * Told that the address has been edited, so the scrub note stops.
   *
   * That note is about one address and it should not outlive it: left standing
   * it sits in place of the ordinary "no user or password in it" hint over an
   * address it was never about, which is the hint most worth reading while
   * somebody is typing a fresh one.
   */
  unscrub,
}: {
  draft: Draft
  setDraft: (next: Draft) => void
  storedName?: string
  hasCredential: boolean
  carried: string[]
  scrubbed: boolean
  unscrub: () => void
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const set = <Key extends keyof Draft>(key: Key, value: Draft[Key]) =>
    setDraft({ ...draft, [key]: value })

  const fieldsAreJson = parseFields(draft.fields) !== null
  const renaming = storedName !== undefined && draft.name !== storedName
  // About the state this edit would leave behind, which is what the server's own
  // check is about: a request that only sets `enabled: true` carries no
  // credential and is still the request that puts one in front of a snapshot.
  //
  // `draft.secret !== ""` because the server's test is `bool(body.credential)`
  // and an empty string is false there -- an empty box stores nothing, so a
  // warning for it would be this form predicting a refusal the server would not
  // make. (That state cannot be saved at all, `halfTyped` sees to it, so this is
  // alignment rather than a fix; two rules that disagree are worth one of them
  // being wrong later.)
  const wouldHoldOne =
    (draft.credential === "set" && draft.secret !== "") ||
    (hasCredential && draft.credential !== "clear")

  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label htmlFor={field("name")}>Name</Label>
        <Input
          id={field("name")}
          value={draft.name}
          onChange={(event) => set("name", event.target.value)}
        />
        {renaming && (
          <p className="max-w-prose text-xs text-destructive">
            {`A panel binds a reading by naming this integration: {{${storedName}.<field>}}. ` +
              "Renaming it breaks every binding that names it, and a rack blanks what it cannot " +
              "resolve rather than drawing an error — so the panel goes empty and nothing says " +
              "why. Re-point those bindings on the Screens page, under Data."}
          </p>
        )}
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("url")}>URL</Label>
        <Input
          id={field("url")}
          value={draft.url}
          onChange={(event) => {
            set("url", event.target.value)
            unscrub()
          }}
        />
        {scrubbed ? (
          <p className="max-w-prose text-xs text-destructive">
            {"The credential has been taken out of the address, and is not repeated anywhere on " +
              "this page. Put it in Credential below, which is encrypted; config is exported in " +
              "plain text beside the database."}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            Where the rack polls. No user or password in it &mdash; there is a field for that below.
          </p>
        )}
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("fields")}>Fields (JSON)</Label>
        <Textarea
          id={field("fields")}
          rows={8}
          spellCheck={false}
          className="font-mono"
          value={draft.fields}
          onChange={(event) => set("fields", event.target.value)}
          placeholder={'{"cpu": {"query": "up", "reduce": "scalar"}}'}
        />
        {draft.fields !== "" && !fieldsAreJson && (
          <p className="max-w-prose text-xs text-destructive">
            {"This is not a JSON object with at least one entry in it, so there is nothing to " +
              "save. Each key is a reading a panel can bind to, and its value is " +
              '{"query": ..., "reduce": "scalar" | "top", "label": ..., "strip": ...}.'}
          </p>
        )}
        <p className="max-w-prose text-xs text-muted-foreground">
          {"One entry per reading. A query returns a vector and a panel shows one value, so " +
            "reduce is what collapses the two — it is written beside the query because it " +
            "depends on the query and not on how the result is drawn."}
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label htmlFor={field("interval")}>Poll interval (seconds)</Label>
          <Input
            id={field("interval")}
            type="number"
            value={draft.pollInterval}
            onChange={(event) => set("pollInterval", event.target.value)}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor={field("timeout")}>Query timeout (seconds)</Label>
          <Input
            id={field("timeout")}
            type="number"
            value={draft.timeout}
            onChange={(event) => set("timeout", event.target.value)}
          />
        </div>
      </div>
      {(positive(draft.pollInterval) === null || positive(draft.timeout) === null) && (
        <p className="max-w-prose text-xs text-destructive">
          Both are seconds and both must be above zero; the rack refuses anything else.
        </p>
      )}

      <div className="grid gap-2">
        <div className="flex items-center gap-3">
          <Switch
            id={field("enabled")}
            checked={draft.enabled}
            onCheckedChange={(next) => set("enabled", next)}
          />
          <Label htmlFor={field("enabled")}>Enabled</Label>
        </div>
        <p className="max-w-prose text-xs text-muted-foreground">
          A disabled integration is polled by nobody, so its readings publish nothing and no panel
          can bind to them.
        </p>
        {/* Shown whenever the edit would leave a credential behind, whether or
            not the switch is on yet: the point of saying it is that somebody
            about to turn it on reads it *before* doing so, and a warning that
            appeared only once the switch had moved would be a refusal restated
            rather than a refusal avoided. */}
        {/* Two wordings, because the remedy is a different control in each case.
            A row that already holds one has a "Remove the stored credential"
            option to point at; a row that does not has no such option at all
            (there is nothing to remove), and telling somebody to remove a
            credential they have not stored names a choice that is not on the
            screen. What is on the screen there is "No credential". */}
        {wouldHoldOne && (
          <p className="max-w-prose text-xs text-destructive">
            {(hasCredential
              ? "This holds a credential, so enabling it will be refused: "
              : "This would store a credential, so enabling it will be refused: ") +
              "no integration type can carry one to a rack yet, and an enabled row holding one " +
              "would block this rack's whole configuration. " +
              (hasCredential
                ? "Leave it off, or remove the stored credential below."
                : "Leave it off, or choose No credential below.")}
          </p>
        )}
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("credential")}>Credential</Label>
        <Select
          value={draft.credential}
          onValueChange={(next) => set("credential", next as CredentialChoice)}
        >
          <SelectTrigger id={field("credential")} aria-label="Credential" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {/* Worded as what it does to the stored secret, not as a state.
                "Keep" on a row with none would be a promise about nothing. */}
            <SelectItem value="keep">
              {hasCredential ? "Leave the stored credential alone" : "No credential"}
            </SelectItem>
            <SelectItem value="set">
              {hasCredential ? "Replace the stored credential" : "Store a credential"}
            </SelectItem>
            {/* Absent when there is nothing to clear: `credential: ""` on a row
                with no secret is a request that changes nothing and still pushes
                a snapshot to the rack. */}
            {hasCredential && (
              <SelectItem value="clear">Remove the stored credential</SelectItem>
            )}
          </SelectContent>
        </Select>

        {draft.credential === "set" && (
          <>
            <Label htmlFor={field("secret")}>Credential to store</Label>
            <Input
              id={field("secret")}
              type="password"
              autoComplete="new-password"
              value={draft.secret}
              onChange={(event) => set("secret", event.target.value)}
            />
            {draft.secret === "" && (
              <p className="max-w-prose text-xs text-destructive">
                {"Type the credential, or choose another option: an empty one is the instruction " +
                  "to remove the stored secret, and this form will not send that by accident."}
              </p>
            )}
          </>
        )}

        <p className="max-w-prose text-xs text-muted-foreground">
          {hasCredential
            ? "A credential is stored, encrypted. It cannot be read back — not here and not " +
              "through any route — so replacing it means typing the new one in full."
            : "Stored encrypted, and write-only: it appears in no response, no log line and no " +
              "export."}
        </p>
      </div>

      {carried.length > 0 && (
        <p className="max-w-prose text-xs text-muted-foreground">
          {`This config also carries ${carried.join(", ")}, which this form does not edit. It is ` +
            "sent back exactly as it came, because a config edit replaces the whole document."}
        </p>
      )}
    </div>
  )
}
