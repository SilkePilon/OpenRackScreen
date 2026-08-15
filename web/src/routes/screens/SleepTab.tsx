import { useId, useState } from "react"

import { useSettings, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { useUnsaved } from "@/routes/screens/unsaved"

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
 *
 * Every control here is an overlay on the row, as in `ConfigTab`: `null` state
 * means untouched and follows `screen`, anything else is what somebody chose
 * and is kept. Three of the four already worked that way for a different
 * reason -- they wait on `GET /api/settings` -- and the override switch was
 * the one seeded at mount.
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
  /**
   * Called when this form starts holding an unsaved edit; see `Inspector`.
   *
   * Answers whether it took the report. `false` means ask again on the next
   * render, which is how an edit made during a write in flight is not lost.
   */
  edited: () => boolean
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const settings = useSettings()

  const held = readNight(screen.sleep_override)
  const night = settings.data?.night
  // `null` is "nobody has touched this switch" here too, and for the same
  // reason as the three states below plus one more: `screen` moves under this
  // form -- a save refetches the list, another tab edits the row -- and a
  // switch seeded once at mount would go on saying what the panel used to hold.
  // Untouched, it follows the row; touched, it keeps what somebody chose.
  const [override, setOverride] = useState<boolean | null>(null)
  const overriding = override ?? held !== null
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
  // The last write's notice is not an answer about a form that has moved on
  // from it.
  useUnsaved(moved, edited)

  return (
    <div className="grid gap-4 pt-4">
      <p className="text-sm text-muted-foreground">
        {night === undefined
          ? "The rack's own night window is still being read."
          : `Every panel on this server: ${describe(night, settings.data?.timezone ?? "server")}`}
      </p>

      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("override")}>Override for this panel</Label>
        <Switch id={field("override")} checked={overriding} onCheckedChange={setOverride} />
      </div>

      {overriding && (
        <>
          <div className="flex items-center justify-between gap-4">
            <Label htmlFor={field("enabled")}>Sleeps at all</Label>
            <Switch id={field("enabled")} checked={shown.enabled} onCheckedChange={setEnabled} />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="grid gap-2">
              <Label htmlFor={field("start")}>Dark from</Label>
              <Input
                id={field("start")}
                type="time"
                value={shown.start}
                onChange={(event) => setStart(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor={field("end")}>Light again at</Label>
              <Input
                id={field("end")}
                type="time"
                value={shown.end}
                onChange={(event) => setEnd(event.target.value)}
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
