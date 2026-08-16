import type { Daemon } from "@/api/queries"
import { nameDaemons } from "@/api/unservable"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"

/**
 * What a write said, once it has landed.
 *
 * Two shapes and one rule: an edit that reached every affected rack says so
 * quietly, and one that did not names the racks -- from `X-Unservable-Daemons`
 * and from nothing else.
 *
 * The header is the *only* thing that can name them. On the Templates page that
 * is starkest -- a template edit affects every rack there is, because those
 * routes never call `affects`, so `Change` reads the affected set as all of
 * them and the response can never be narrowed to a 202 -- but it is no less true
 * of an integration write, which does call `affects` and still answers 201 on a
 * create that reached nobody. There is no body field, no status code and no rack
 * id in the request that says which rack missed one.
 *
 * In `components/` rather than beside one page for the reason `nameDaemons` was
 * lifted out of the pages in task 14: two pages now say this sentence, and two
 * copies of it are two chances for one of them to start reading the body.
 */
export function Landed({
  saved,
  racks,
  what,
}: {
  /** The mutation's answer, or `undefined` because nothing has been written yet. */
  saved: { unservable: number[] } | undefined
  racks: Daemon[]
  /** What to say when every rack got it. */
  what: string
}) {
  if (saved === undefined) return null
  if (saved.unservable.length === 0) {
    return <p className="text-sm text-muted-foreground">{what}</p>
  }
  return (
    <Alert variant="destructive">
      <AlertTitle>Not every rack was given that change</AlertTitle>
      <AlertDescription>
        {`${nameDaemons(saved.unservable, racks)}: the change was saved, but nothing was sent. ` +
          "Each of those racks has a reason on the Daemons page, and the usual recovery is the " +
          "next edit plus a push."}
      </AlertDescription>
    </Alert>
  )
}
