import { useId, useState } from "react"

import { api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import { daemonsKey, type DaemonCreated } from "@/api/queries"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * The token, the line that carries it, and the warning that it will not be
 * shown again.
 *
 * A component rather than a copy in each dialog, because two routes mint a
 * token -- `POST /api/daemons` and `POST /api/daemons/{id}/rotate-key` -- and
 * the rule they share is the one thing here that must not drift between them:
 * the server holds a hash and nothing else, so this render is the only copy of
 * the token that will ever exist, and the sentence saying so has to be beside
 * it rather than in a manual. Losing it is not fatal, and saying which
 * recovery it costs -- rotating, which leaves the rack unpaired until the new
 * token reaches it -- is the difference between a warning and a scare.
 *
 * `--server` is this browser's own origin, and that is not a guess: the
 * interface is served by the server it talks to, so the URL in the address bar
 * is the URL the Pi has to dial. Both flags are required by `ors-daemon
 * connect`, so a line with either one missing is not runnable.
 */
export function TokenOnce({ token }: { token: string }) {
  const command = `ors-daemon connect --server ${window.location.origin} --token ${token}`
  return (
    <div className="grid gap-3">
      <div className="grid gap-1">
        <p className="text-xs text-muted-foreground">The pairing token</p>
        <code className="rounded-md bg-muted px-2 py-1.5 font-mono text-sm break-all">
          {token}
        </code>
      </div>
      <div className="grid gap-1">
        <p className="text-xs text-muted-foreground">Run this on the Pi</p>
        <code className="rounded-md bg-muted px-2 py-1.5 font-mono text-sm break-all">
          {command}
        </code>
      </div>
      <p className="text-sm font-medium">
        {"Copy it now: this is the only time it is shown. The server keeps a hash and " +
          "nothing else, so if you lose it you will have to rotate this rack's key for a " +
          "new one, and the rack stays unpaired until that one reaches it."}
      </p>
    </div>
  )
}

/**
 * Mint a rack and the token that pairs it.
 *
 * Pairing happens here and nowhere else -- there is no CLI for it -- so this
 * dialog has two states and they are not two steps of a form: the second one is
 * an answer that cannot be asked for again. It is left up until the person
 * dismisses it, and dismissing it is what ends the "once": closing resets the
 * mutation, and nothing on the page or in the cache is holding the token
 * afterwards.
 *
 * The name is kept in form state rather than being read back from the server,
 * so a refused create -- 409 for a name that exists -- leaves what was typed
 * where it was, next to the sentence saying why it was refused.
 */
export function PairDialog() {
  const nameId = useId()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")

  const create = useMutate<DaemonCreated, string>({
    send: (wanted) => api.POST("/api/daemons", { body: { name: wanted } }),
    invalidates: [daemonsKey],
  })
  const minted = create.data?.body

  function change(next: boolean) {
    setOpen(next)
    if (!next) {
      setName("")
      // The whole of "exactly once": the only copy of the token this interface
      // ever held was `create.data`, and this is it going away.
      create.reset()
    }
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button>Pair a rack</Button>
      </DialogTrigger>
      <DialogContent>
        {minted ? (
          <>
            <DialogHeader>
              <DialogTitle>{`${minted.name} is waiting for its token`}</DialogTitle>
              <DialogDescription>
                The rack connects on its own once this has run, and appears here as paired.
              </DialogDescription>
            </DialogHeader>
            <TokenOnce token={minted.token} />
            <DialogFooter>
              <DialogClose asChild>
                <Button>Done</Button>
              </DialogClose>
            </DialogFooter>
          </>
        ) : (
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault()
              create.mutate(name.trim())
            }}
          >
            <DialogHeader>
              <DialogTitle>Pair a rack</DialogTitle>
              <DialogDescription>
                {"A name for the Pi, so you can tell it from the others. It can be changed " +
                  "later. This is the way in for a rack that cannot find this server on " +
                  "its own -- a network that drops multicast, or a Pi on another subnet. " +
                  "A rack that can find it asks to join instead, and is let in from this " +
                  "page without a token ever being typed."}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor={nameId}>Name</Label>
              <Input
                id={nameId}
                value={name}
                autoComplete="off"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            {create.isError && (
              <p role="alert" className="text-sm text-destructive">
                {create.error.message}
              </p>
            )}
            <DialogFooter>
              <Button type="submit" disabled={create.isPending || name.trim() === ""}>
                Mint the pairing token
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
