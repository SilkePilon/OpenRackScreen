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
 * Point one panel at this template.
 *
 * **The body carries the name.** A screen's `template` column is a name and not
 * an id -- there is no foreign key on it, and the snapshot rather than the
 * database is what catches a screen naming a template that does not exist -- so
 * an assign that sent the row's id would write the string "21" into a column
 * every rack then fails to resolve. What goes on the wire is `{template: name}`
 * and nothing else: `ScreenBody` is `extra="forbid"`, and a body repeating the
 * fields this page happens to hold would overwrite whatever another tab changed
 * in the meantime.
 *
 * The write is the card's, not this dialog's, so what it answered outlives the
 * dialog closing over it -- and it is the same `useSaveScreen` the Screens
 * inspector writes through, so the two cannot drift apart in what they make
 * stale.
 */
export function AssignPanelDialog({
  template,
  candidates,
  rackName,
  patch,
}: {
  template: Template
  /** The panels that do not already name it, in the server's order. */
  candidates: Screen[]
  rackName: (daemonId: number) => string
  patch: ReturnType<typeof useSaveScreen>
}) {
  const fieldId = useId()
  const [open, setOpen] = useState(false)
  // The id as text, because that is what a select's value is. `null` is "nobody
  // has chosen", which is not the same as the first row and must not act like
  // it: an assign with nothing chosen would move a panel nobody named.
  const [chosen, setChosen] = useState<string | null>(null)

  function change(next: boolean) {
    setOpen(next)
    if (next) {
      // Opening starts a fresh write: the last one's answer is on the card, and
      // leaving it there while a second is set up would let it be read as this
      // one's. The choice is cleared for the same reason.
      patch.reset()
      setChosen(null)
    }
  }

  const screen = candidates.find((each) => String(each.id) === chosen)

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Assign a panel to ${template.name}`}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Assign a panel to ${template.name}`}</DialogTitle>
          <DialogDescription>
            {"The panel stops drawing whatever it draws now and draws this instead. Its own " +
              "parameters are kept, so anything this template does not declare is simply not " +
              "used, and the panel keeps its position and its wiring."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-2">
          <Label htmlFor={fieldId}>Panel</Label>
          <Select value={chosen ?? ""} onValueChange={setChosen}>
            <SelectTrigger id={fieldId}>
              <SelectValue placeholder="Choose a panel" />
            </SelectTrigger>
            <SelectContent>
              {candidates.map((each) => (
                <SelectItem key={each.id} value={String(each.id)}>
                  {`${each.name} on ${rackName(each.daemon_id)}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

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
            disabled={patch.isPending || screen === undefined}
            onClick={() => {
              if (screen === undefined) return
              // Closed on success only. A refused assign stays up with the
              // server's own sentence in it rather than dismissing itself with
              // nothing saying why the panel did not move.
              patch.mutate(
                { id: screen.id, body: { template: template.name } },
                { onSuccess: () => setOpen(false) },
              )
            }}
          >
            Assign the panel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
