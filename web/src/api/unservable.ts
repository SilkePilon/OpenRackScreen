/**
 * The racks an edit could not be given to, read from the response header.
 *
 * The header rather than the status code, and this is the whole reason the
 * function exists: `POST /api/screens` answers 201 even when nothing was
 * pushed, because the row does exist and its representation is in the body.
 * An interface that branched on `status === 202` would miss every create.
 */
export function parseUnservable(headers: Headers): number[] {
  const raw = headers.get("X-Unservable-Daemons")
  if (!raw) return []
  return raw
    .split(",")
    .map((part) => part.trim())
    // Emptiness is checked before the number, and not merged into the
    // `Number.isInteger` filter below, because `Number("")` is `0` rather than
    // `NaN` -- so a header of "3,,7" would otherwise name rack 0, which is a
    // rack no database has (SQLite's rowids begin at 1) and which the interface
    // would then tell somebody to go and look at.
    .filter((part) => part.length > 0)
    .map((part) => Number(part))
    // Everything else that is not a number is `NaN`, and `Number.isInteger`
    // is what stops "rack NaN did not get it" reaching a person.
    .filter((id) => Number.isInteger(id))
}
