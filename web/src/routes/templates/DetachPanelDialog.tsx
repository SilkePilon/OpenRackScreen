import { useId, useState } from "react"

import { useSaveScreen, type Screen, type Template } from "@/api/queries"
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
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

/**
 * Stop one panel drawing this template, by naming what it draws instead.
 *
 * **What "detach" can honestly mean here, and why it is not "nothing".** The
 * `screen.template` column is a name, it is NOT NULL, and it has no foreign key;
 * `ScreenBody.template` is a string and there is no value the server reads as
 * "no template". A panel must name something. So detaching it from this template
 * is pointing it at another one, and the dialog says that in as many words
 * rather than inventing an empty template to park panels on -- an invented row
 * would be a rule this server does not have, it would be re-created by nothing,
 * and a rack would draw whatever an empty scene list renders as.
 *
 * When there is no other template, there is nothing honest to offer: the dialog
 * refuses with the reason and the thing to do about it. That state is reachable
 * -- one template on a server whose table was emptied and re-seeded with one row
 * -- and the alternative is an empty select that looks broken.
 */
export function DetachPanelDialog({
  screen,
  template,
  others,
  patch,
}: {
  screen: Screen
  template: Template
  /** Every template except this one, in the server's order. */
  others: Template[]
  patch: ReturnType<typeof useSaveScreen>
}) {
  const fieldId = useId()
  const [open, setOpen] = useState(false)
  // A name, because a name is what the column holds. `null` is "nobody has
  // chosen", which must not act like the first row.
  const [chosen, setChosen] = useState<string | null>(null)

  function change(next: boolean) {
    setOpen(next)
    if (next) {
      patch.reset()
      setChosen(null)
    }
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          {`Detach ${screen.name} from ${template.name}`}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Detach ${screen.name} from ${template.name}?`}</DialogTitle>
          <DialogDescription>
            {`A panel must always name a template — there is no "none" the rack would understand — ` +
              `so detaching ${screen.name} means pointing it at another one. Its parameters, its ` +
              "position and its wiring are untouched."}
          </DialogDescription>
        </DialogHeader>

        {others.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {`There is no other template for it to draw, so ${screen.name} cannot be detached from ` +
              `${template.name} yet. Add a template first — this page does not make them — and ` +
              "the panel can be moved to it."}
          </p>
        ) : (
          <div className="grid gap-2">
            <Label htmlFor={fieldId}>Draws instead</Label>
            <Select value={chosen ?? ""} onValueChange={setChosen}>
              <SelectTrigger id={fieldId}>
                <SelectValue placeholder="Choose a template" />
              </SelectTrigger>
              <SelectContent>
                {others.map((each) => (
                  <SelectItem key={each.id} value={each.name}>
                    {each.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}

        {patch.isError && (
          <p role="alert" className="text-sm text-destructive">
            {patch.error.message}
          </p>
        )}

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            disabled={patch.isPending || chosen === null}
            onClick={() => {
              if (chosen === null) return
              patch.mutate(
                { id: screen.id, body: { template: chosen } },
                { onSuccess: () => setOpen(false) },
              )
            }}
          >
            Detach the panel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
