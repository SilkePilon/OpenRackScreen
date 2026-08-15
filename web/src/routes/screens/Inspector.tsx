import { useId, useState } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import { screensKey, type Daemon, type Deleted, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
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

/** Every field of a screen, all optional, and `extra="forbid"` on the far end. */
type ScreenBody = components["schemas"]["ScreenBody"]

/**
 * The racks an edit could not be given to, by name, from the header.
 *
 * From `X-Unservable-Daemons` and from nothing else. A screen belongs to one
 * rack, so naming `screen.daemon_id` would look right on every response the
 * server realistically gives and would still be the interface answering a
 * question it was not asked: the header is the server's account of which racks
 * did not receive the push this edit caused, and a screen's own rack is not
 * that set -- a template edit reaches every rack that names it.
 *
 * An id with no row is named as an id. It is a rack this page's listing does
 * not have -- deleted in another tab between the fetch and the write, most
 * plainly -- and drawing nothing for it would quietly shorten the list of racks
 * somebody has to go and look at.
 */
function nameThem(ids: number[], racks: Daemon[]): string {
  return ids.map((id) => racks.find((rack) => rack.id === id)?.name ?? `daemon ${id}`).join(", ")
}

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

  const patch = useMutate<Screen, ScreenBody>({
    send: (body) =>
      api.PATCH("/api/screens/{screen_id}", {
        params: { path: { screen_id: screen.id } },
        body,
      }),
    // The screen list and nothing else. The edit does bump the rack's
    // `config_version`, but no version is drawn on this page, and every page
    // that draws one asks for the racks when it mounts.
    invalidates: [screensKey],
  })
  const saved = patch.data
  const save = (body: ScreenBody) => patch.mutate(body)

  /**
   * Forget what the last write said, because the form has moved on from it.
   *
   * "Saved, and every rack was given it" is true of an edit that has happened,
   * and stops being an answer to anything the moment somebody types the next
   * one: a person who renames a panel, is told it saved, then changes their mind
   * and edits three more boxes is looking at a reassurance about a write that no
   * longer describes what is on the screen -- and the destructive "not every
   * rack was given that change" is worse, because it names racks against an edit
   * the user has already left behind. Every tab calls this before it changes
   * anything.
   *
   * Guarded on the mutation being settled: `reset()` on an idle mutation still
   * dispatches, so an unguarded call would re-render the inspector on every
   * keystroke, and calling it on a *pending* one would throw away the result of
   * a write that is still in flight.
   */
  const edited = () => {
    if (patch.isSuccess || patch.isError) patch.reset()
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
              {`${nameThem(saved.unservable, racks)}: the change was saved, but nothing was sent. ` +
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
