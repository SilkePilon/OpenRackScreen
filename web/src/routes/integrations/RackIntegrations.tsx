import { useId } from "react"

import {
  useCreateIntegration,
  useDeleteIntegration,
  useIntegrations,
  type Daemon,
} from "@/api/queries"
import { Landed } from "@/components/Landed"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { AddIntegrationDialog } from "@/routes/integrations/AddIntegrationDialog"
import { IntegrationCard } from "@/routes/integrations/IntegrationCard"

/**
 * One rack's integrations, asked for by that rack's id.
 *
 * **A component per rack, because the hook is per rack.** `useIntegrations`
 * takes a `daemon_id` and `GET /api/integrations?daemon_id=` is what the Screens
 * page's Data tab already reads through -- one entry per rack, keyed the same
 * way -- so an edit made here invalidates exactly the entry the binding editor
 * is reading. Listing every rack's integrations through one unfiltered request
 * would be a second hook on a second key answering the same question, and the
 * two would go stale at different times.
 *
 * **The create and the delete are mounted here, above the cards.** A create has
 * no card to live on yet and a delete takes its own card off the page before
 * anything could be drawn about it -- the invalidation is awaited before the
 * mutation settles -- so both answers, including the racks that did not get
 * them, have to be held by something that outlives a row.
 */
export function RackIntegrations({ rack, racks }: { rack: Daemon; racks: Daemon[] }) {
  const headingId = useId()
  const integrations = useIntegrations(rack.id)
  const create = useCreateIntegration(rack.id)
  const remove = useDeleteIntegration(rack.id)

  const created = create.data
  const removed = remove.data

  return (
    <section aria-labelledby={headingId} className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="text-lg font-medium">
          {rack.name}
        </h2>
        <AddIntegrationDialog rack={rack} create={create} />
      </div>

      {integrations.isPending && (
        <p className="text-sm text-muted-foreground">
          {`Reading the integrations on ${rack.name}…`}
        </p>
      )}
      {integrations.isError && (
        <Alert variant="destructive">
          <AlertTitle>{`${rack.name}'s integrations could not be read`}</AlertTitle>
          <AlertDescription>{integrations.error.message}</AlertDescription>
        </Alert>
      )}
      {/* **A definite claim, so it waits until it has been checked.** Read as
          `data ?? []`, a fetch in flight and a fetch that failed both look
          exactly like a rack that polls nothing -- which is the defect this
          milestone has already fixed twice, on ScreensPage and on TemplatesPage.
          `data?.length === 0` is true only once there is a list, which is the
          narrowest way to say it. */}
      {integrations.data?.length === 0 && (
        <p className="max-w-prose text-sm text-muted-foreground">
          {`${rack.name} polls nothing yet. Add an integration and each of its fields becomes a ` +
            "reading a panel on this rack can bind to, under Data on the Screens page."}
        </p>
      )}

      <Landed
        saved={created}
        racks={racks}
        what={`${created?.body?.name ?? "The integration"} was added.`}
      />
      <Landed
        saved={removed}
        racks={racks}
        what={`${remove.variables?.name ?? "The integration"} was deleted.`}
      />

      <div className="grid gap-4">
        {/* The server's order, `ORDER BY id`, and this page does not re-sort:
            re-sorting by name would move a row whenever it is renamed, which is
            the one edit most likely to be made twice in a row. */}
        {integrations.data?.map((integration) => (
          <IntegrationCard
            key={integration.id}
            integration={integration}
            rackName={rack.name}
            racks={racks}
            remove={remove}
          />
        ))}
      </div>
    </section>
  )
}
