import { useId, useState } from "react"

import { ApiError } from "@/api/client"
import { useChangePassword } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * The two refusals this route has, told apart.
 *
 * The server refuses with 429 **before** it verifies anything, so reporting that
 * as a wrong password would send somebody off to remember a password that was
 * right all along -- and to type it again, keeping the window open. The 403 is
 * the other one, and its sentence is the server's own.
 */
function refusal(error: Error): { title: string; body: string } {
  const status = error instanceof ApiError ? error.status : 0
  if (status === 429) {
    return {
      title: "Too many attempts",
      body: `${error.message}: the server is refusing password attempts from here for a minute. Wait, then try again.`,
    }
  }
  return { title: "The password was not changed", body: error.message }
}

/** "2 other sign-ins were ended", from the count the route answered with. */
function saidAbout(ended: number): string {
  if (ended === 0) return "The password was changed. No other browser was signed in."
  const plural = ended === 1 ? "1 other sign-in was ended" : `${ended} other sign-ins were ended`
  return `The password was changed. ${plural}.`
}

/**
 * Change the admin password.
 *
 * **The current password is asked for and sent**, because the server requires
 * it: a session alone is a password reset for whoever is sitting at a browser
 * somebody left signed in. A form that posted without it would only be teaching
 * people to ignore a refusal.
 *
 * **The new password is typed twice.** There is no reset route on this server --
 * `POST /api/auth/setup` 409s the moment a password is set, atomically -- so a
 * mistyped new password is a rack nobody can sign in to again, with no way back
 * that does not involve the SQLite file. The confirmation therefore blocks the
 * request rather than describing the problem after it has been sent.
 *
 * **Neither plaintext outlives the request.** The three boxes are emptied when
 * the change lands, for the reason the integrations page forgets a credential:
 * what they hold is a password, and it has been used. Nothing here logs, and
 * nothing puts either password into a message -- what is rendered on a refusal
 * is the server's sentence, which names neither.
 *
 * What it does *not* do is claim the other sessions are gone in prose. The route
 * answers with a count, and a page that said "everyone else has been signed out"
 * would say it just as confidently on a server where nobody else ever was.
 */
export function PasswordCard() {
  const headingId = useId()
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const change = useChangePassword()

  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirmed, setConfirmed] = useState("")

  // Only once there is something to compare. A confirmation that is still being
  // typed is not yet a mismatch, and telling somebody they got it wrong on the
  // first keystroke is a warning they learn to read past.
  const mismatched = next !== "" && confirmed !== "" && next !== confirmed
  const ready = current !== "" && next !== "" && confirmed !== "" && !mismatched
  const refused = change.isError ? refusal(change.error) : null

  /** The last answer is not an answer about a form somebody has since edited. */
  function edit(set: (value: string) => void) {
    return (value: string) => {
      if (change.isSuccess || change.isError) change.reset()
      set(value)
    }
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!ready) return
    change.mutate(
      { current, new: next },
      {
        onSuccess: () => {
          setCurrent("")
          setNext("")
          setConfirmed("")
        },
      },
    )
  }

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          Admin password
        </h2>
        <p className="text-sm text-muted-foreground">
          The one password this server has. Changing it signs every other browser out and leaves
          this one signed in.
        </p>
      </CardHeader>
      <CardContent>
        <form className="grid max-w-sm gap-4" onSubmit={submit}>
          <div className="grid gap-2">
            <Label htmlFor={field("current")}>Current password</Label>
            <Input
              id={field("current")}
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(event) => edit(setCurrent)(event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={field("next")}>New password</Label>
            <Input
              id={field("next")}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => edit(setNext)(event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={field("confirmed")}>Confirm the new password</Label>
            <Input
              id={field("confirmed")}
              type="password"
              autoComplete="new-password"
              value={confirmed}
              onChange={(event) => edit(setConfirmed)(event.target.value)}
            />
          </div>

          {mismatched && (
            <p className="text-sm text-destructive">
              Those two are not the same, so the new password was typed differently twice. There is
              no way back into this server if the one that is stored is not the one you meant.
            </p>
          )}

          <Button type="submit" className="justify-self-start" disabled={!ready || change.isPending}>
            Change the password
          </Button>

          {refused && (
            <Alert variant="destructive">
              <AlertTitle>{refused.title}</AlertTitle>
              <AlertDescription>{refused.body}</AlertDescription>
            </Alert>
          )}
          {change.data && (
            <p className="text-sm text-muted-foreground">
              {/* `body` is optional because a success with an unparseable body
                  is a thing a proxy can produce, and "no other browser was
                  signed in" is then a claim from a number nobody read. */}
              {change.data.body === undefined
                ? "The password was changed."
                : saidAbout(change.data.body.other_sessions_ended)}
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  )
}
