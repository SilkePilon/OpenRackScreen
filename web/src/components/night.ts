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
export const WRAP_NOTE =
  "A start later than the end means the window crosses midnight, which is the usual way round."

/** Whether two windows are the same one. `null` is "no window", not a value. */
export function sameNight(left: NightWindow | null, right: NightWindow | null): boolean {
  if (left === null || right === null) return left === right
  return left.enabled === right.enabled && left.start === right.start && left.end === right.end
}
