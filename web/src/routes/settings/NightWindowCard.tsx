import { useId, useState } from "react"

import { useSaveSettings, type Daemon, type Settings } from "@/api/queries"
import { Landed } from "@/components/Landed"
import { NightWindowFields } from "@/components/NightWindowFields"
import { WRAP_NOTE, describeNight, sameNight, type NightWindow } from "@/components/night"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"

/**
 * When every panel on this server is dark, unless it overrides it.
 *
 * The controls are `NightWindowFields`, the same ones the Screens inspector's
 * Sleep tab draws, because this and that are one edit made in two places and the
 * two must not drift. What differs is what surrounds them: there is always a
 * window here, so there is no override switch and no three-deep fallback -- this
 * card is only rendered once `GET /api/settings` has answered.
 *
 * **A start later than the end is the normal case and is drawn as one.** Nothing
 * here refuses it, swaps it, or marks it. "Dark between 22:45 and 06:20" is a
 * night; "dark between 06:20 and 22:45" is a working day, and a card that
 * quietly reordered the two times would leave four panels lit all night and the
 * form still showing what was typed.
 *
 * Each field is `null` until it is touched, so a window edited in another tab
 * arrives here rather than being held off by a form that seeded itself once.
 */
export function NightWindowCard({ settings, racks }: { settings: Settings; racks: Daemon[] }) {
  const headingId = useId()
  const save = useSaveSettings()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [start, setStart] = useState<string | null>(null)
  const [end, setEnd] = useState<string | null>(null)

  const held = settings.night
  const shown: NightWindow = {
    enabled: enabled ?? held.enabled,
    start: start ?? held.start,
    end: end ?? held.end,
  }
  const moved = !sameNight(shown, held)

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!moved) return
    // `night` alone: a body that also carried the timezone would write this
    // page's copy of it over an edit somebody made between the read and this
    // save.
    save.mutate(
      { night: shown },
      {
        onSuccess: () => {
          setEnabled(null)
          setStart(null)
          setEnd(null)
        },
      },
    )
  }

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          Night window
        </h2>
        <p className="text-sm text-muted-foreground">
          When every panel on this server goes dark. A panel can be given a window of its own on
          the Screens page, and one that has been keeps it.
        </p>
      </CardHeader>
      <CardContent>
        <form className="grid max-w-sm gap-4" onSubmit={submit}>
          <NightWindowFields
            value={shown}
            onEnabled={setEnabled}
            onStart={setStart}
            onEnd={setEnd}
          />

          <p className="text-xs text-muted-foreground">
            {`Every panel on this server: ${describeNight(shown, settings.timezone)} ${WRAP_NOTE}`}
          </p>

          <Button type="submit" className="justify-self-start" disabled={!moved || save.isPending}>
            Save the night window
          </Button>

          {save.isError && (
            <Alert variant="destructive">
              <AlertTitle>The night window was not saved</AlertTitle>
              <AlertDescription>{save.error.message}</AlertDescription>
            </Alert>
          )}
          <Landed saved={save.data} racks={racks} what="The night window was saved." />
        </form>
      </CardContent>
    </Card>
  )
}
