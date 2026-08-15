import { useState } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import { daemonsKey, type Daemon, type Deleted } from "@/api/queries"
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

/**
 * Delete a rack, having said what goes with it.
 *
 * `screen`, `integration` and `daemon_event` all reference `daemon(id) ON
 * DELETE CASCADE`, so this is never only a rack: it is every panel's wiring,
 * position and template binding, every integration on it, and everything the
 * rack has recorded. Naming the screens is the spec's requirement and it is the
 * right one -- the wiring is the part that took somebody an afternoon and a
 * screwdriver, and it is the part a confirmation that only said "delete this
 * daemon?" would let them throw away without knowing.
 *
 * How many screens is deliberately not said here. This page does not fetch
 * them, and a count invented from what the interface happens to have cached
 * would be wrong in exactly the case it is read: another tab having added one.
 */
export function DeleteDaemonDialog({ rack }: { rack: Daemon }) {
  const [open, setOpen] = useState(false)

  const remove = useMutate<Deleted, void>({
    send: () =>
      api.DELETE("/api/daemons/{daemon_id}", { params: { path: { daemon_id: rack.id } } }),
    invalidates: [daemonsKey],
  })

  function change(next: boolean) {
    setOpen(next)
    if (!next) remove.reset()
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">Delete</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Delete ${rack.name}?`}</DialogTitle>
          <DialogDescription>
            {"Its screens go with it, including their wiring, their positions and what they " +
              "are bound to, and so do its integrations and its recent events. This cannot " +
              "be undone, and re-pairing the same Pi afterwards gives you an empty rack."}
          </DialogDescription>
        </DialogHeader>
        {remove.isError && (
          <p role="alert" className="text-sm text-destructive">
            {remove.error.message}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            // Closed on success only. A refused delete leaves the dialog up
            // with the server's own sentence in it, rather than dismissing
            // itself and leaving the rack on the page with no explanation.
            onClick={() => remove.mutate(undefined, { onSuccess: () => setOpen(false) })}
          >
            Delete the rack
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
