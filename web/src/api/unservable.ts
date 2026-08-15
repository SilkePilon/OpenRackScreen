/**
 * The racks an edit could not be given to, read from the response header.
 *
 * The header rather than the status code, and this is the whole reason the
 * function exists: `POST /api/screens` answers 201 even when nothing was
 * pushed, because the row does exist and its representation is in the body.
 * An interface that branched on `status === 202` would miss every create.
 *
 * The grammar it accepts is exactly the one the server writes and no wider:
 * decimal digits, comma-separated, spacing allowed around each id
 * (`changes.py` joins `str(id)` over a sorted set of rowids). A part that is
 * not that -- empty, a word, `-4`, `0x10`, `1e3` -- names no rack and is
 * dropped. The test is on the *text* rather than on the number, because
 * `Number` reads empty as `0`, `0x10` as `16` and `1e3` as `1000`, and
 * `Number.isInteger` agrees with all three (and with `-4`): a parser on the
 * number would answer "rack 16 did not get it" for a header that never
 * mentioned rack 16, and rack 0 exists in no database (SQLite's rowids begin
 * at 1). Every one of these is a rack somebody would be sent to go and look at.
 */
export function parseUnservable(headers: Headers): number[] {
  const raw = headers.get("X-Unservable-Daemons")
  if (!raw) return []
  return raw
    .split(",")
    .map((part) => part.trim())
    // Anchored at both ends: an unanchored test passes "0x10" on its "10".
    .filter((part) => /^[0-9]+$/.test(part))
    .map((part) => Number(part))
}
