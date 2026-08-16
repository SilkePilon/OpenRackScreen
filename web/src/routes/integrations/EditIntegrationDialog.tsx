import { useState } from "react"

import { usePatchIntegration, type Integration } from "@/api/queries"
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
import { carriedKeys, changesFrom, draftFrom, withoutUserinfo } from "@/routes/integrations/draft"

/**
 * Edit one integration, sending exactly the fields that moved.
 *
 * **The credential is the reason this dialog is careful about what it sends.**
 * `credential` is write-only with three states -- absent leaves the stored
 * secret alone, `""` clears it, a value replaces it -- so a body that always
 * carried the field would wipe a stored secret every time somebody changed a
 * poll interval. `changesFrom` is where that is decided, once, and this dialog
 * does not add to what it produces.
 *
 * **A refused edit stays open**, holding what was set, so the server's own
 * sentence is next to the control that provoked it and the remedy is one click
 * away rather than a form to fill in again. Two refusals are worth staying open
 * for in particular: an enabled row holding a credential, whose answer is to
 * turn the switch back off, and an address carrying userinfo.
 *
 * **An address that carried a credential loses it when the edit is refused.**
 * The address is worth keeping and the password inside it is not keepable at
 * all: React writes a controlled input's value into the document as an
 * attribute, so a retained one would sit in the page for as long as the dialog
 * is open, in a form that anything reading the DOM can see. So `withoutUserinfo`
 * takes it out and the form says it did. It is not taken out *before* sending:
 * refusing a credential in a config is the server's rule, it applies at any
 * depth in a document this form only partly understands, and a copy of it here
 * would be a second rule to keep in step.
 */
export function EditIntegrationDialog({
  integration,
  save,
}: {
  integration: Integration
  /**
   * The card's mutation, passed down rather than mounted here.
   *
   * What a write answered -- including the racks it could not be pushed to --
   * has to outlive the dialog closing over it, and a hook inside this component
   * is unmounted the moment the edit succeeds.
   */
  save: ReturnType<typeof usePatchIntegration>
}) {
  const [open, setOpen] = useState(false)
  const [initial, setInitial] = useState(() => draftFrom(integration))
  const [draft, setDraft] = useState(initial)
  const [scrubbed, setScrubbed] = useState(false)

  function change(next: boolean) {
    setOpen(next)
    if (!next) return
    // Opening starts from the row as it is *now* -- another tab moves it under
    // this dialog -- and drops whatever was typed and abandoned last time,
    // including any plaintext credential, which must not outlive the form that
    // asked for it. The last write's answer is cleared for the same reason the
    // Templates page clears it: it is on the card, and left standing it reads as
    // this edit's.
    const fresh = draftFrom(integration)
    setInitial(fresh)
    setDraft(fresh)
    setScrubbed(false)
    save.reset()
  }

  const body = changesFrom(integration, initial, draft)
  // `null` is "this draft cannot be sent"; an empty object is "nothing moved".
  // Both disable the button and the form says which it is, but they are not the
  // same state: an empty PATCH is accepted by the server and still bumps this
  // rack's config_version and pushes a fresh snapshot for an edit nobody made.
  const nothingToSave = body === null || Object.keys(body).length === 0

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Edit ${integration.name}`}</Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{`Edit ${integration.name}`}</DialogTitle>
          <DialogDescription>
            {"Only what changes here is sent. Saving pushes a fresh configuration to this rack, " +
              "and the credential is never read back — the server has no field to send it in."}
          </DialogDescription>
        </DialogHeader>

        <IntegrationForm
          draft={draft}
          setDraft={setDraft}
          storedName={integration.name}
          hasCredential={integration.has_credential}
          carried={carriedKeys(integration.config)}
          scrubbed={scrubbed}
        />

        {save.isError && (
          <p role="alert" className="text-sm text-destructive">
            {save.error.message}
          </p>
        )}

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            disabled={save.isPending || nothingToSave}
            onClick={() => {
              if (body === null) return
              save.mutate(body, {
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
            Save the integration
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
