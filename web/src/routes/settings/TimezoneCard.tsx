import { useId, useState } from "react"

import { useSaveSettings, type Daemon, type Settings } from "@/api/queries"
import { Landed } from "@/components/Landed"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * The zone every rack's clock is drawn in.
 *
 * A free text box and not a list of the four hundred zones this browser knows:
 * the column is a free string, the server refuses what it cannot resolve, and a
 * `<Select>` here would offer whatever *this* browser's tzdata holds while the
 * server and the Pi each have their own. What is needed is a name and a legible
 * refusal, which is what this is.
 *
 * **The refusal says what to do**, because the server's own sentence does not:
 * "no such timezone" names the value and not the rule. Two things a person can
 * act on -- the shape of the name, and the fact that the check is against this
 * server's tzdata rather than the rack's, so a zone accepted here can still be
 * one the Pi does not know. The server's docstring is explicit that its check is
 * "necessary rather than sufficient", and a page that implied otherwise would
 * send somebody looking for the wrong fault when the rack nacks hours later.
 *
 * The box follows the server while nobody is typing in it -- `null` is
 * untouched, not a value -- so an edit made in another tab arrives here, and the
 * value that is drawn after a save is the one that came back rather than the one
 * that was sent.
 */
export function TimezoneCard({ settings, racks }: { settings: Settings; racks: Daemon[] }) {
  const headingId = useId()
  const fieldId = useId()
  const save = useSaveSettings()
  const [typed, setTyped] = useState<string | null>(null)

  const shown = typed ?? settings.timezone
  const moved = shown !== settings.timezone && shown.trim() !== ""

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!moved) return
    // Only the field that moved. `night` in a timezone edit would write this
    // page's idea of the night window over whatever anybody else had just set,
    // and `PATCH /api/settings` takes its semantics from `exclude_unset`
    // precisely so it need not be sent.
    save.mutate({ timezone: shown }, { onSuccess: () => setTyped(null) })
  }

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          Timezone
        </h2>
        <p className="text-sm text-muted-foreground">
          What every rack&rsquo;s clock reads, and the zone the night window below is measured in.
        </p>
      </CardHeader>
      <CardContent>
        <form className="grid max-w-sm gap-4" onSubmit={submit}>
          <div className="grid gap-2">
            <Label htmlFor={fieldId}>Timezone</Label>
            <Input
              id={fieldId}
              value={shown}
              placeholder="Europe/Amsterdam"
              onChange={(event) => {
                if (save.isError) save.reset()
                setTyped(event.target.value)
              }}
            />
          </div>

          <Button type="submit" className="justify-self-start" disabled={!moved || save.isPending}>
            Save the timezone
          </Button>

          {save.isError && (
            <Alert variant="destructive">
              <AlertTitle>The timezone was not saved</AlertTitle>
              <AlertDescription>
                {`${save.error.message}. Give it an IANA zone name — Europe/Amsterdam, ` +
                  "America/New_York, UTC. This server checks against its own tzdata, so a name " +
                  "it accepts can still be one the Pi does not know; that arrives later as a " +
                  "refusal on the Daemons page."}
              </AlertDescription>
            </Alert>
          )}
          <Landed saved={save.data} racks={racks} what="The timezone was saved." />
        </form>
      </CardContent>
    </Card>
  )
}
