import { useDaemons, useSettings } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { NightWindowCard } from "@/routes/settings/NightWindowCard"
import { PasswordCard } from "@/routes/settings/PasswordCard"
import { TimezoneCard } from "@/routes/settings/TimezoneCard"

/**
 * The three things that belong to the server rather than to a rack.
 *
 * Spec 5.8's list, in its order: the admin password, the timezone, the global
 * night window. The password is a credential and goes nowhere near a rack; the
 * other two are **rack-wide** -- `build_snapshot` reads both for whichever
 * daemon it is assembling, so `PATCH /api/settings` never calls `affects` and
 * every rack there is gets a fresh snapshot for either edit.
 *
 * **Which is why this page asks for the racks.** Not to draw them: an edit that
 * reaches every rack can still fail to reach one of them, the response says so
 * only in `X-Unservable-Daemons`, and the header answers in ids that have to
 * become names somebody can go and look at. A rack-wide route can never be
 * narrowed to 202 -- one rack that was already unservable would otherwise turn
 * every settings edit into one forever -- so there is no status code carrying
 * this either.
 *
 * **No form is drawn before `GET /api/settings` has answered.** A night window
 * built from `NightWindow`'s defaults while the request is still out reads as
 * "this server sleeps 23:00 to 07:00", and is one press of Save away from making
 * that true on every panel that was set to something else. A fetch in flight and
 * a fetch that failed are both said out loud instead.
 */
export function SettingsPage() {
  const settings = useSettings()
  const racks = useDaemons()

  return (
    <>
      <h1 className="text-2xl font-semibold">Settings</h1>
      <p className="max-w-prose text-sm text-muted-foreground">
        The server&rsquo;s own settings. The timezone and the night window are read by every rack,
        so a change to either is a change to all of them.
      </p>

      <PasswordCard />

      {settings.isPending && <p className="text-sm text-muted-foreground">Reading the settings&hellip;</p>}
      {settings.isError && (
        <Alert variant="destructive">
          <AlertTitle>The settings could not be read</AlertTitle>
          <AlertDescription>{settings.error.message}</AlertDescription>
        </Alert>
      )}
      {/* The racks are a separate fetch and this page draws none of them -- what
          it needs them for is turning the ids in `X-Unservable-Daemons` into
          names. `nameDaemons` falls back to "rack 26" for an id it has no row
          for, so an unread listing degrades rather than hiding a rack; it is
          said out loud all the same, because a page that named racks by number
          while claiming nothing was wrong would read as a bug in the notice. */}
      {racks.isError && (
        <Alert variant="destructive">
          <AlertTitle>The racks could not be read</AlertTitle>
          <AlertDescription>{racks.error.message}</AlertDescription>
        </Alert>
      )}

      {settings.data && (
        <>
          <TimezoneCard settings={settings.data} racks={racks.data ?? []} />
          <NightWindowCard settings={settings.data} racks={racks.data ?? []} />
        </>
      )}
    </>
  )
}
