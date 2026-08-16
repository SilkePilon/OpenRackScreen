import { useId, useState } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import {
  screensKey,
  useSaveScreen,
  type Daemon,
  type Deleted,
  type Screen,
  type ScreenBody,
} from "@/api/queries"
import { nameDaemons } from "@/api/unservable"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ConfigTab } from "@/routes/screens/ConfigTab"
import { DataTab } from "@/routes/screens/DataTab"
import { SleepTab } from "@/routes/screens/SleepTab"

/**
 * Delete one panel, having said what that costs.
 *
 * The wiring is the part that took somebody an afternoon and a screwdriver, and
 * it is what a confirmation saying only "delete this screen?" would let them
 * throw away without knowing. The rack keeps its other panels and their
 * positions: `delete_screen` renumbers nothing, so a gap in the numbering is
 * exactly what is left behind, and the reorder buttons are what closes it.
 */
function DeleteScreenDialog({ screen }: { screen: Screen }) {
  const [open, setOpen] = useState(false)

  const remove = useMutate<Deleted, void>({
    send: () =>
      api.DELETE("/api/screens/{screen_id}", { params: { path: { screen_id: screen.id } } }),
    invalidates: [screensKey],
  })

  function change(next: boolean) {
    setOpen(next)
    if (!next) remove.reset()
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">Delete this panel</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Delete ${screen.name}?`}</DialogTitle>
          <DialogDescription>
            {"Its wiring, its position, the template it draws and everything bound to it go " +
              "with it. This cannot be undone, and adding the panel back means confirming " +
              "which SPI device it is on and which pins drive it all over again."}
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
            // Closed on success only. A refused delete stays up with the
            // server's own sentence in it rather than dismissing itself and
            // leaving the panel on the canvas with nothing saying why.
            onClick={() => remove.mutate(undefined, { onSuccess: () => setOpen(false) })}
          >
            Delete the panel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The three tabs over one screen, and the single PATCH they all write through.
 *
 * One mutation for three forms, deliberately. Every tab edits fields of the
 * same row through the same route, and what has to be said about a write -- the
 * server's refusal, and which racks did not get it -- is said in one place
 * where it cannot be forgotten by the third tab somebody adds. What each tab
 * owns is its own form state and the decision of *which fields it changed*;
 * what it hands over is a `ScreenBody` carrying those and nothing else.
 *
 * **Only the changed fields.** `ScreenBody` has `extra="forbid"` and PATCH
 * semantics come from `exclude_unset`, so a body echoing the whole `ScreenView`
 * back is a 422 on `id` alone -- and a body repeating every field the form
 * holds would overwrite whatever another tab changed in the meantime with what
 * this page happened to read a minute ago.
 */
function ScreenInspector({ screen, racks }: { screen: Screen; racks: Daemon[] }) {
  const headingId = useId()

  // The one screen PATCH this interface has, shared with the Templates page --
  // which assigns and detaches through the same route and the same field. Two
  // copies of it drifted apart in what they made stale is the seam that has
  // bitten this project three times, so there is one hook and no second set of
  // invalidations to forget.
  const patch = useSaveScreen()
  const saved = patch.data
  const save = (body: ScreenBody) => patch.mutate({ id: screen.id, body })

  /**
   * Forget what the last write said, because a form has moved on from it.
   *
   * "Saved, and every rack was given it" is true of an edit that has happened,
   * and stops being an answer to anything the moment somebody makes the next
   * one: a person who renames a panel, is told it saved, then changes their mind
   * and edits three more boxes is looking at a reassurance about a write that no
   * longer describes what is on the screen -- and the destructive "not every
   * rack was given that change" is worse, because it names racks against an edit
   * the user has since abandoned.
   *
   * Each tab reports this through `useUnsaved`, from the same fact that decides
   * whether its Save button is enabled, so there is one place per form rather
   * than one per control.
   *
   * Guarded on the mutation being settled: `reset()` on an idle mutation still
   * dispatches a re-render for nothing, and calling it on a *pending* one would
   * throw away the outcome of a write that is still in flight.
   *
   * **What the answer means.** `false` is "not taken, ask again", and only a
   * pending write gives it: somebody who types while the PATCH is on the wire
   * has made an edit this cannot act on yet, and `useUnsaved` keeps owing the
   * report until the mutation settles and a re-render brings it back. Reporting
   * it once and dropping it is how the success notice -- or the destructive
   * unservable alert -- came to stand over a form that had already moved on.
   */
  const edited = () => {
    if (patch.isPending) return false
    if (patch.isSuccess || patch.isError) patch.reset()
    return true
  }

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          {screen.name}
        </h2>
        <p className="text-sm text-muted-foreground">
          {`Position ${screen.position} on ${
            racks.find((rack) => rack.id === screen.daemon_id)?.name ?? `rack ${screen.daemon_id}`
          }`}
        </p>
      </CardHeader>

      <CardContent className="grid gap-4">
        {patch.isError && (
          <p role="alert" className="text-sm text-destructive">
            {patch.error.message}
          </p>
        )}
        {saved !== undefined && saved.unservable.length === 0 && (
          <p className="text-sm text-muted-foreground">Saved, and every rack was given it.</p>
        )}
        {saved !== undefined && saved.unservable.length > 0 && (
          <Alert variant="destructive">
            <AlertTitle>Not every rack was given that change</AlertTitle>
            <AlertDescription>
              {`${nameDaemons(saved.unservable, racks)}: the change was saved, but nothing was sent. ` +
                "Each of those racks has a reason on the Daemons page, and the usual recovery is " +
                "the next edit plus a push."}
            </AlertDescription>
          </Alert>
        )}

        <Tabs defaultValue="config">
          <TabsList>
            <TabsTrigger value="config">Config</TabsTrigger>
            <TabsTrigger value="data">Data</TabsTrigger>
            <TabsTrigger value="sleep">Sleep</TabsTrigger>
          </TabsList>
          <TabsContent value="config">
            <ConfigTab screen={screen} save={save} saving={patch.isPending} edited={edited} />
          </TabsContent>
          <TabsContent value="data">
            <DataTab screen={screen} save={save} saving={patch.isPending} edited={edited} />
          </TabsContent>
          <TabsContent value="sleep">
            <SleepTab screen={screen} save={save} saving={patch.isPending} edited={edited} />
          </TabsContent>
        </Tabs>

        <DeleteScreenDialog screen={screen} />
      </CardContent>
    </Card>
  )
}

/**
 * What is selected, or an invitation to select something.
 *
 * The inner component is keyed by screen id, so choosing another panel builds
 * fresh form state rather than leaving one screen's typed-but-unsaved wiring in
 * the boxes belonging to the next. It is *not* keyed by anything that changes
 * when the list is refetched: a save invalidates the list, and a form that
 * remounted on every refetch would throw away what somebody was still typing.
 */
export function Inspector({ screen, racks }: { screen: Screen | null; racks: Daemon[] }) {
  if (screen === null) {
    return (
      <Card className="gap-3">
        <CardHeader>
          <h2 className="text-base font-medium">Nothing selected</h2>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Choose a panel from the rack to see what it is drawing, change its wiring, or bind it
            to a reading.
          </p>
        </CardContent>
      </Card>
    )
  }
  return <ScreenInspector key={screen.id} screen={screen} racks={racks} />
}
