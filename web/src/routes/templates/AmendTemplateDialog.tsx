import { useId, useState } from "react"

import { useAmendTemplate, type Template, type TemplateBody } from "@/api/queries"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** What this dialog holds while it is open. Text, because a box holds text. */
type Draft = {
  name: string
  category: string
}

/**
 * Rename a template, or file it under something else.
 *
 * **Only the fields that changed go on the wire.** `TemplateBody` is
 * `extra="forbid"` and the route's PATCH semantics come from `exclude_unset`, so
 * a body echoing the whole `TemplateView` back is a 422 on `id` and `builtin`
 * alone -- and, worse, `scenes` in a body this page cannot edit would write back
 * whatever it last read, silently reverting a change made anywhere else. The
 * empty body is refused before it is sent: this route affects **every** rack,
 * and a PATCH that named nothing would still bump every config_version there is
 * and push a snapshot to each of them for an edit nobody made.
 *
 * **Scenes and parameters are not here.** A template is a document of scenes and
 * the editor for those is phase 2. What is offered is what can be offered
 * honestly with one text box each.
 *
 * **What renaming a built-in really does**, said before it is done rather than
 * discovered afterwards: the panels that name it fall back to the copy
 * `ors-render` ships -- `build_snapshot` resolves a screen's template against the
 * table *and* the built-ins -- and `seed_builtin_templates` puts the old name
 * back as a fresh built-in row at the next start, because it inserts whatever is
 * missing. So a rename is a fork, not a move.
 */
export function AmendTemplateDialog({
  template,
  amend,
}: {
  template: Template
  amend: ReturnType<typeof useAmendTemplate>
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>({
    name: template.name,
    category: template.category,
  })

  function change(next: boolean) {
    setOpen(next)
    if (next) {
      // Opening starts from the row as it is now, not from what was typed and
      // abandoned last time -- the row moves under this dialog when another tab
      // amends it -- and clears the last write's answer, which is on the card
      // and would otherwise read as this one's.
      amend.reset()
      setDraft({ name: template.name, category: template.category })
    }
  }

  /** Exactly the fields that differ, and never one more. */
  function changes(): TemplateBody {
    const body: TemplateBody = {}
    if (draft.name !== template.name) body.name = draft.name
    if (draft.category !== template.category) body.category = draft.category
    return body
  }

  const body = changes()
  const nothingToSave = Object.keys(body).length === 0

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Amend ${template.name}`}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Amend ${template.name}`}</DialogTitle>
          <DialogDescription>
            {"The scenes this template draws are not edited here. A template edit reaches every " +
              "rack there is, so this one is pushed to all of them."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-2">
          <Label htmlFor={field("name")}>Name</Label>
          <Input
            id={field("name")}
            value={draft.name}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          />
          <p className="text-xs text-muted-foreground">
            {template.builtin
              ? "Renaming a built-in forks it: the panels that name it fall back to the copy " +
                "ors-render ships, and the old name is re-seeded as a fresh built-in at the next " +
                "restart."
              : "A panel names its template by name. A rename that would leave a panel naming " +
                "nothing is refused whole, and the refusal says which panel."}
          </p>
        </div>

        <div className="grid gap-2">
          <Label htmlFor={field("category")}>Category</Label>
          <Input
            id={field("category")}
            value={draft.category}
            onChange={(event) => setDraft({ ...draft, category: event.target.value })}
          />
        </div>

        {amend.isError && (
          <p role="alert" className="text-sm text-destructive">
            {amend.error.message}
          </p>
        )}

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            disabled={amend.isPending || nothingToSave}
            // Closed on success only. A refused amendment stays up with the
            // server's own sentence in it -- "a template named 'x' exists", or
            // the panel a rename would have orphaned -- rather than dismissing
            // itself over a card that has not changed.
            onClick={() => amend.mutate(body, { onSuccess: () => setOpen(false) })}
          >
            Save the template
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
