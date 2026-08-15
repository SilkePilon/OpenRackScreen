import { useId, useState } from "react"

import { useIntegrations, useTemplates, type Integration, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
import { Button } from "@/components/ui/button"
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

type ScreenBody = components["schemas"]["ScreenBody"]

/**
 * What a template says one of its parameters is.
 *
 * The generated `TemplateView.params_schema` is `{[key: string]: unknown}` --
 * the server declares the column `dict[str, Any]` and answers it as it parsed
 * it, so the shape below is *what the model says*, not what the wire promises.
 * Everything is therefore narrowed at the boundary and a row that does not fit
 * degrades to a plain text box rather than failing the tab.
 */
const PARAM_TYPES = ["string", "number", "color", "palette", "binding", "boolean"] as const
type ParamType = (typeof PARAM_TYPES)[number]
type Spec = { type: ParamType; label: string; fallback: unknown }

function readSpec(key: string, raw: unknown): Spec {
  const spec = (typeof raw === "object" && raw !== null ? raw : {}) as {
    type?: unknown
    label?: unknown
    default?: unknown
  }
  return {
    // `"string"` is `ParamSpec.type`'s own default, so an entry that omits it
    // is not being guessed at here.
    type: PARAM_TYPES.find((each) => each === spec.type) ?? "string",
    // And `label` defaults to the empty string, which is a label nobody can
    // read; the key is what the template author called it.
    label: typeof spec.label === "string" && spec.label !== "" ? spec.label : key,
    fallback: spec.default,
  }
}

/** A scalar as a form value. Anything else is a document, and says so. */
function isScalar(value: unknown): value is string | number | boolean {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
}

function textOf(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (isScalar(value)) return String(value)
  return JSON.stringify(value)
}

/**
 * The readings this rack publishes, written the way a binding has to name them.
 *
 * The head of the expression is the *integration's own name* -- that is what
 * `ors_daemon.snapshot` keys its data by and what `config._dependencies`
 * intersects against the configured integrations -- and the next part is a key
 * of that integration's `fields`. A field that reduces to `top` answers
 * `{"node": ..., "value": ...}`, so its two halves are offered separately: a
 * binding to the field alone resolves to a dict, which draws as one.
 *
 * **A disabled integration publishes nothing.** `ors_daemon.config` builds
 * pollers from the enabled integrations only, so nothing ever writes that name
 * into `snapshot.data` -- a binding naming it would be left unresolved and the
 * field would draw blank. Offering it here would be offering a reading this rack
 * does not take, which is a worse answer than not offering it: the panel goes
 * empty and nothing says why. Turning the integration back on is what makes it
 * bindable, and that is a decision on the Integrations page.
 */
function readingsOf(integrations: Integration[]): string[] {
  const readings: string[] = []
  for (const integration of integrations) {
    if (!integration.enabled) continue
    const fields = integration.config.fields
    if (typeof fields !== "object" || fields === null) continue
    for (const [key, raw] of Object.entries(fields as Record<string, unknown>)) {
      const spec = (typeof raw === "object" && raw !== null ? raw : {}) as { reduce?: unknown }
      if (spec.reduce === "top") {
        readings.push(`{{${integration.name}.${key}.node}}`)
        readings.push(`{{${integration.name}.${key}.value}}`)
      } else {
        readings.push(`{{${integration.name}.${key}}}`)
      }
    }
  }
  return readings
}

/**
 * The template's parameters, and what this screen puts in them.
 *
 * **What a binding is, and where that was established.** A parameter of type
 * `binding` holds a plain string carrying `{{ ... }}`:
 * `ors_render.render.expand_params` resolves any parameter value that is a
 * string containing `{{` against the rack's live data, once, and blanks
 * anything still unresolved. `daemon/examples/rack.yaml` writes them by hand --
 * `big: "{{prom.cpu | round:0}}%"` -- and `ors_daemon.config._dependencies`
 * reads the head identifier back out to decide which integrations a screen
 * depends on. So the wire format is a string, this tab writes a string, and the
 * select beside each binding field inserts one that names a reading the rack
 * really publishes. `ParamSpec.type` chooses the control and nothing else; no
 * code anywhere branches on it, which is why a template that declares a binding
 * and a screen that puts a literal in it both work.
 *
 * **Only what changed is written.** `params` is one column and one field of
 * `ScreenBody`, so the whole map goes in the body -- but a key whose control
 * still shows the template's default is left out of the map entirely. Writing
 * it would freeze today's default into this screen, and a later edit of the
 * template would then move every screen except the ones somebody had opened.
 */
export function DataTab({
  screen,
  save,
  saving,
  edited,
}: {
  screen: Screen
  save: (body: ScreenBody) => void
  saving: boolean
  /** Called before every change to this form; see `Inspector`'s `edited`. */
  edited: () => void
}) {
  const fieldId = useId()
  const templates = useTemplates()
  const integrations = useIntegrations(screen.daemon_id)

  const template = templates.data?.find((each) => each.name === screen.template)
  const specs = Object.entries(template?.params_schema ?? {}).map(
    ([key, raw]) => [key, readSpec(key, raw)] as const,
  )
  /** What the screen draws with today: its own value, or the template's default. */
  const effective = (key: string, spec: Spec) =>
    Object.hasOwn(screen.params, key) ? screen.params[key] : spec.fallback

  const [draft, setDraft] = useState<Record<string, string | boolean>>({})
  const valueOf = (key: string, spec: Spec): string | boolean => {
    const held = draft[key]
    if (held !== undefined) return held
    const current = effective(key, spec)
    return spec.type === "boolean" ? current === true : textOf(current)
  }
  const set = (key: string, value: string | boolean) => {
    edited()
    setDraft({ ...draft, [key]: value })
  }

  /**
   * The parameters map to write, or `null` if nothing in it moved.
   *
   * Built over the screen's existing map rather than over the schema: a
   * parameter the template no longer declares is kept, exactly as
   * `Template.bind_params` keeps it -- `params_schema` is what an editor
   * offers, not a filter on what a scene may read, and a user-edited template
   * can draw `{{params.extra}}` without declaring it.
   */
  function changed(): { params: Record<string, unknown>; moved: boolean } {
    const params: Record<string, unknown> = { ...screen.params }
    let moved = false
    for (const [key, spec] of specs) {
      const current = effective(key, spec)
      if (!isScalar(current) && current !== null && current !== undefined) continue
      const value = valueOf(key, spec)
      if (spec.type === "boolean") {
        if (value === (current === true)) continue
        params[key] = value
      } else {
        if (value === textOf(current)) continue
        params[key] =
          spec.type === "number" ? (value === "" ? null : Number(value)) : (value as string)
      }
      moved = true
    }
    return { params, moved }
  }

  const { params, moved } = changed()
  const readings = readingsOf(integrations.data ?? [])

  if (templates.isPending) {
    return <p className="pt-4 text-sm text-muted-foreground">Reading the templates&hellip;</p>
  }
  if (template === undefined) {
    return (
      <p className="pt-4 text-sm text-muted-foreground">
        {`This panel names the template ${screen.template}, which the server does not have. Until ` +
          "it does, its rack cannot be given a configuration at all -- the Daemons page says so " +
          "against the rack -- and there are no parameters to offer here."}
      </p>
    )
  }

  return (
    <div className="grid gap-4 pt-4">
      {specs.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {`${template.name} declares no parameters, so there is nothing to bind. What it draws is ` +
            "fixed in the template itself."}
        </p>
      )}

      {specs.map(([key, spec]) => {
        const id = `${fieldId}-${key}`
        const current = effective(key, spec)
        const isDocument = !isScalar(current) && current !== null && current !== undefined
        const value = valueOf(key, spec)

        return (
          <div key={key} className="grid gap-2">
            <Label htmlFor={id}>{spec.label}</Label>

            {spec.type === "boolean" ? (
              <Switch
                id={id}
                checked={value === true}
                disabled={isDocument}
                onCheckedChange={(next) => set(key, next)}
              />
            ) : (
              <Input
                id={id}
                type={spec.type === "number" ? "number" : "text"}
                value={typeof value === "string" ? value : String(value)}
                readOnly={isDocument}
                onChange={(event) => set(key, event.target.value)}
              />
            )}

            {isDocument && (
              <p className="text-xs text-muted-foreground">
                {`${spec.label} holds a document rather than a value. This editor writes single ` +
                  "values, so it is shown here and left alone."}
              </p>
            )}

            {spec.type === "binding" && !isDocument && (
              <Select
                value=""
                disabled={readings.length === 0}
                onValueChange={(reading) =>
                  set(key, `${typeof value === "string" ? value : ""}${reading}`)
                }
              >
                <SelectTrigger
                  className="w-full"
                  aria-label={`Insert a reading into ${spec.label}`}
                >
                  <SelectValue
                    placeholder={
                      readings.length === 0
                        ? "This rack polls nothing yet"
                        : "Insert a reading"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {readings.map((reading) => (
                    <SelectItem key={reading} value={reading}>
                      {reading}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        )
      })}

      {specs.some(([, spec]) => spec.type === "binding") && (
        <p className="text-xs text-muted-foreground">
          A binding is text with a reading in it: <code>{"{{prom.cpu | round:0}}%"}</code>. The rack
          fills it in as it draws; anything it cannot resolve is left blank rather than printed.
        </p>
      )}

      <Button
        className="justify-self-start"
        disabled={saving || !moved}
        onClick={() => save({ params })}
      >
        Save changes
      </Button>
    </div>
  )
}
