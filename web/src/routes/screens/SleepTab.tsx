import { useId, useState } from "react"

import { useSettings, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
import { NightWindowFields } from "@/components/NightWindowFields"
import { WRAP_NOTE, describeNight, sameNight, type NightWindow } from "@/components/night"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { useUnsaved } from "@/routes/screens/unsaved"

type ScreenBody = components["schemas"]["ScreenBody"]

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

/**
 * When this one panel is dark, against what the whole server does.
 *
 * **The window wraps midnight when `start > end`, and that is the normal case.**
 * It is two times rather than a start and a duration because the person who
 * owns the rack thinks in "dark between 23:00 and 07:00", not in "dark for
 * eight hours" -- and interpreting the wrap is the daemon's job, so nothing
 * here reads a clock or decides whether the panel is asleep right now.
 *
 * The three controls, the sentence and the note about the wrap are
 * `components/night`'s, shared with the settings page's global window: this is
 * the same edit made in two places, and the seam this project has been bitten
 * by three times is the one where the two copies start behaving differently.
 * What stays here is what really differs -- whether there is an override at
 * all, and what an untouched field falls back to.
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
  const moved = !sameNight(wanted, held)
  // The last write's notice is not an answer about a form that has moved on
  // from it.
  useUnsaved(moved, edited)

  return (
    <div className="grid gap-4 pt-4">
      <p className="text-sm text-muted-foreground">
        {night === undefined
          ? "The rack's own night window is still being read."
          : `Every panel on this server: ${describeNight(night, settings.data?.timezone ?? "server")}`}
      </p>

      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("override")}>Override for this panel</Label>
        <Switch id={field("override")} checked={overriding} onCheckedChange={setOverride} />
      </div>

      {overriding && (
        <>
          <NightWindowFields
            value={shown}
            onEnabled={setEnabled}
            onStart={setStart}
            onEnd={setEnd}
          />

          <p className="text-xs text-muted-foreground">
            {`This panel: ${describeNight(shown, settings.data?.timezone ?? "server")} ${WRAP_NOTE}`}
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
