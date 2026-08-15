import { useState } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import { daemonsKey, eventsKey, type Daemon, type DaemonCreated } from "@/api/queries"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { TokenOnce } from "@/routes/daemons/PairDialog"

/**
 * Revoke the key this rack presents on every connect, and mint a token for a
 * new one.
 *
 * The action a leaked key needs. Until the route existed the only way to
 * invalidate a key was to delete the rack, which cascades its screens away, so
 * recovering from a leak meant re-entering the whole configuration.
 *
 * What the description has to say, because both halves surprise somebody:
 * a connected rack is **not** dropped -- it keeps working on the socket it is
 * already holding -- and it is nonetheless **unpaired from now on**, so the
 * next reconnect fails until the new token has reached the Pi. A rack that is
 * offline right now is therefore unreachable until somebody walks to it.
 */
export function RotateKeyDialog({ rack }: { rack: Daemon }) {
  const [open, setOpen] = useState(false)

  const rotate = useMutate<DaemonCreated, void>({
    send: () =>
      api.POST("/api/daemons/{daemon_id}/rotate-key", {
        params: { path: { daemon_id: rack.id } },
      }),
    // The rack's status becomes `unpaired`, and the rotation is recorded
    // against it as a warning -- so both the row and its events are stale.
    invalidates: [daemonsKey, eventsKey(rack.id)],
  })
  const minted = rotate.data?.body

  function change(next: boolean) {
    setOpen(next)
    // Same "exactly once" as pairing: the token lives in this mutation's result
    // and nowhere else, and dismissing the dialog is what forgets it.
    if (!next) rotate.reset()
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">Rotate key</Button>
      </DialogTrigger>
      <DialogContent>
        {minted ? (
          <>
            <DialogHeader>
              <DialogTitle>{`${minted.name} is waiting for its new token`}</DialogTitle>
              <DialogDescription>
                {`The old key is gone. ${minted.name} is unpaired until this has run on the Pi.`}
              </DialogDescription>
            </DialogHeader>
            <TokenOnce token={minted.token} />
            <DialogFooter>
              <DialogClose asChild>
                <Button>Done</Button>
              </DialogClose>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>{`Rotate ${rack.name}'s key?`}</DialogTitle>
              <DialogDescription>
                {`The key ${rack.name} presents on every connect is revoked and a new pairing ` +
                  "token is minted. The rack keeps working on the connection it already has, " +
                  "and it stays unpaired until the new token reaches it, so it cannot come " +
                  "back on its own until you have run the line this gives you."}
              </DialogDescription>
            </DialogHeader>
            {rotate.isError && (
              <p role="alert" className="text-sm text-destructive">
                {rotate.error.message}
              </p>
            )}
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Cancel</Button>
              </DialogClose>
              <Button disabled={rotate.isPending} onClick={() => rotate.mutate()}>
                Rotate the key
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
