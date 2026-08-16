import { useState } from "react"

import { useDeleteTemplate, type Template } from "@/api/queries"
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
 * Delete a template the editor made, having said what stops it.
 *
 * **This control is not rendered for a built-in at all.** The server refuses one
 * with 409 -- it is re-seeded at every start, so a delete that undoes itself
 * overnight is worse than one that refuses -- and a button that always fails is
 * the button that lies. The card says why instead, and offers the amendment that
 * *is* allowed.
 *
 * The other refusal cannot be predicted from the row and is therefore left to
 * the server: a template still named by an enabled screen takes the whole change
 * back, because `build_snapshot` will not assemble a configuration whose screen
 * names a template that is not defined, and the message names the panel. This
 * dialog says that is possible and then shows whatever the server said.
 */
export function DeleteTemplateDialog({
  template,
  remove,
}: {
  template: Template
  remove: ReturnType<typeof useDeleteTemplate>
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
        <Button variant="outline">{`Delete ${template.name}`}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Delete ${template.name}?`}</DialogTitle>
          <DialogDescription>
            {"Its scenes and its parameter schema go with it, and this interface cannot make a " +
              "template to put back. A panel that still draws it stops the whole delete, and the " +
              "refusal names the panel — detach it first."}
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
            // sentence in it rather than dismissing itself and leaving the
            // template on the page with nothing saying why.
            onClick={() =>
              remove.mutate(
                { id: template.id, name: template.name },
                { onSuccess: () => setOpen(false) },
              )
            }
          >
            Delete the template
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
