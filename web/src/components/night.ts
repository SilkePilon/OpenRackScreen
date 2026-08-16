import type { components } from "@/api/schema"

/** When a screen -- or every screen on the server -- is dark. */
export type NightWindow = components["schemas"]["NightWindow"]

/**
 * "Dark between 23:00 and 07:00", which is the sentence the two times mean.
 *
 * **The window wraps midnight when `start > end`, and that is the normal case.**
 * It is two times rather than a start and a duration because the person who owns
 * the rack thinks in "dark between 23:00 and 07:00", not in "dark for eight
 * hours" -- and interpreting the wrap is the daemon's job, so nothing here reads
 * a clock, decides whether anything is asleep right now, or treats a start later
 * than an end as anything but a night.
 *
 * Here rather than in either of the two components that say it, because both of
 * them do: the Screens inspector's per-panel override and the settings page's
 * global window. Two copies of this sentence are two chances for one of them to
 * start calling the ordinary case an error.
 */
export function describeNight(night: NightWindow, timezone: string): string {
  if (!night.enabled) return "Never sleeps."
  return `Dark between ${night.start} and ${night.end}, ${timezone} time.`
}

/**
 * Said under both editors, because a start after an end looks like a mistake
 * and is not one.
 */
const WRAP_NOTE =
  "A start later than the end means the window crosses midnight, which is the usual way round."

/**
 * The sentence under an editor's two time boxes: what the window means, and the
 * note about the wrap **when there is a wrap to have**.
 *
 * A window that is switched off has no start and no end that mean anything, so
 * "Never sleeps. A start later than the end means the window crosses midnight"
 * is a rule stated about two boxes nothing is reading -- prose that says nothing
 * in the state it appears in, which is how people learn to stop reading the
 * prose that does. Here rather than at either call site because both of them
 * said it, and both of them said it in the state where it says nothing.
 *
 * Not folded into `describeNight`, which is also used where there is no editor
 * at all: `SleepTab` draws the server's own window above the override switch as
 * a fact about the rack, and a note about which way round to type two times has
 * nothing to do with a sentence nobody can edit.
 */
export function describeNightEdit(night: NightWindow, timezone: string): string {
  const said = describeNight(night, timezone)
  return night.enabled ? `${said} ${WRAP_NOTE}` : said
}

/** Whether two windows are the same one. `null` is "no window", not a value. */
export function sameNight(left: NightWindow | null, right: NightWindow | null): boolean {
  if (left === null || right === null) return left === right
  return left.enabled === right.enabled && left.start === right.start && left.end === right.end
}
