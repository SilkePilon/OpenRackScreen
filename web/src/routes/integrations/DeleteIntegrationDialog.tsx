import { useState } from "react"

import { type Integration, type useDeleteIntegration } from "@/api/queries"
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
 * Remove an integration, having said what goes with it.
 *
 * Two things go that are not obvious from the row. The **encrypted secret** is
 * deleted with it -- `integration.secret_id` is `ON DELETE SET NULL`, which
 * protects the integration from a deleted secret and does nothing in this
 * direction, so without the delete the ciphertext would outlive the only thing
 * that referenced it, in the database and in every export of it, unreachable and
 * undeletable through any route. And **every panel binding that names it** stops
 * resolving: a binding's head identifier is this integration's name, and a rack
 * blanks what it cannot resolve rather than drawing an error, so the panel goes
 * empty and nothing on it says why.
 *
 * There is no refusal to predict here -- no other table references an
 * integration row -- so this dialog states the consequences and then shows
 * whatever the server said.
 */
export function DeleteIntegrationDialog({
  integration,
  remove,
}: {
  integration: Integration
  /** The section's mutation: this delete takes its own card off the page. */
  remove: ReturnType<typeof useDeleteIntegration>
}) {
  const [open, setOpen] = useState(false)

  function change(next: boolean) {
    setOpen(next)
    // Opening clears the last delete's answer, which is drawn above the list and
    // would otherwise be read as this one's.
    if (next) remove.reset()
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Delete ${integration.name}`}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Delete ${integration.name}?`}</DialogTitle>
          <DialogDescription>
            {"Its stored credential is deleted with it — nothing else references the secret, and " +
              "no route could reach it afterwards. Any panel that binds a reading from it draws " +
              "blank from the next configuration onwards, and nothing on the panel says why, so " +
              "re-point those bindings first."}
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
            // Closed on success only, so a refusal stays up with the server's
            // sentence rather than dismissing itself over a row that is still
            // on the page with nothing saying why.
            onClick={() =>
              remove.mutate(
                { id: integration.id, name: integration.name },
                { onSuccess: () => setOpen(false) },
              )
            }
          >
            Delete the integration
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
