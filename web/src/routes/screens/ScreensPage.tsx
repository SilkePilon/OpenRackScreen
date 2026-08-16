import { useState } from "react"

import { useDaemons, useScreens, type Daemon, type Screen } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Inspector } from "@/routes/screens/Inspector"
import { RackCanvas } from "@/routes/screens/RackCanvas"

/**
 * The screens of one rack, kept in the order the server sent them.
 *
 * Grouped rather than drawn as one row, because `position` is the ordinal of a
 * panel *within its rack*: two racks each numbered from 1 interleave into a row
 * that describes no wall, and a reorder driven from that row would renumber one
 * rack's panels from the other's positions. A `Map` keeps insertion order, so
 * the racks come out in the order their first panel does -- which, the list
 * being ordered by `position, id`, is stable between fetches.
 */
function byRack(screens: Screen[]): Map<number, Screen[]> {
  const racks = new Map<number, Screen[]>()
  for (const screen of screens) {
    const group = racks.get(screen.daemon_id)
    if (group === undefined) racks.set(screen.daemon_id, [screen])
    else group.push(screen)
  }
  return racks
}

/**
 * The rack canvas and the inspector beside it: §5.4's approved layout.
 *
 * **Why this page asks for the racks.** `useDaemons` is the one hook that fills
 * the `daemons` cache entry, and a panel reads *offline* from that entry and
 * from nothing else. The socket's `daemons` message only patches rows that are
 * already there -- it invents none -- so on a page that never fetched the list,
 * every panel would sit at `undefined` forever and a rack that had gone would
 * never be drawn as gone. It is also what puts a name on each canvas and on the
 * racks an edit could not be given to. That is the whole of it: one fetch on
 * mount, no polling, and the socket keeps it true afterwards.
 *
 * `GET /api/screens/{id}/preview` is deliberately not used here. Every panel on
 * this page is live, and a server-rendered still would be a second picture of
 * the same screen -- drawn from no data at all, so a rack that is showing real
 * readings would appear to be showing zeroes. The preview belongs where there is
 * nothing streaming: the Templates page.
 */
export function ScreensPage() {
  const screens = useScreens()
  const racks = useDaemons()
  // The id rather than the row: the row is refetched after every edit, and a
  // selection holding a stale copy would go on drawing the name a panel had
  // before it was renamed. A selection whose screen has been deleted resolves
  // to nothing, which is how the inspector lets go of it.
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const rows = screens.data ?? []
  const selected = rows.find((screen) => screen.id === selectedId) ?? null
  const rackRows: Daemon[] = racks.data ?? []
  const nameOf = (daemonId: number) =>
    rackRows.find((rack) => rack.id === daemonId)?.name ?? `rack ${daemonId}`

  /**
   * One row per rack, and a paired rack with no panels gets one too.
   *
   * Task 8 built this list out of the screens alone, which drew nothing at all
   * for a rack that had none -- so the one rack that most needs an add-screen
   * affordance was the one rack with nowhere to put it. The racks come first and
   * in the order `GET /api/daemons` gives them, so the page does not reshuffle
   * itself as panels are added; a rack that has screens but is in no listing --
   * deleted in another tab between the two fetches -- still gets its row, because
   * dropping it would silently hide panels that exist.
   */
  const grouped = byRack(rows)
  const rackIds = [...new Set([...rackRows.map((rack) => rack.id), ...grouped.keys()])]

  return (
    <>
      <h1 className="text-2xl font-semibold">Screens</h1>

      {screens.isPending && <p className="text-sm text-muted-foreground">Reading the panels&hellip;</p>}
      {screens.isError && (
        <Alert variant="destructive">
          <AlertTitle>The panels could not be read</AlertTitle>
          <AlertDescription>{screens.error.message}</AlertDescription>
        </Alert>
      )}
      {racks.isError && (
        <Alert variant="destructive">
          <AlertTitle>The racks could not be read</AlertTitle>
          <AlertDescription>{racks.error.message}</AlertDescription>
        </Alert>
      )}
      {/* **A definite claim, so it waits until it has been checked.** "No racks
          are paired yet" is a statement about `GET /api/daemons`, and this page
          consumes that list as `racks.data ?? []` -- which is also what a fetch
          still in flight and a fetch that failed both look like. Gated on the
          screens alone, a fresh load where the panels resolve first says it for
          a moment on a rack wall that is fully paired, and a daemons fetch that
          404s or times out says it permanently, underneath an alert saying the
          list could not be read. The old copy ("No panels yet") was true in both
          of those; this one is more useful and is not, so it is the one that has
          to wait. */}
      {rackRows.length === 0 &&
        rows.length === 0 &&
        !screens.isPending &&
        !racks.isPending &&
        !racks.isError && (
          <p className="text-sm text-muted-foreground">
            No racks are paired yet. Pair one on the Daemons page; its panels are added from its
            own row here.
          </p>
        )}

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="grid gap-4">
          {rackIds.map((daemonId) => (
            <RackCanvas
              key={daemonId}
              daemonId={daemonId}
              rackName={nameOf(daemonId)}
              screens={grouped.get(daemonId) ?? []}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          ))}
        </div>

        <Inspector screen={selected} racks={rackRows} />
      </div>
    </>
  )
}
