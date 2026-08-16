import { useState } from "react"

import { type Daemon, type useCreateIntegration } from "@/api/queries"
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
import { IntegrationForm } from "@/routes/integrations/IntegrationForm"
import { draftFrom, newBodyFrom, withoutUserinfo } from "@/routes/integrations/draft"

/**
 * Add an integration to one rack.
 *
 * The rack is fixed by the button that opened this, rather than chosen inside
 * it: an integration is per rack -- a screen can only bind to readings its own
 * rack polls -- so the section it was added from is the answer, and a rack
 * select here would be a second way to say the same thing that could be left
 * pointing somewhere else.
 *
 * There is no credential-carrying create to speak of yet, and the form says so
 * rather than hiding the field: a credential can be stored, on a **disabled**
 * row, and doing that is only useful as preparation for the M4 that can carry
 * it. Enabled plus a credential is refused by the server, and the warning is at
 * the switch.
 *
 * `type` is not offered. `NewIntegration.type` is `Literal["prometheus"]` and
 * there is exactly one type today; a select with one option is a control that
 * teaches nothing and a place for a second type to be forgotten.
 */
export function AddIntegrationDialog({
  rack,
  create,
}: {
  rack: Daemon
  /** The section's mutation, so what it answered outlives this dialog closing. */
  create: ReturnType<typeof useCreateIntegration>
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(() => draftFrom())
  const [scrubbed, setScrubbed] = useState(false)

  function change(next: boolean) {
    setOpen(next)
    if (!next) return
    // A fresh form every time, which for this dialog is also the rule that any
    // plaintext credential typed into an abandoned attempt does not survive it.
    setDraft(draftFrom())
    setScrubbed(false)
    create.reset()
  }

  const body = newBodyFrom(rack.id, draft)

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Add an integration to ${rack.name}`}</Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{`Add an integration to ${rack.name}`}</DialogTitle>
          <DialogDescription>
            {"A Prometheus this rack polls, and the readings it takes from it. Each field becomes " +
              "something a panel can bind to, under Data on the Screens page."}
          </DialogDescription>
        </DialogHeader>

        <IntegrationForm
          draft={draft}
          setDraft={setDraft}
          hasCredential={false}
          carried={[]}
          scrubbed={scrubbed}
        />

        {create.isError && (
          <p role="alert" className="text-sm text-destructive">
            {create.error.message}
          </p>
        )}

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            disabled={create.isPending || body === null}
            onClick={() => {
              if (body === null) return
              create.mutate(body, {
                // Closed on success only. A refusal stays up with the server's
                // own sentence in it and with what was typed, rather than
                // dismissing itself over a list that has not changed.
                onSuccess: () => setOpen(false),
                onError: () => {
                  const clean = withoutUserinfo(draft.url)
                  if (clean === draft.url) return
                  setDraft({ ...draft, url: clean })
                  setScrubbed(true)
                },
              })
            }}
          >
            Add the integration
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
