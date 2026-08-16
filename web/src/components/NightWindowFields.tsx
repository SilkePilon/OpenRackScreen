import { useId } from "react"

import type { NightWindow } from "@/components/night"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"

/**
 * The three controls a night window is edited with, wherever it is edited.
 *
 * Two places edit one: the Screens inspector's Sleep tab, where the window is a
 * per-panel override that may also be absent, and the settings page, where it is
 * the server's own and always exists. **The same edit in two places with two
 * different behaviours is the seam this project has been bitten by three times**,
 * so what is shared is exactly what must not diverge -- the labels, the control
 * types, and the fact that a start after an end is an ordinary night rather than
 * something to refuse or quietly swap.
 *
 * What is *not* shared is what really differs: whether there is a window at all,
 * what a form falls back to before the server has answered, and what the prose
 * around it is about. Those stay with each caller.
 *
 * One callback per field rather than one for the whole window, and that is not
 * a style choice: `SleepTab` holds each of the three as `null` until somebody
 * touches it, so an untouched field can still follow a `GET /api/settings` that
 * has not answered yet. A single `onChange(window)` would mark all three as
 * touched the moment the switch moved, and the panel's night would silently stop
 * following the rack's.
 */
export function NightWindowFields({
  value,
  onEnabled,
  onStart,
  onEnd,
}: {
  value: NightWindow
  onEnabled: (enabled: boolean) => void
  onStart: (start: string) => void
  onEnd: (end: string) => void
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`

  return (
    <>
      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("enabled")}>Sleeps at all</Label>
        <Switch id={field("enabled")} checked={value.enabled} onCheckedChange={onEnabled} />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="grid gap-2">
          <Label htmlFor={field("start")}>Dark from</Label>
          <Input
            id={field("start")}
            type="time"
            value={value.start}
            onChange={(event) => onStart(event.target.value)}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor={field("end")}>Light again at</Label>
          <Input
            id={field("end")}
            type="time"
            value={value.end}
            onChange={(event) => onEnd(event.target.value)}
          />
        </div>
      </div>
    </>
  )
}
