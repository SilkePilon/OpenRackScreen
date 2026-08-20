import { useId } from "react"

import { useClaims, type PendingClaim } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { ApproveClaimDialog } from "@/routes/daemons/ApproveClaimDialog"
import { DenyClaimDialog } from "@/routes/daemons/DenyClaimDialog"

/**
 * When the server first heard from this rack, as the instant it recorded.
 *
 * The route answers `first_seen` as a float of seconds since the epoch, and
 * nothing readable can be done with that as it stands. It is turned into the
 * same UTC ISO string every other timestamp in this interface shows -- see
 * `EventList`, which prints `event.at` exactly as the server wrote it -- for
 * that component's reason: this row is read beside a server log and a Pi's
 * journal, and the one thing that makes the three line up is that all of them
 * name the same instant the same way. The fraction is dropped because
 * `time.time()`'s microseconds say nothing to somebody comparing a claim
 * against a log line, and it is the only part of the value this discards.
 *
 * `null` for anything a `Date` cannot represent. The generated type says
 * `number`, so this is not a case the server produces -- but `toISOString`
 * answers a `RangeError` rather than a bad string, and an exception thrown
 * during render takes the whole Daemons page down with it, which is a steep
 * price for a field nobody acts on.
 */
function firstSeenAt(seconds: number): string | null {
  if (!Number.isFinite(seconds)) return null
  const at = new Date(seconds * 1000)
  if (Number.isNaN(at.getTime())) return null
  return at.toISOString().replace(/\.\d+Z$/, "Z")
}

/** One labelled fact about a waiting rack. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  )
}

/**
 * One rack asking to join: who it says it is, where it asked from, and the code.
 *
 * Everything here is the claimant's own account of itself and is drawn as such,
 * with one exception: the address. `file_claim_route` records
 * `request.client.host` and never reads an address out of the body, precisely
 * because a field the claimant fills in is a field the claimant chooses -- so
 * the address is the one line on this card the rack could not have made up.
 *
 * The fingerprint is not shown. It is the key both admin routes take and it is
 * nothing a person compares against anything: the six characters of its base32
 * are what the Pi printed, and those are what this card is for.
 */
function ClaimCard({ claim }: { claim: PendingClaim }) {
  const headingId = useId()
  const seen = firstSeenAt(claim.first_seen)

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h3 id={headingId} className="text-base font-medium">
          {claim.hostname}
        </h3>
        <p className="text-sm text-muted-foreground">
          Asking to join. It has been granted nothing and can be given nothing until it is
          approved.
        </p>
      </CardHeader>

      <CardContent>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label="Short code">
            <code className="font-mono tracking-widest">{claim.short_code}</code>
          </Fact>
          <Fact label="Asked from">{claim.address}</Fact>
          <Fact label="Daemon version">{claim.version}</Fact>
          <Fact label="First seen">
            {seen === null ? (
              "unknown"
            ) : (
              <time dateTime={seen} className="tabular-nums">
                {seen}
              </time>
            )}
          </Fact>
        </dl>
      </CardContent>

      <CardFooter className="flex flex-wrap gap-2">
        <ApproveClaimDialog claim={claim} />
        <DenyClaimDialog claim={claim} />
      </CardFooter>
    </Card>
  )
}

/**
 * Design spec S7's "Waiting to join", above the rack list and only when there
 * is something in it.
 *
 * **Nothing at all when nothing is pending -- not a heading, not an empty
 * state.** A labelled `<section>` is a landmark, and "Waiting to join" with
 * nothing under it is one a screen reader can enter and find nothing in. It is
 * the same rule `RackCanvas` follows for a rack with no panels, and the same
 * reason: on the ordinary day, which is every day after the racks are up, this
 * section is empty, so an empty rendering of it is what most people would
 * actually meet.
 *
 * A failed read is the one thing that is drawn without the heading. An `Alert`
 * is not a landmark -- it is a `role="alert"` div -- so this says what went
 * wrong without promising a section behind it; and it must be said, because the
 * alternative is a page that looks exactly like "no rack is waiting" while the
 * one waiting to be let in is invisible.
 */
export function PendingClaims() {
  const headingId = useId()
  const claims = useClaims()

  if (claims.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>The racks waiting to join could not be read</AlertTitle>
        <AlertDescription>
          {`${claims.error.message}. A rack asking to join would not be shown here until this can be read.`}
        </AlertDescription>
      </Alert>
    )
  }

  const pending = claims.data ?? []
  if (pending.length === 0) return null

  return (
    <section aria-labelledby={headingId} className="grid gap-3">
      <div className="grid gap-1">
        <h2 id={headingId} className="text-lg font-semibold">
          Waiting to join
        </h2>
        <p className="text-sm text-muted-foreground">
          {pending.length === 1
            ? "One rack has asked to join this server. Check its code against the Pi before you approve it."
            : `${pending.length} racks have asked to join this server. Check each code against its Pi before you approve it.`}
        </p>
      </div>

      <div className="grid gap-3">
        {pending.map((claim) => (
          <ClaimCard key={claim.fingerprint} claim={claim} />
        ))}
      </div>
    </section>
  )
}
