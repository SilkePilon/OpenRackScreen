import { useId } from "react"

import { api } from "@/api/client"
import { useMutate, type Saved } from "@/api/mutate"
import {
  daemonsKey,
  eventsKey,
  useDaemons,
  type Daemon,
  type Pushed,
} from "@/api/queries"
import { nameDaemons } from "@/api/unservable"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { DeleteDaemonDialog } from "@/routes/daemons/DeleteDaemonDialog"
import { EventList } from "@/routes/daemons/EventList"
import { PairDialog } from "@/routes/daemons/PairDialog"
import { RotateKeyDialog } from "@/routes/daemons/RotateKeyDialog"

/**
 * What a push really did, said in the server's terms and not in the socket's.
 *
 * `delivered` is about **the send**, and the two are not the same question:
 * `hub.is_online` is true of a rack whose committed configuration cannot be
 * built, so there is no snapshot to send and nothing is attempted -- which is
 * how this route once answered `{"version": 2, "delivered": true}` with nothing
 * on the wire. The version is minted either way, and saying so matters: it is
 * why pressing the button again is not a no-op, and why the rack's
 * `applied_version` is now behind by a number nobody saw applied.
 *
 * The third case is `Saved.body` being absent, which is not a 204 -- no M3a
 * route answers one -- but a 2xx whose body did not parse: a proxy's HTML, a
 * truncated response. There is no version to name and no claim that can be
 * made about the send, so it makes neither.
 */
function pushOutcome(saved: Saved<Pushed>, name: string): string {
  const answer = saved.body
  if (answer === undefined) {
    return `The server accepted that push for ${name}, but sent no answer this interface could read, so nothing can be said about what reached the rack.`
  }
  if (answer.delivered) return `Version ${answer.version} was delivered to ${name}.`
  return `Version ${answer.version} was minted, but nothing was sent to ${name}.`
}

/**
 * One rack: what it is, what it is running, what is wrong with it, and the
 * three things that can be done to it.
 *
 * `config_version` and `applied_version` are drawn together and never one
 * without the other. On its own the first is a number this server invented, and
 * reporting it as though the glass agreed is the stale image the pair exists to
 * make visible.
 *
 * **`applied_version: null` is not a mismatch.** It is "no rack has told me" --
 * what an offline rack, a rack that has not answered its first push, and a rack
 * that has just reconnected all report, because the hub clears the ack on every
 * connect. Drawing it as "behind" would put a fault on every rack for the first
 * second after a reboot, and on every rack at all after a server restart.
 */
function RackCard({ rack, racks }: { rack: Daemon; racks: Daemon[] }) {
  const headingId = useId()
  const push = useMutate<Pushed, void>({
    send: () =>
      api.POST("/api/daemons/{daemon_id}/push", { params: { path: { daemon_id: rack.id } } }),
    // The version is minted whatever happens to the send, and the push is
    // recorded against the rack -- so the row and its events are both stale.
    invalidates: [daemonsKey, eventsKey(rack.id)],
  })
  const pushed = push.data
  const behind = rack.applied_version !== null && rack.applied_version !== rack.config_version

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          {rack.name}
        </h2>
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          {/* Decorative: the sentence beside it carries the whole meaning, so
              nothing here is said in colour alone. */}
          <span
            aria-hidden="true"
            className={cn(
              "inline-block size-2 shrink-0 rounded-full",
              rack.online ? "bg-emerald-500" : "bg-muted-foreground/40",
            )}
          />
          {rack.online ? "Online" : "Offline"} &middot; {rack.status}
        </p>
      </CardHeader>

      <CardContent className="grid gap-3">
        <div className="grid gap-0.5 text-sm">
          <p>Configuration version {rack.config_version}</p>
          <p>Applied version {rack.applied_version ?? "unknown"}</p>
        </div>

        {behind && (
          <p className="text-sm text-amber-600 dark:text-amber-500">
            {`This rack last applied version ${rack.applied_version}; the saved configuration is version ${rack.config_version}, so its panels are showing something older.`}
          </p>
        )}
        {rack.applied_version === null && (
          <p className="text-sm text-muted-foreground">
            No rack has told this server which version it is running. That is what an offline
            rack, and one that has only just reconnected, both report.
          </p>
        )}

        {rack.config_error !== null && (
          <Alert variant="destructive">
            <AlertTitle>This rack cannot be given a configuration</AlertTitle>
            <AlertDescription>{rack.config_error}</AlertDescription>
          </Alert>
        )}

        {push.isError && (
          <p role="alert" className="text-sm text-destructive">
            {push.error.message}
          </p>
        )}
        {pushed !== undefined && <p className="text-sm">{pushOutcome(pushed, rack.name)}</p>}
        {pushed !== undefined && pushed.unservable.length > 0 && (
          <Alert variant="destructive">
            <AlertTitle>Not every rack was given that change</AlertTitle>
            <AlertDescription>
              {`${nameDaemons(pushed.unservable, racks)}: no configuration that can be sent, so nothing was sent.`}
            </AlertDescription>
          </Alert>
        )}

        <EventList daemonId={rack.id} />
      </CardContent>

      <CardFooter className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={push.isPending} onClick={() => push.mutate()}>
          Push configuration now
        </Button>
        <RotateKeyDialog rack={rack} />
        <DeleteDaemonDialog rack={rack} />
      </CardFooter>
    </Card>
  )
}

/**
 * The racks, and the only page in this interface that asks the server for them.
 *
 * `useDaemons` fills the `daemons` cache entry the header's status strip and
 * every panel read without fetching, and the `daemons` message keeps `online`
 * true in it between fetches. Nothing here polls: `GET /api/daemons` assembles
 * a snapshot per rack on the event loop, which is time the server owes to
 * relaying frames, and the socket already says what a poll would be asking.
 */
export function DaemonsPage() {
  const racks = useDaemons()

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Daemons</h1>
        <PairDialog />
      </div>

      {racks.isPending && <p className="text-sm text-muted-foreground">Reading the racks&hellip;</p>}
      {racks.isError && (
        <Alert variant="destructive">
          <AlertTitle>The racks could not be read</AlertTitle>
          <AlertDescription>{racks.error.message}</AlertDescription>
        </Alert>
      )}
      {racks.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No racks yet. Pair one, and its screens can be added afterwards.
        </p>
      )}

      <div className="grid gap-4">
        {racks.data?.map((rack) => (
          <RackCard key={rack.id} rack={rack} racks={racks.data} />
        ))}
      </div>
    </>
  )
}
