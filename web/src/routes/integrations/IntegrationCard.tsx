import { useId } from "react"

import {
  usePatchIntegration,
  useTestIntegration,
  type Daemon,
  type Integration,
  type TestReport,
  type useDeleteIntegration,
} from "@/api/queries"
import { Landed } from "@/components/Landed"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { DeleteIntegrationDialog } from "@/routes/integrations/DeleteIntegrationDialog"
import { EditIntegrationDialog } from "@/routes/integrations/EditIntegrationDialog"

/** The field names this integration publishes, in the order the config lists them. */
function fieldNames(config: Record<string, unknown>): string[] {
  const fields = config.fields
  if (typeof fields !== "object" || fields === null || Array.isArray(fields)) return []
  return Object.keys(fields)
}

/**
 * What a dry-run answered, said as what it is.
 *
 * **This is a reachability check and not a preview, and the difference is not a
 * nicety.** `POST /api/integrations/{id}/test` runs each field's query and
 * reports the *first sample of the result vector*. The daemon's `reduce`,
 * `label` and `strip` -- the three things that turn a vector into the one number
 * a 240x240 round panel shows -- are deliberately not reimplemented server-side,
 * because "a second copy of them in the server would be two answers to 'what
 * does this screen show' that could disagree". So a `top` field's panel value
 * and the number below it are routinely different, and an interface that called
 * this a preview would be claiming otherwise.
 *
 * What it is genuinely for: "a PromQL query that returns no series is
 * indistinguishable, on the glass, from a panel that has not finished
 * connecting", and this tells the two apart without saving anything and walking
 * to the rack.
 */
function Reachability({ report }: { report: TestReport }) {
  const titleId = useId()
  return (
    <div className="grid gap-1">
      <p id={titleId} className="text-sm font-medium">
        Reachability check
      </p>
      {/* Named after its own heading, so what the check answered is addressable
          apart from the field names the card already lists above it. */}
      <ul aria-labelledby={titleId} className="grid gap-1">
        {report.fields.map((field) => (
          <li key={field.name} className="text-sm text-muted-foreground">
            <code>{field.name}</code>
            {field.ok
              ? ` — answered, first sample ${field.value ?? ""}`
              : ` — ${field.error ?? "no answer, and no reason given"}`}
          </li>
        ))}
      </ul>
      <p className="max-w-prose text-xs text-muted-foreground">
        {"This is not a preview. It is the first sample of each query straight from the server; " +
          "the rack's own reduce, label and strip are what turn a result vector into the one " +
          "number a panel draws, and they are not applied here."}
      </p>
    </div>
  )
}

/**
 * One integration: what it polls, whether it holds a credential, and what may be
 * done to it.
 *
 * **`has_credential` and nothing else.** `IntegrationView` has no credential
 * field at all, so this card can say a secret is stored without one ever
 * travelling; there is no shape of this component in which it could draw the
 * plaintext, because no response it reads contains one.
 */
export function IntegrationCard({
  integration,
  rackName,
  racks,
  remove,
}: {
  integration: Integration
  rackName: string
  /** For naming the racks an edit could not be pushed to. */
  racks: Daemon[]
  /** The section's delete: this one takes the card off the page. */
  remove: ReturnType<typeof useDeleteIntegration>
}) {
  const headingId = useId()
  const whereId = useId()
  const save = usePatchIntegration(integration.id, integration.daemon_id)
  const check = useTestIntegration(integration.id)

  const fields = fieldNames(integration.config)
  const url = typeof integration.config.url === "string" ? integration.config.url : ""
  const saved = save.data
  const report = check.data?.body

  return (
    <Card role="region" aria-labelledby={`${headingId} ${whereId}`} className="gap-3">
      <CardHeader className="gap-1">
        <h3 id={headingId} className="text-base font-medium">
          {integration.name}
        </h3>
        <p className="text-sm text-muted-foreground">
          <span id={whereId}>{`on ${rackName}`}</span>
          {` · ${integration.type} · polled every ${integration.poll_interval} seconds · `}
          {integration.enabled ? "enabled" : "disabled"}
        </p>
      </CardHeader>

      <CardContent className="grid gap-3">
        <p className="text-sm text-muted-foreground">
          {url === "" ? "No URL in its config." : url}
        </p>

        <p className="text-sm text-muted-foreground">
          {fields.length === 0
            ? "It defines no fields, so it publishes no readings."
            : `Fields: ${fields.join(", ")}`}
        </p>
        {fields.length > 0 && (
          <p className="max-w-prose text-xs text-muted-foreground">
            {`A panel binds one of these as {{${integration.name}.${fields[0]}}}, under Data on ` +
              "the Screens page. The head is this integration's name, which is why renaming it " +
              "breaks the bindings that name it."}
          </p>
        )}
        {!integration.enabled && (
          <p className="max-w-prose text-xs text-muted-foreground">
            Disabled, so this rack builds no poller for it: it publishes nothing and no panel can
            bind to it until it is turned back on.
          </p>
        )}

        {/* `has_credential`, from the only field there is. The plaintext has no
            field in any response and therefore no way to reach this card. */}
        <p className="text-sm text-muted-foreground">
          {integration.has_credential
            ? "A credential is stored, encrypted, and cannot be read back."
            : "No credential stored."}
        </p>

        {save.isError && (
          <p role="alert" className="text-sm text-destructive">
            {save.error.message}
          </p>
        )}
        <Landed saved={saved} racks={racks} what={`${integration.name} was saved.`} />

        {check.isError && (
          <p role="alert" className="text-sm text-destructive">
            {check.error.message}
          </p>
        )}
        {report !== undefined && <Reachability report={report} />}
      </CardContent>

      <CardFooter className="flex flex-wrap gap-2">
        <EditIntegrationDialog integration={integration} save={save} />
        <Button
          variant="outline"
          disabled={check.isPending}
          // Named as the question it answers. "Test" alone reads as "show me
          // what this will look like", which is the one thing it does not do.
          onClick={() => check.mutate()}
        >
          {`Check ${integration.name} is reachable`}
        </Button>
        <DeleteIntegrationDialog integration={integration} remove={remove} />
      </CardFooter>
    </Card>
  )
}
