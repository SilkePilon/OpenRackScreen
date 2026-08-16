import { useDaemons } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { RackIntegrations } from "@/routes/integrations/RackIntegrations"

/**
 * What each rack polls, and the credentials it polls with.
 *
 * **Grouped by rack because an integration belongs to one.** `integration` has a
 * `daemon_id`, `ors_daemon.config` builds pollers from that rack's enabled rows,
 * and a screen can only bind to readings its own rack takes -- so a flat list
 * would be a list in which the most important column is the one nobody reads.
 * Each rack's section asks for its own rows, on the key the Screens page's Data
 * tab reads, so an edit here goes stale in exactly the place it is consumed.
 *
 * **What this page is careful about, said once.** A credential is write-only:
 * `IntegrationView` has no field for one, so nothing here can draw a stored
 * secret even by accident, and what goes *out* carries `credential` only when
 * somebody asked for it to. Absent, `""` and a value are three different
 * instructions -- leave it alone, clear it, replace it -- and `draft.ts` is the
 * one place that decides which of the three a form is making.
 *
 * **Why it asks for the racks.** Two reasons, both about naming: a section is a
 * rack and has to be titled with its name, and `X-Unservable-Daemons` answers in
 * ids that have to become names. `useDaemons` is also the hook that fills the
 * cache entry the header strip reads, so this is one fetch on mount and no
 * polling, exactly as on the Screens and Templates pages.
 */
export function IntegrationsPage() {
  const racks = useDaemons()
  const rackRows = racks.data ?? []

  return (
    <>
      <h1 className="text-2xl font-semibold">Integrations</h1>
      <p className="max-w-prose text-sm text-muted-foreground">
        What each rack polls, and what its panels can bind to. A credential is stored encrypted and
        never read back &mdash; there is no response anywhere that carries one &mdash; so replacing
        one means typing it in full.
      </p>

      {racks.isPending && <p className="text-sm text-muted-foreground">Reading the racks&hellip;</p>}
      {racks.isError && (
        <Alert variant="destructive">
          <AlertTitle>The racks could not be read</AlertTitle>
          <AlertDescription>{racks.error.message}</AlertDescription>
        </Alert>
      )}
      {/* Gated on the racks having actually been read, for the reason the same
          sentence is gated on ScreensPage: a list consumed as `data ?? []`
          cannot tell "no racks" from "not yet" or "the request failed", and this
          one would otherwise be the only thing on the page for a server whose
          daemon table 500s. */}
      {rackRows.length === 0 && !racks.isPending && !racks.isError && (
        <p className="text-sm text-muted-foreground">
          No racks are paired yet, and an integration belongs to a rack. Pair one on the Daemons
          page and its section appears here.
        </p>
      )}

      <div className="grid gap-8">
        {rackRows.map((rack) => (
          <RackIntegrations key={rack.id} rack={rack} racks={rackRows} />
        ))}
      </div>
    </>
  )
}
