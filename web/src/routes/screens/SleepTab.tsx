import { useId, useState } from "react"

import { useSettings, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"

type ScreenBody = components["schemas"]["ScreenBody"]
type NightWindow = components["schemas"]["NightWindow"]

/**
 * The override this screen holds, or `null` because it holds none.
 *
 * `ScreenView.sleep_override` is `dict[str, Any] | None` -- the server answers
 * the column as it parsed it -- so the three fields are narrowed here. The
 * fallbacks are `NightWindow`'s own defaults, which is what a document missing
 * a field would validate to on the far end.
 */
function readNight(raw: Screen["sleep_override"]): NightWindow | null {
  if (raw === null) return null
  return {
    enabled: raw.enabled !== false,
    start: typeof raw.start === "string" ? raw.start : "23:00",
    end: typeof raw.end === "string" ? raw.end : "07:00",
  }
}

function same(left: NightWindow | null, right: NightWindow | null): boolean {
  if (left === null || right === null) return left === right
  return left.enabled === right.enabled && left.start === right.start && left.end === right.end
}

/** "dark between 23:00 and 07:00", which is the sentence the two times mean. */
function describe(night: NightWindow, timezone: string): string {
  if (!night.enabled) return "Never sleeps."
  return `Dark between ${night.start} and ${night.end}, ${timezone} time.`
}

/**
 * When this one panel is dark, against what the whole server does.
 *
 * **The window wraps midnight when `start > end`, and that is the normal case.**
 * It is two times rather than a start and a duration because the person who
 * owns the rack thinks in "dark between 23:00 and 07:00", not in "dark for
 * eight hours" -- and interpreting the wrap is the daemon's job, so nothing
 * here reads a clock or decides whether the panel is asleep right now.
 *
 * Turning the override off sends `sleep_override: null`, which is a change and
 * not an absence: `_columns` uses `exclude_unset` rather than `exclude_none`
 * precisely so that "this screen has stopped overriding the rack" is sayable.
 */
export function SleepTab({
  screen,
  save,
  saving,
  edited,
}: {
  screen: Screen
  save: (body: ScreenBody) => void
  saving: boolean
  /** Called before every change to this form; see `Inspector`'s `edited`. */
  edited: () => void
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const settings = useSettings()

  /** A setter that first says the form has moved on from what was last saved. */
  function changing<T>(set: (value: T) => void): (value: T) => void {
    return (value: T) => {
      edited()
      set(value)
    }
  }

  const held = readNight(screen.sleep_override)
  const night = settings.data?.night
  const [overriding, setOverriding] = useState(held !== null)
  // `null` is "nobody has touched this box", not a value, and the three lines
  // below are why it has to be a state of its own: a panel with no override of
  // its own starts from **the window it is already keeping**, which is the
  // server's -- and the server's arrives from a query that may not have
  // answered when this tab first rendered. Initialising the state from
  // `settings` would freeze whatever was known at that instant, and turning the
  // override on a moment too early would silently move the panel's night by
  // however much the two windows differ.
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [start, setStart] = useState<string | null>(null)
  const [end, setEnd] = useState<string | null>(null)
  const shown: NightWindow = {
    enabled: enabled ?? held?.enabled ?? night?.enabled ?? true,
    start: start ?? held?.start ?? night?.start ?? "23:00",
    end: end ?? held?.end ?? night?.end ?? "07:00",
  }

  const wanted: NightWindow | null = overriding ? shown : null
  const moved = !same(wanted, held)

  return (
    <div className="grid gap-4 pt-4">
      <p className="text-sm text-muted-foreground">
        {night === undefined
          ? "The rack's own night window is still being read."
          : `Every panel on this server: ${describe(night, settings.data?.timezone ?? "server")}`}
      </p>

      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("override")}>Override for this panel</Label>
        <Switch
          id={field("override")}
          checked={overriding}
          onCheckedChange={changing(setOverriding)}
        />
      </div>

      {overriding && (
        <>
          <div className="flex items-center justify-between gap-4">
            <Label htmlFor={field("enabled")}>Sleeps at all</Label>
            <Switch
              id={field("enabled")}
              checked={shown.enabled}
              onCheckedChange={changing(setEnabled)}
            />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="grid gap-2">
              <Label htmlFor={field("start")}>Dark from</Label>
              <Input
                id={field("start")}
                type="time"
                value={shown.start}
                onChange={(event) => changing(setStart)(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor={field("end")}>Light again at</Label>
              <Input
                id={field("end")}
                type="time"
                value={shown.end}
                onChange={(event) => changing(setEnd)(event.target.value)}
              />
            </div>
          </div>

          <p className="text-xs text-muted-foreground">
            {`This panel: ${describe(shown, settings.data?.timezone ?? "server")} ` +
              "A start later than the end means the window crosses midnight, which is the usual " +
              "way round."}
          </p>
        </>
      )}

      {!overriding && (
        <p className="text-xs text-muted-foreground">
          This panel follows the server&rsquo;s night window. Turning the override on gives it one
          of its own; turning it off again hands it back.
        </p>
      )}

      <Button
        className="justify-self-start"
        disabled={saving || !moved}
        onClick={() => save({ sleep_override: wanted })}
      >
        Save changes
      </Button>
    </div>
  )
}
