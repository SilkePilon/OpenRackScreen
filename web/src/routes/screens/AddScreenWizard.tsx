import { useId, useState } from "react"

import { ApiError, api } from "@/api/client"
import { useMutate } from "@/api/mutate"
import {
  screensKey,
  useDetect,
  useProbe,
  useTemplates,
  type PanelCandidate,
  type Screen,
} from "@/api/queries"
import type { components } from "@/api/schema"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type NewScreen = components["schemas"]["NewScreen"]
type DisplayConfig = components["schemas"]["DisplayConfig"]

/**
 * How long a probe asks the rack to hold a panel lit, in seconds.
 *
 * **5.0 and never more, and this is the only place the number is written.**
 * `PROBE_HOLD_BUDGET` on the daemon is 5.0; a longer `hold_s` is cut to it
 * *silently* -- `_capped_hold` takes the `min` and `ProbeResult` carries no
 * field saying what was really honoured, so nothing that comes back could tell
 * an interface it had asked for too much. A wizard counting an operator down
 * from ten therefore has the panel go dark at five and then asks them whether
 * they saw it, and "no, nothing lit" is the answer it gets about wiring that is
 * perfectly good.
 *
 * `openapi-typescript` does not carry OpenAPI's `maximum` into the generated
 * type -- `ProbeBody.hold_s` is plain `number` -- so nothing in the type system
 * stops a larger value going out. The bound is this constant's to keep, and it
 * is used twice: it is what goes on the wire, and it is what the operator is
 * told the panel will be lit for. One number, so the two cannot drift.
 */
export const PROBE_HOLD_S = 5

/**
 * The clock a panel is proved at when the rack has none to copy.
 *
 * `DisplayConfig.hz`'s own default, restated here because a probe cannot fall
 * back on it: `ProbeBody` defaults nothing at all, on purpose -- "a probe that
 * filled a field in from a default would be proving wiring nobody chose and
 * reporting it as the wiring that was asked for". So the box is filled in
 * visibly, with the number the screen would be created with, and the operator
 * can see it before it is proved rather than after.
 */
export const DEFAULT_HZ = 40_000_000

/** The wiring, as text, because "not filled in" is a state a number cannot hold. */
type WiringDraft = {
  dc: string
  rst: string
  hz: string
}

/** The same three, once they are numbers a rack can be asked about. */
type Pinout = {
  dc: number
  rst: number
  hz: number
}

/** A whole number, or `null` because the box does not hold one. */
function intOf(text: string): number | null {
  if (text.trim() === "") return null
  const value = Number(text)
  // Integers only. `Number("")` is 0 and `Number.isFinite(0)` is true, which is
  // the pair that has bitten this project four times now: an empty pin box read
  // as a number is a request to prove GPIO 0, which is a real pin on a real
  // board and is not what anybody meant by leaving it blank.
  return Number.isInteger(value) ? value : null
}

function pinoutOf(draft: WiringDraft): Pinout | null {
  const dc = intOf(draft.dc)
  const rst = intOf(draft.rst)
  const hz = intOf(draft.hz)
  if (dc === null || rst === null || hz === null) return null
  return { dc, rst, hz }
}

/**
 * The wiring to start the operator from: the rack's own, from its last panel.
 *
 * **This is the whole reason the wizard has a wiring step.** A GC9A01 has no
 * readable id over 4-wire SPI and DC and RST are GPIO lines somebody chose with
 * a screwdriver, so enumeration can say which `/dev/spidev*` exist and nothing
 * whatever about what drives them. A panel cannot introduce itself. The only
 * thing this interface knows about a rack's soldering is the rack's own
 * configured panels -- and a rack is usually wired the same way throughout,
 * because it was wired in one afternoon by one person.
 *
 * **The last panel, and the pair from one panel.** Last, because a rack is
 * ordered left to right and the panel most recently added sits at the end, so it
 * is the one whose pinout was most recently in somebody's hand -- and the list
 * arrives `ORDER BY position, id`, so "last" is the server's order and not this
 * interface's. From *one* panel, because a pinout is a pair: taking the
 * commonest `dc` and the commonest `rst` across a rack could compose a pair no
 * panel on it actually has, and proving that would prove nothing about anything
 * that exists.
 *
 * A rack with no gc9a01 panel to copy gets **empty** pin boxes. Not 0:
 * `DisplayConfig` gives `dc` and `rst` no default for exactly this reason, and
 * an interface that offered 0 would be proposing a board nobody built.
 */
function prefill(screens: Screen[]): WiringDraft {
  for (let index = screens.length - 1; index >= 0; index -= 1) {
    const display = screens[index].display
    if (display.backend !== "gc9a01") continue
    const { dc, rst, hz } = display
    if (typeof dc !== "number" || typeof rst !== "number") continue
    return {
      dc: String(dc),
      rst: String(rst),
      hz: String(typeof hz === "number" ? hz : DEFAULT_HZ),
    }
  }
  return { dc: "", rst: "", hz: String(DEFAULT_HZ) }
}

/**
 * Where the new panel goes: one past the rightmost one on this rack.
 *
 * One past the *highest position* rather than one past the count, because
 * `position` is not required to be dense -- deleting a panel renumbers nothing,
 * so a rack of three can be sitting at 1, 2 and 6 -- and count-plus-one would
 * then land on top of a panel that is already there. An empty rack starts at 1,
 * which is where `ScreenBody.position`'s `ge=1` starts; `Math.max()` of nothing
 * is `-Infinity`, which is why this counts rather than reduces.
 */
function nextPosition(screens: Screen[]): number {
  let highest = 0
  for (const each of screens) if (each.position > highest) highest = each.position
  return highest + 1
}

/** `/dev/spidev<bus>.<cs>`, as the daemon's own log and the operator both write it. */
function deviceName(candidate: PanelCandidate): string {
  return `SPI${candidate.bus}.${candidate.cs}`
}

/**
 * What each status this pair of routes can answer means, and what to do about it.
 *
 * Six of them, and they send somebody to six different places -- which is the
 * entire argument for not collapsing them into "the request failed". The
 * server's own `detail` already names a working next step for four of them, so
 * it is rendered as it arrived and never replaced: "start the daemon on the Pi
 * and watch GET /api/daemons for it to come online" is better advice than
 * anything this file could invent, and it is the sentence written by the code
 * that knows why it refused. What is added here is the heading that makes the
 * kind of failure findable at a glance, and an extra line for the two statuses
 * whose sentence names no step at all.
 *
 * **409 is the one that must not read as a verdict on the wiring.** It means
 * another probe is in flight on that rack and the bus is held -- for at most
 * `PROBE_HOLD_BUDGET` -- and the pins in the boxes have not been tested yet, let
 * alone found wanting. Drawn as a failure it sends somebody back to the rack
 * with a screwdriver to fix something that was never broken. It is the only
 * status here that is answered by waiting, and `kind` is what says so.
 *
 * **409 does not mean this on every route.** `POST /api/daemons/{id}/command`
 * answers 409 for a rack that is *not connected*, where these two answer 503.
 * This mapping is therefore only correct for detect and probe, which is why it
 * lives beside them rather than in the shared client.
 */
type RefusalKind = "busy" | "refused"

function refusalOf(status: number): { title: string; kind: RefusalKind; advice: string | null } {
  switch (status) {
    case 503:
      return { title: "That rack is not connected", kind: "refused", advice: null }
    case 504:
      return { title: "The rack did not answer", kind: "refused", advice: null }
    case 502:
      return { title: "The rack answered something else", kind: "refused", advice: null }
    case 409:
      return {
        title: "That rack is already probing a panel",
        kind: "busy",
        advice:
          `The bus is busy, and that is all this says: the wiring in the boxes has not been ` +
          `tested yet. A probe holds a rack's bus for at most ${PROBE_HOLD_S} seconds, so try ` +
          `again in a moment.`,
      }
    case 404:
      return {
        title: "There is no such rack",
        kind: "refused",
        advice:
          "It is not in this server's list any more -- another tab may have removed it. " +
          "Reload the Screens page to see what is still paired.",
      }
    case 422:
      return {
        title: "The request was refused before it reached the rack",
        kind: "refused",
        advice: "Nothing was sent to the rack, so no panel was touched.",
      }
    default:
      return { title: "The request was refused", kind: "refused", advice: null }
  }
}

/**
 * A refusal, with the server's own words in it.
 *
 * `ApiError` carries the status; anything else that reached a mutation's `error`
 * is not an HTTP answer at all and falls to the last case, which says the least
 * and invents nothing.
 */
function Refusal({ error }: { error: Error }) {
  const status = error instanceof ApiError ? error.status : 0
  const { title, kind, advice } = refusalOf(status)
  return (
    <Alert data-refusal={kind} variant={kind === "busy" ? "default" : "destructive"}>
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="grid gap-2">
        <p>{error.message}</p>
        {advice !== null && <p>{advice}</p>}
      </AlertDescription>
    </Alert>
  )
}

/** Which of the four steps is on screen. */
type Step = "detect" | "wiring" | "probe" | "add"

const STEPS: { step: Step; title: string; blurb: string }[] = [
  {
    step: "detect",
    title: "Find the devices",
    blurb: "The rack lists the SPI devices it has, and says which ones it is already driving.",
  },
  {
    step: "wiring",
    title: "Confirm the wiring",
    blurb:
      "Which GPIO lines drive data/command and reset. Nothing on the bus reports them, so this " +
      "is the one thing only you can say.",
  },
  {
    step: "probe",
    title: "Prove the panel",
    blurb: "The rack lights that one panel and holds it, and you say whether you saw it.",
  },
  {
    step: "add",
    title: "Add the screen",
    blurb: "Created with the wiring that was just proved.",
  },
]

/**
 * Detect, confirm, probe, add -- and why it is four steps rather than a button.
 *
 * The Pi can enumerate `/dev/spidev*`: which buses and chip selects exist is a
 * fact its kernel knows. It cannot discover DC and RST. Those are GPIO pins
 * chosen when the panel was wired, nothing on the bus reports them, and a
 * GC9A01 has no readable id over 4-wire SPI -- a panel cannot introduce itself.
 * So detection is enumeration *plus a guided probe*: the rack says what devices
 * exist and which it is already driving, the operator supplies the wiring, and
 * the rack proves it by lighting that panel while somebody watches.
 *
 * Each step's state is held here rather than in the steps, because every one of
 * them is read by a later step: the device decides what is probed and what is
 * created, the wiring is probed and then saved unchanged, and the confirmation
 * is the only thing that makes the last step reachable at all.
 *
 * **`ok: true` is not a confirmation.** It means "the device opened and the
 * pattern was written", never "somebody saw it": a panel on the wrong DC line
 * can open, take a frame and show nothing. Only the person in front of the rack
 * can answer that, so `step` reaches `"add"` through a press of *Yes, that panel
 * lit up* and through no other path in this file.
 *
 * **A failed probe goes back one step, not to the beginning.** The detect is
 * still true and the device is still the device; what was wrong was a pin. So
 * the boxes keep what was tried and one keystroke is the whole of the fix.
 */
function Wizard({
  daemonId,
  rackName,
  screens,
  onAdded,
}: {
  daemonId: number
  rackName: string
  /** This rack's panels, in the server's order. The only wiring there is to copy. */
  screens: Screen[]
  onAdded: () => void
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`

  const [step, setStep] = useState<Step>("detect")
  const [chosen, setChosen] = useState<PanelCandidate | null>(null)
  const [wiring, setWiring] = useState<WiringDraft>(() => prefill(screens))
  const [name, setName] = useState("")
  // `null` is "nobody chose", which is not the same as an empty template name --
  // the first template the server offers stands in until somebody does.
  const [template, setTemplate] = useState<string | null>(null)

  const detect = useDetect(daemonId)
  const probe = useProbe(daemonId)
  // Fetched when the dialog opens rather than when the last step is reached: the
  // wizard is a walk to the rack and back, and a select that is still loading
  // when somebody arrives at it is a control they have to wait on for no reason.
  // The list is shared with the inspector on the same key, so this is at worst
  // one small GET.
  const templates = useTemplates()

  const create = useMutate<Screen, NewScreen>({
    send: (body) => api.POST("/api/screens", { body }),
    // The screen list, which the canvas behind this dialog draws from. The rack's
    // `config_version` moves too, and nothing on this page shows one.
    invalidates: [screensKey],
  })

  const pinout = pinoutOf(wiring)
  const position = nextPosition(screens)
  const names = templates.data?.map((each) => each.name) ?? []
  const wanted = template ?? names[0] ?? ""

  const toWiring = () => setStep("wiring")

  /**
   * Arrive at the probe step with no verdict in hand.
   *
   * The reset is the point, and it is here rather than on the way *out* because
   * this is the only step that reads a verdict and this is the only way into it.
   * Without it, somebody who lit a panel, said no, changed a pin and came back
   * would find the previous *yes* still sitting in the mutation -- offering "did
   * it light up?" about wiring that has since been changed and never proved, and
   * with no button left to prove it with.
   */
  const toProbe = () => {
    probe.reset()
    setStep("probe")
  }

  const choose = (candidate: PanelCandidate) => {
    setChosen(candidate)
    toWiring()
  }

  const current = STEPS.findIndex((each) => each.step === step)
  const here = STEPS[current]
  const probed = probe.data?.body
  const device = chosen === null ? "" : deviceName(chosen)

  return (
    <>
      <DialogHeader>
        <DialogTitle>{`Add a screen to ${rackName}`}</DialogTitle>
        <DialogDescription>
          {`Step ${current + 1} of ${STEPS.length}: ${here.title}. ${here.blurb}`}
        </DialogDescription>
      </DialogHeader>

      {step === "detect" && (
        <div className="grid gap-3">
          <p className="text-sm text-muted-foreground">
            {"Only the rack knows which SPI devices it has, so this asks it. It changes " +
              "nothing and touches no panel: it is a directory listing on the Pi."}
          </p>
          {detect.isError && <Refusal error={detect.error} />}
          {detect.data?.body !== undefined && (
            <ul
              aria-label={`SPI devices on ${rackName}`}
              className="grid gap-2 rounded-md border p-3"
            >
              {detect.data.body.panels.map((candidate) => (
                <li
                  key={deviceName(candidate)}
                  className="flex items-center justify-between gap-3"
                >
                  {/* `claimed_by === null` and not `!claimed_by`. The name of a
                      screen is never the empty string -- the schema forbids it,
                      because `""` is falsy and a device would then read as free
                      -- and a device a live worker is mid-frame on must not be
                      offered: probing it is a second panel init on a bus that is
                      already in use. It is still *listed*, with the screen that
                      has it, because an operator who counted three devices with
                      a torch and is shown two has been told nothing about the
                      third. */}
                  {candidate.claimed_by === null ? (
                    <>
                      <span className="font-mono text-sm">{deviceName(candidate)}</span>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => choose(candidate)}
                      >
                        {`Use ${deviceName(candidate)}`}
                      </Button>
                    </>
                  ) : (
                    <span className="text-sm text-muted-foreground">
                      {`${deviceName(candidate)} is already driving ${candidate.claimed_by}, ` +
                        "so it cannot be probed"}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <Button
            className="justify-self-start"
            disabled={detect.isPending}
            onClick={() => detect.mutate()}
          >
            Detect panels
          </Button>
        </div>
      )}

      {step === "wiring" && chosen !== null && (
        <div className="grid gap-4">
          <p className="text-sm">
            {`The new panel is on ${device}. Which GPIO lines drive it?`}
          </p>

          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor={field("dc")}>DC pin</Label>
              <Input
                id={field("dc")}
                type="number"
                value={wiring.dc}
                onChange={(event) => setWiring({ ...wiring, dc: event.target.value })}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor={field("rst")}>RST pin</Label>
              <Input
                id={field("rst")}
                type="number"
                value={wiring.rst}
                onChange={(event) => setWiring({ ...wiring, rst: event.target.value })}
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor={field("hz")}>Clock speed</Label>
            <Input
              id={field("hz")}
              type="number"
              value={wiring.hz}
              onChange={(event) => setWiring({ ...wiring, hz: event.target.value })}
            />
          </div>

          <p className="text-xs text-muted-foreground">
            {screens.length === 0
              ? "This rack has no panels to copy a pinout from, so these start empty. There is " +
                "no GPIO 0 here by default: a pin nobody named is not a pin."
              : `Started from ${screens[screens.length - 1].name}, the panel at the ` +
                "right-hand end of this rack. Nothing on the bus reports these, so they are a " +
                "guess until the probe proves them."}
          </p>
          <p className="text-xs text-muted-foreground">
            {"The numbers themselves are not checked here or on the server -- which GPIO lines " +
              "exist is a fact about your board, and the rack is what finds out."}
          </p>

          <div className="flex gap-2">
            <Button disabled={pinout === null} onClick={toProbe}>
              Continue to the probe
            </Button>
            <Button variant="outline" onClick={() => setStep("detect")}>
              Choose another device
            </Button>
          </div>
          {pinout === null && (
            <p className="text-xs text-muted-foreground">
              {"Both pins and a clock speed, as whole numbers. The rack is asked for exactly " +
                "what is in these boxes and defaults none of it."}
            </p>
          )}
        </div>
      )}

      {/* `chosen` and `pinout` are narrowing rather than guards: this step is
          only reached from *Continue to the probe*, which is disabled until both
          are there. TypeScript needs them said; nothing else does. */}
      {step === "probe" && chosen !== null && pinout !== null && (
        <div className="grid gap-4">
          {/* Said **before** the button that does it, and this is the reason the
              step exists as a step. Detection is a directory listing; a probe is
              a real panel init -- roughly 160 ms of resets and register writes,
              then a frame -- and it takes the rack's bus for the whole hold, so
              every other panel sharing that bus stops drawing until it is done.
              Somebody pressing a button labelled only "Probe" has not been told
              any of that. */}
          {probe.isIdle && (
            <Alert>
              <AlertTitle>{`This lights ${device} on ${rackName}`}</AlertTitle>
              <AlertDescription className="grid gap-2">
                <p>
                  {`A real panel init: about 160 ms of resets and register writes, then one ` +
                    `frame, held lit for ${PROBE_HOLD_S} seconds. Go and look at the rack ` +
                    `before you press it.`}
                </p>
                <p>
                  {`The rack's SPI bus is held for the whole of it, so every other panel ` +
                    `sharing that bus stops drawing until it is done, and ${rackName} will ` +
                    `refuse a second probe while this one runs.`}
                </p>
              </AlertDescription>
            </Alert>
          )}

          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">Device</dt>
            <dd className="font-mono">{device}</dd>
            <dt className="text-muted-foreground">DC / RST</dt>
            <dd className="font-mono">{`${pinout.dc} / ${pinout.rst}`}</dd>
            <dt className="text-muted-foreground">Clock</dt>
            <dd className="font-mono">{`${pinout.hz} Hz`}</dd>
          </dl>

          {probe.isPending && (
            <p className="text-sm">
              {`${device} is lit now. The rack holds it for about ${PROBE_HOLD_S} seconds and ` +
                "then lets the bus go; this answers when it does."}
            </p>
          )}

          {probe.isError && <Refusal error={probe.error} />}

          {probe.data !== undefined && probed?.ok === true && (
            <div className="grid gap-3">
              <p className="text-sm font-medium">{`Did the panel on ${device} light up?`}</p>
              <p className="text-sm text-muted-foreground">
                {"The rack says the device opened and took the frame. It cannot say whether " +
                  "anything appeared on the glass, and a panel on the wrong DC line does both " +
                  "of those things and shows nothing."}
              </p>
              <div className="flex gap-2">
                <Button onClick={() => setStep("add")}>Yes, that panel lit up</Button>
                <Button variant="outline" onClick={toWiring}>
                  No, nothing lit up
                </Button>
              </div>
            </div>
          )}

          {probe.data !== undefined && probed?.ok !== true && (
            <Alert variant="destructive">
              <AlertTitle>{`${rackName} could not drive that wiring`}</AlertTitle>
              <AlertDescription>
                <p>
                  {probed?.error ??
                    "The rack refused the wiring and said nothing more about it."}
                </p>
              </AlertDescription>
            </Alert>
          )}

          {!probe.isPending && probed?.ok !== true && (
            <div className="flex gap-2">
              {/* Every field spelled out rather than spread from `chosen`, which
                  also carries `claimed_by`: `ProbeBody` is `extra="forbid"`, so a
                  wizard sending a field it does not have is a 422 rather than a
                  field quietly ignored -- which is the right refusal and not one
                  worth provoking. `hold_s` is `PROBE_HOLD_S` and can be nothing
                  else; see that constant. */}
              <Button
                onClick={() =>
                  probe.mutate({
                    bus: chosen.bus,
                    cs: chosen.cs,
                    dc: pinout.dc,
                    rst: pinout.rst,
                    hz: pinout.hz,
                    hold_s: PROBE_HOLD_S,
                  })
                }
              >
                Light the panel
              </Button>
              <Button variant="outline" onClick={toWiring}>
                Change the wiring
              </Button>
            </div>
          )}
        </div>
      )}

      {step === "add" && chosen !== null && pinout !== null && (
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            const display: DisplayConfig = {
              backend: "gc9a01",
              spi_bus: chosen.bus,
              spi_cs: chosen.cs,
              dc: pinout.dc,
              rst: pinout.rst,
              hz: pinout.hz,
            }
            create.mutate(
              {
                daemon_id: daemonId,
                name: name.trim(),
                position,
                template: wanted,
                display,
              },
              { onSuccess: onAdded },
            )
          }}
        >
          <p className="text-sm">
            {`${device} lit up with DC ${pinout.dc} and RST ${pinout.rst}. That is the wiring ` +
              `it will be created with.`}
          </p>

          <div className="grid gap-2">
            <Label htmlFor={field("name")}>Name</Label>
            <Input
              id={field("name")}
              value={name}
              autoComplete="off"
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor={field("template")}>Template</Label>
            <Select value={wanted} onValueChange={setTemplate}>
              <SelectTrigger id={field("template")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {names.map((each) => (
                  <SelectItem key={each} value={each}>
                    {each}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <p className="text-xs text-muted-foreground">
            {`It goes in at position ${position}, at the right-hand end of ${rackName}. The ` +
              "arrows under the rack move it, and they renumber the whole rack at once."}
          </p>

          {create.isError && <Refusal error={create.error} />}

          <div className="flex gap-2">
            <Button type="submit" disabled={create.isPending || name.trim() === "" || wanted === ""}>
              Add the screen
            </Button>
            <Button type="button" variant="outline" onClick={toWiring}>
              Change the wiring
            </Button>
          </div>
        </form>
      )}
    </>
  )
}

/**
 * The button that starts it, and the dialog it opens.
 *
 * Split from `Wizard` so that closing the dialog is the whole of forgetting it:
 * Radix unmounts the content, which takes every piece of step state and all
 * three mutations with it, so the next person to open it starts at "detect" with
 * nothing remembered about a device that may since have been claimed. It is also
 * what keeps `useTemplates` off a page that is not showing this dialog.
 */
export function AddScreenWizard({
  daemonId,
  rackName,
  screens,
}: {
  daemonId: number
  rackName: string
  screens: Screen[]
}) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline">{`Add a screen to ${rackName}`}</Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <Wizard
          daemonId={daemonId}
          rackName={rackName}
          screens={screens}
          onAdded={() => setOpen(false)}
        />
      </DialogContent>
    </Dialog>
  )
}
