import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useId } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import { screensKey, type Screen } from "@/api/queries"
import { Panel } from "@/components/Panel"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/**
 * How big a panel is drawn, in CSS pixels.
 *
 * A GC9A01 is 240x240 and the frames arrive at that size, so this is a
 * reduction rather than an enlargement -- four of them fit a laptop's width
 * with room for the inspector beside them, and nothing is upscaled. The number
 * is here rather than in a stylesheet because it is also the canvas's own
 * drawing surface: `<Panel>` assigns it to `width` and `height`, which is what
 * makes the picture sharp instead of stretched.
 */
export const PANEL_SIZE = 160

/**
 * One rack, drawn as the row of round panels it is, in the order it is wired.
 *
 * **The order is the server's.** `GET /api/screens` answers `ORDER BY position,
 * id` and this component does not re-sort: sorting by id here would draw a rack
 * nobody wired, and the id tiebreak is the same one `snapshot._screens` uses, so
 * the row on screen and the row on the wall agree even when two panels share a
 * position.
 *
 * **Nothing here rotates.** `rotation` and `hflip` are edited in the inspector
 * and applied by the daemon, which streams the frame *before* it applies them --
 * so what arrives is already what a person standing at the rack sees, and the
 * panel is not even handed the value. There is no transform on the button around
 * it either, for the same reason: a hover that flipped a panel would be wrong
 * twice for as long as the pointer was over it.
 */
export function RackCanvas({
  rackName,
  screens,
  selectedId,
  onSelect,
}: {
  rackName: string
  screens: Screen[]
  selectedId: number | null
  onSelect: (screenId: number) => void
}) {
  const headingId = useId()
  /**
   * The reorder, and the reason there is no optimistic update behind it.
   *
   * `POST /api/screens/reorder` can refuse the whole request -- a duplicate id
   * is 422 and an unknown one is 404, both decided before anything is written
   * -- and the refusal is total by transaction. An interface that renumbered
   * its own list on the click would then be showing an order the rack is not
   * in, which is the exact failure the route's docstring names. So the request
   * goes, `useMutate` invalidates the list when it settles either way, and what
   * is drawn afterwards is what the server answered when asked again.
   */
  const reorder = useMutate<Screen[], number[]>({
    send: (ids) => api.POST("/api/screens/reorder", { body: { ids } }),
    invalidates: [screensKey],
  })

  /**
   * Move one panel one place, by naming the whole rack's new order.
   *
   * Every screen on this rack is named, not just the two that swap: the route
   * renumbers *the screens named*, from 1, so a request naming a pair would
   * leave the others holding numbers this edit did not assign -- and two panels
   * on one position is a state the schema allows, which is a rack whose order
   * is then decided by the id tiebreak rather than by anybody.
   */
  const move = (index: number, by: -1 | 1) => {
    const to = index + by
    // Not reachable through the buttons, which are disabled at the ends. Here
    // because the array swap below would otherwise write `undefined` into the
    // list and send it.
    if (to < 0 || to >= screens.length) return
    const ids = screens.map((screen) => screen.id)
    const moved = ids[index]
    ids[index] = ids[to]
    ids[to] = moved
    reorder.mutate(ids)
  }

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          {rackName}
        </h2>
        <p className="text-sm text-muted-foreground">
          {screens.length === 1 ? "One panel" : `${screens.length} panels`}, left to right as they
          are wired.
        </p>
      </CardHeader>

      <CardContent className="grid gap-3">
        {reorder.isError && (
          <p role="alert" className="text-sm text-destructive">
            {reorder.error.message}
          </p>
        )}

        <ol
          aria-label={`Panels on ${rackName}`}
          className="flex flex-wrap items-start gap-6 md:flex-nowrap md:overflow-x-auto"
        >
          {screens.map((screen, index) => {
            const selected = screen.id === selectedId
            return (
              <li key={screen.id} className="flex shrink-0 flex-col items-center gap-2">
                {/* The panel itself is the target: §5.4's layout is selection by
                    click, and the picture is what a person is looking at when
                    they decide which one they mean. `aria-pressed` because this
                    is a toggle in a set of them, and the label is the panel's
                    name so the accessible name is stable whatever the picture
                    is doing. */}
                <button
                  type="button"
                  aria-label={screen.name}
                  aria-pressed={selected}
                  onClick={() => onSelect(screen.id)}
                  className={cn(
                    "rounded-full outline-offset-4 focus-visible:outline-2 focus-visible:outline-ring",
                    selected && "outline-2 outline-primary",
                  )}
                >
                  <Panel screenId={screen.id} daemonId={screen.daemon_id} size={PANEL_SIZE} />
                </button>

                <div className="max-w-40 text-center">
                  <p className="truncate text-sm font-medium">{screen.name}</p>
                  <p className="truncate text-xs text-muted-foreground">{screen.template}</p>
                  {!screen.enabled && <p className="text-xs text-muted-foreground">Disabled</p>}
                </div>

                {/* Two buttons rather than a drag. A pointer-only reorder cannot
                    be done from a keyboard at all, and there is no drag a test
                    can perform that is not a fiction about the pointer. */}
                <div className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Move ${screen.name} left`}
                    disabled={index === 0 || reorder.isPending}
                    onClick={() => move(index, -1)}
                  >
                    <ChevronLeftIcon />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Move ${screen.name} right`}
                    disabled={index === screens.length - 1 || reorder.isPending}
                    onClick={() => move(index, 1)}
                  >
                    <ChevronRightIcon />
                  </Button>
                </div>
              </li>
            )
          })}
        </ol>
      </CardContent>
    </Card>
  )
}
