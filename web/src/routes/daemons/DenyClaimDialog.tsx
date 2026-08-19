import { useState } from "react"

import { useDenyClaim, type PendingClaim } from "@/api/queries"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"

/**
 * Refuse a rack, and say for how long before it is refused.
 *
 * A deny is not only "remove this row". `claims.deny` deletes the claim *and*
 * records the fingerprint as suppressed for `DENY_SUPPRESSION_S`, which is 24
 * hours -- design spec S6.5, whose reason is worth keeping in view: "a denied
 * rack that reappears every 5 seconds trains people to click Approve." So the
 * cost of a mis-click is a day in which the rack you actually wanted cannot get
 * back onto this page, and that has to be said before the button rather than
 * discovered afterwards.
 *
 * The other half is what the rack is told, which is nothing. `claims.deny`
 * removes the row, and a poll for a claim that is gone answers exactly the 404
 * an id nobody ever filed answers -- deliberately, so that nobody probing a
 * fingerprint can confirm it was the one an admin refused. A denied rack cannot
 * distinguish that from a server that never got round to it, so it keeps asking
 * in the background and reappears here when the day is up. An interface that
 * implied a deny reaches the Pi would be describing a message this protocol
 * does not send.
 */
export function DenyClaimDialog({ claim }: { claim: PendingClaim }) {
  const [open, setOpen] = useState(false)
  const deny = useDenyClaim(claim.fingerprint)

  function change(next: boolean) {
    setOpen(next)
    if (!next) deny.reset()
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">Deny</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Deny ${claim.hostname}?`}</DialogTitle>
          <DialogDescription>
            {"The claim is removed and this rack is refused for the next 24 hours, so it " +
              "cannot come back onto this page before then -- including if you meant to " +
              "approve it. The Pi is told nothing: a denial and a server that never " +
              "answered look the same from there, so it goes on asking in the background " +
              "and reappears here when the day is up."}
          </DialogDescription>
        </DialogHeader>
        {deny.isError && (
          <p role="alert" className="text-sm text-destructive">
            {deny.error.message}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={deny.isPending}
            // Closed on success only, as every confirmation on this page is: a
            // refused deny leaves the dialog up with the server's own sentence
            // in it, rather than dismissing itself and leaving the claim on the
            // page with nothing saying why.
            onClick={() => deny.mutate(undefined, { onSuccess: () => setOpen(false) })}
          >
            Deny this rack
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
