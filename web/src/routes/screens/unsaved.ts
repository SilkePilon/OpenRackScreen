import { useEffect, useRef } from "react"

/**
 * Say once, when a form starts holding an edit that has not been saved yet.
 *
 * What this exists for: the inspector's "Saved, and every rack was given it."
 * is an answer about a write that has happened, and it stops describing what is
 * on screen the moment somebody types the next edit. So each tab tells the
 * inspector when it has become unsaved, and the inspector forgets the last
 * write.
 *
 * **Derived from the form's own state rather than announced by each control.**
 * The first version of this called the inspector from every `onChange`,
 * `onValueChange` and `onCheckedChange` in the three tabs -- fourteen call sites
 * in `ConfigTab` alone, each identical, each individually forgettable. A
 * mutation run said so: dropping the call from the wiring setters alone killed
 * no test, and there is no honest number of tests that pins fourteen copies of
 * one rule. A form is unsaved or it is not, that fact is already computed to
 * decide whether Save is enabled, and a control added tomorrow is covered by
 * having been added to the form at all.
 *
 * The transition is what is reported, not the state: `unsaved` stays true for
 * as long as somebody keeps typing, and the last write is forgotten once.
 * Mounting already unsaved is not an edit either -- a tab builds its form from
 * the row, so that only happens when the row and the form disagree about
 * something nobody touched.
 *
 * **The report is only discharged when the caller takes it**, which is what
 * `began`'s answer is for. A write that is still in flight has an outcome
 * nobody has seen, so the inspector cannot act on the edit yet and says so by
 * answering `false`; the transition is then still owed, and is delivered on the
 * next render -- the one the mutation settling causes. Without that, somebody
 * typing *during* a PATCH got a clean-to-unsaved edge that was swallowed, and
 * "Saved, and every rack was given it." stood over a form holding newer edits
 * with no further transition left to clear it.
 */
export function useUnsaved(unsaved: boolean, began: () => boolean) {
  // Not in a dependency array: `began` is written inline by the caller and is a
  // new function on every render, and the ref below is what makes this fire
  // once regardless. So the effect runs after every render and does nothing on
  // almost all of them.
  const before = useRef(unsaved)
  useEffect(() => {
    // Left where it was when the report was refused, so it is made again.
    if (unsaved && !before.current && !began()) return
    before.current = unsaved
  })
}
