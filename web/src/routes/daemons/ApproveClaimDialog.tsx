import { useState } from "react"

import { ApiError } from "@/api/client"
import { useApproveClaim, type PendingClaim } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
 * The one gate in the whole joining flow, and what it is honest to say about it.
 *
 * `POST /api/racks/claims` is unauthenticated by necessity -- a rack that has
 * not been approved holds no credential, which is the entire reason the claim
 * protocol exists rather than a shared secret -- so **anyone on the network can
 * put an entry on the page behind this button**. Design spec S6.4: "The only
 * gate is an authenticated admin's click."
 *
 * That makes the short code the only thing that makes the click mean anything.
 * It is printed by `ors-daemon install` and repeated in `journalctl -u
 * openrackscreen`, and the person clicking is meant to have both in front of
 * them. So it is shown here, large, before the button rather than after it, and
 * the sentence the spec asks for in those words -- approving without comparing
 * it is approving a stranger's rack onto your server -- is beside it.
 *
 * **And what the code does not prove is said too.** Its thirty bits are a check
 * against confusing the racks an admin is actually choosing between, not
 * against an attacker: grinding secrets until the first thirty bits of their
 * SHA-256 match a code already seen on somebody's screen is about 2^30 hashes,
 * which is seconds. A dialog that presented six characters as proof of identity
 * would be overstating the one guarantee this entire milestone rests on, and
 * `identity.py`'s own docstring says the same thing at the other end.
 *
 * What approving grants is named rather than left to be inferred: this server's
 * configuration goes to the rack, and the rack draws on its own panels. The key
 * is minted and sealed to this claim's ephemeral public key on the server; there
 * is nothing to show once and forget here, which is what makes this dialog
 * shorter than `PairDialog`'s and not longer.
 */
export function ApproveClaimDialog({ claim }: { claim: PendingClaim }) {
  const [open, setOpen] = useState(false)
  const approve = useApproveClaim(claim.fingerprint)

  function change(next: boolean) {
    setOpen(next)
    if (!next) approve.reset()
  }

  // A `daemon.name` collision, which is the one refusal with a remedy behind
  // it. The rack joins under its own hostname, and 409 means a *different* rack
  // already holds that name here -- `claims.approve` reclaims a name only from
  // an earlier uncollected grant to this same fingerprint, and lets everything
  // else raise. The transaction rolls back whole, so the claim is exactly as
  // pending and as deniable as it was a moment ago, and saying so is half of
  // what makes this recoverable rather than alarming. Rendered as its own
  // sentence rather than as the generic red line every other refusal gets,
  // because the generic line names nothing an admin can act on.
  const collision = approve.error instanceof ApiError && approve.error.status === 409

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button>Approve</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Approve ${claim.hostname}?`}</DialogTitle>
          <DialogDescription>
            {"Anyone on this network can ask to join, so the code below is the check. It " +
              "must match the one this Pi printed when it was installed, which it also " +
              "repeats in its log. Approving without comparing it is approving a " +
              "stranger's rack onto your server."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-1">
          <p className="text-xs text-muted-foreground">The code this rack sent</p>
          <code className="rounded-md bg-muted px-3 py-2 text-center font-mono text-3xl font-medium tracking-[0.35em]">
            {claim.short_code}
          </code>
        </div>

        <div className="grid gap-2 text-sm">
          <p>
            {`Approving creates ${claim.hostname} as a rack and mints its key. From then on it ` +
              "receives this server's configuration and draws on its own panels."}
          </p>
          <p className="text-muted-foreground">
            {"Six characters is 30 bits. That is enough to tell one rack you are choosing " +
              "between from another; it is not proof against somebody who has already seen " +
              "this code and is racing you to be approved under it."}
          </p>
        </div>

        {collision && (
          <Alert variant="destructive">
            <AlertTitle>{`Another rack here is already called ${claim.hostname}`}</AlertTitle>
            <AlertDescription>
              {`The server said: ${approve.error?.message}. A rack joins under its own ` +
                "hostname, and that one is taken by a different rack. Rename the rack that " +
                "has it, or change this Pi's hostname and let it ask again. This claim is " +
                "still waiting either way, so nothing was lost by trying."}
            </AlertDescription>
          </Alert>
        )}
        {approve.isError && !collision && (
          <p role="alert" className="text-sm text-destructive">
            {approve.error.message}
          </p>
        )}

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            disabled={approve.isPending}
            // Closed on success only, for `DeleteDaemonDialog`'s reason: an
            // approval that lands normally takes its own entry off the page
            // when the list is re-read, but a re-read that fails leaves the
            // entry -- and the dialog -- exactly where they were, asking again
            // about a claim that has already been granted.
            onClick={() => approve.mutate(undefined, { onSuccess: () => setOpen(false) })}
          >
            Approve this rack
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
