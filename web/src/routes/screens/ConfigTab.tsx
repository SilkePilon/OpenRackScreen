import { useId, useState } from "react"

import { useTemplates, type Screen } from "@/api/queries"
import type { components } from "@/api/schema"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"

type ScreenBody = components["schemas"]["ScreenBody"]
type DisplayConfig = components["schemas"]["DisplayConfig"]
type Rotation = NonNullable<ScreenBody["rotation"]>
type Backend = DisplayConfig["backend"]

const ROTATIONS: Rotation[] = [0, 90, 180, 270]

/**
 * A rotation from whatever the row holds.
 *
 * The generated `ScreenView.rotation` is `number`, not the four-way literal:
 * the column is an integer and the *body* is what the server constrains, so a
 * database somebody hand-edited or restored can hold 45 and this page still has
 * to draw a control. The fallback picks the value that describes a panel bolted
 * in the way it was made, which is also what `create_screen` writes when no
 * rotation is given.
 */
function rotationFrom(text: string): Rotation {
  return ROTATIONS.find((each) => String(each) === text) ?? 0
}

/**
 * The wiring, as text, because an empty box is a state the model has.
 *
 * `dc` and `rst` are `int | None` with no default -- a GC9A01 is wired to
 * whichever header pins were free -- so "not set" has to survive a round trip
 * through a form, and a number kept as a number cannot say it.
 */
type Wiring = {
  backend: Backend
  spi_bus: string
  spi_cs: string
  dc: string
  rst: string
  hz: string
  out_dir: string
}

/**
 * Read the wiring out of the row.
 *
 * `ScreenView.display` is `dict[str, Any]` -- the generated type is an index
 * signature of `unknown` -- because the server answers the column as it parsed
 * it, and a column it could *not* parse it answers as `{}` with a warning
 * rather than failing the whole listing. So every field is narrowed here, and a
 * screen whose display column is unreadable gets an empty form rather than a
 * crash on a page that is also showing the rack's `config_error`.
 */
function readWiring(display: Screen["display"]): Wiring {
  const digits = (value: unknown) => (typeof value === "number" ? String(value) : "")
  const text = (value: unknown) => (typeof value === "string" ? value : "")
  return {
    backend: display.backend === "virtual" ? "virtual" : "gc9a01",
    spi_bus: digits(display.spi_bus),
    spi_cs: digits(display.spi_cs),
    dc: digits(display.dc),
    rst: digits(display.rst),
    hz: digits(display.hz),
    out_dir: text(display.out_dir),
  }
}

/**
 * The position the box asks for, or `null` because it asks for none.
 *
 * `Number("")` is `0` and `Number.isFinite(0)` is true, which is the third time
 * this project has been bitten by that pair: an emptied box read as a number is
 * a request to move the panel to position 0, which `ScreenBody.position`
 * (`ge=1`) refuses. An empty box is not an edit -- exactly as an empty pin box
 * is "not set" rather than GPIO 0 -- and neither is a box holding something a
 * `type="number"` input normalises to nothing (`e`, `-`, `1.5e`).
 *
 * Integers only, for the same reason: `Number("2.5")` is finite and is not a
 * position, and the server would answer 422 for it.
 */
function positionOf(text: string): number | null {
  if (text.trim() === "") return null
  const value = Number(text)
  return Number.isInteger(value) ? value : null
}

/** An empty box means the schema's own default, and never zero by accident. */
function digitsOr(text: string, fallback: number): number {
  const value = Number(text)
  return text === "" || !Number.isFinite(value) ? fallback : value
}

/** An empty box means "not set", which for a pin is a different thing from 0. */
function pin(text: string): number | null {
  const value = Number(text)
  return text === "" || !Number.isFinite(value) ? null : value
}

function toDisplay(wiring: Wiring): DisplayConfig {
  return {
    backend: wiring.backend,
    spi_bus: digitsOr(wiring.spi_bus, 0),
    spi_cs: digitsOr(wiring.spi_cs, 0),
    dc: pin(wiring.dc),
    rst: pin(wiring.rst),
    hz: digitsOr(wiring.hz, 40_000_000),
    out_dir: wiring.out_dir === "" ? null : wiring.out_dir,
  }
}

function sameWiring(left: Wiring, right: Wiring): boolean {
  return (Object.keys(left) as (keyof Wiring)[]).every((key) => left[key] === right[key])
}

/**
 * What a panel is: its name, where it sits, what it draws, and how it is wired.
 *
 * **`rotation` and `hflip` are edited here and applied nowhere in this
 * interface.** They describe how the panel is bolted into the rack -- all four
 * of the user's are 270 -- and the daemon transposes for the glass *after* it
 * has streamed the frame, so the picture on the canvas is already what a person
 * standing at the rack sees. This form writes the numbers; nothing reads them
 * back to draw with.
 *
 * **The pin numbers are not validated here, on purpose.** Which buses, chip
 * selects and GPIO lines exist is a fact about the board, and `DisplayConfig`
 * says so: no range check, no `dc != rst` rule. The two rules it does enforce
 * -- a virtual backend needs `out_dir`, a gc9a01 needs both `dc` and `rst` --
 * are stated below the fields so a refusal reads as an answer to something,
 * and are left to the server to apply, because the server is what the rack
 * agrees with.
 */
export function ConfigTab({
  screen,
  save,
  saving,
  edited,
}: {
  screen: Screen
  save: (body: ScreenBody) => void
  saving: boolean
  /** Called before every change to this form; see `Inspector`'s `edited`. */
  edited: () => void
}) {
  const fieldId = useId()
  const field = (part: string) => `${fieldId}-${part}`
  const templates = useTemplates()

  const [name, setName] = useState(screen.name)
  const [position, setPosition] = useState(String(screen.position))
  const [template, setTemplate] = useState(screen.template)
  const [rotation, setRotation] = useState(() => rotationFrom(String(screen.rotation)))
  const [hflip, setHflip] = useState(screen.hflip)
  const [enabled, setEnabled] = useState(screen.enabled)
  const [wiring, setWiring] = useState(() => readWiring(screen.display))

  /** A setter that first says the form has moved on from what was last saved. */
  function changing<T>(set: (value: T) => void): (value: T) => void {
    return (value: T) => {
      edited()
      set(value)
    }
  }

  /** One field of the wiring, the rest of it as it was. */
  const wire = <K extends keyof Wiring>(part: K, value: Wiring[K]) =>
    changing(setWiring)({ ...wiring, [part]: value })

  // What the server last said, which is what "changed" is measured against.
  // Recomputed rather than remembered from the first render: after a save the
  // list is refetched, the row arrives with the new values, and the fields that
  // were just written stop counting as changes.
  const saved = readWiring(screen.display)

  /**
   * Exactly the fields that differ, and never one more.
   *
   * `ScreenBody` is `extra="forbid"` and the route's PATCH semantics come from
   * `exclude_unset`, so this is not a preference: a body carrying `id` is a
   * 422, and a body repeating every field the form holds silently overwrites
   * whatever another tab changed in the meantime. `display` goes whole when any
   * part of it moved, because it is one column and one model, and the server
   * validates it as one document.
   */
  function changes(): ScreenBody {
    const body: ScreenBody = {}
    if (name !== screen.name) body.name = name
    // An unreadable or empty box asks for nothing rather than for position 0.
    const wanted = positionOf(position)
    if (wanted !== null && wanted !== screen.position) body.position = wanted
    if (template !== screen.template) body.template = template
    if (rotation !== screen.rotation) body.rotation = rotation
    if (hflip !== screen.hflip) body.hflip = hflip
    if (enabled !== screen.enabled) body.enabled = enabled
    if (!sameWiring(wiring, saved)) body.display = toDisplay(wiring)
    return body
  }

  const body = changes()
  const nothingToSave = Object.keys(body).length === 0

  // The template this screen names may not be in the list: a template deleted
  // from another tab is a screen the server then refuses to build a snapshot
  // for, and a select with no matching option would draw that as blank -- which
  // reads as "no template" rather than as "the one it names is gone".
  const names = templates.data?.map((each) => each.name) ?? []
  const options = names.includes(template) ? names : [template, ...names]

  return (
    <div className="grid gap-4 pt-4">
      <div className="grid gap-2">
        <Label htmlFor={field("name")}>Name</Label>
        <Input
          id={field("name")}
          value={name}
          onChange={(event) => changing(setName)(event.target.value)}
        />
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("position")}>Position</Label>
        <Input
          id={field("position")}
          type="number"
          min={1}
          value={position}
          onChange={(event) => changing(setPosition)(event.target.value)}
        />
        <p className="text-xs text-muted-foreground">
          {positionOf(position) === null
            ? `${
                position.trim() === "" ? "Empty" : "Not a whole number"
              }, so this panel keeps the position it has. The arrows under the rack renumber the ` +
              "whole rack at once, which is the safer way to move one."
            : "Where this panel sits from the left. The arrows under the rack renumber the whole " +
              "rack at once, which is the safer way to move one."}
        </p>
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("template")}>Template</Label>
        <Select value={template} onValueChange={changing(setTemplate)}>
          <SelectTrigger id={field("template")}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((each) => (
              <SelectItem key={each} value={each}>
                {each}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-2">
        <Label htmlFor={field("rotation")}>Rotation</Label>
        <Select
          value={String(rotation)}
          onValueChange={(next) => changing(setRotation)(rotationFrom(next))}
        >
          <SelectTrigger id={field("rotation")}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ROTATIONS.map((each) => (
              <SelectItem key={each} value={String(each)}>
                {`${each}°`}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">
          How the panel is bolted into the rack. The rack applies it; the picture above already
          shows what you would see standing in front of it, so nothing here turns.
        </p>
      </div>

      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("hflip")}>Horizontal flip</Label>
        <Switch id={field("hflip")} checked={hflip} onCheckedChange={changing(setHflip)} />
      </div>

      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={field("enabled")}>Enabled</Label>
        <Switch id={field("enabled")} checked={enabled} onCheckedChange={changing(setEnabled)} />
      </div>

      <div className="grid gap-3 rounded-md border p-3">
        <p className="text-sm font-medium">Wiring</p>

        <div className="grid gap-2">
          <Label htmlFor={field("backend")}>Backend</Label>
          <Select
            value={wiring.backend}
            onValueChange={(next) => wire("backend", next === "virtual" ? "virtual" : "gc9a01")}
          >
            <SelectTrigger id={field("backend")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="gc9a01">gc9a01</SelectItem>
              <SelectItem value="virtual">virtual</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="grid gap-2">
            <Label htmlFor={field("bus")}>SPI bus</Label>
            <Input
              id={field("bus")}
              type="number"
              value={wiring.spi_bus}
              onChange={(event) => wire("spi_bus", event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={field("cs")}>SPI chip select</Label>
            <Input
              id={field("cs")}
              type="number"
              value={wiring.spi_cs}
              onChange={(event) => wire("spi_cs", event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={field("dc")}>DC pin</Label>
            <Input
              id={field("dc")}
              type="number"
              value={wiring.dc}
              onChange={(event) => wire("dc", event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={field("rst")}>RST pin</Label>
            <Input
              id={field("rst")}
              type="number"
              value={wiring.rst}
              onChange={(event) => wire("rst", event.target.value)}
            />
          </div>
        </div>

        <div className="grid gap-2">
          <Label htmlFor={field("hz")}>Clock speed</Label>
          <Input
            id={field("hz")}
            type="number"
            value={wiring.hz}
            onChange={(event) => wire("hz", event.target.value)}
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor={field("out")}>Output directory</Label>
          <Input
            id={field("out")}
            value={wiring.out_dir}
            onChange={(event) => wire("out_dir", event.target.value)}
          />
        </div>

        <p className="text-xs text-muted-foreground">
          A gc9a01 needs both DC and RST; a virtual panel needs an output directory. The pin
          numbers themselves are not checked here or on the server &mdash; which lines exist is a
          fact about your board, and the rack is what finds out.
        </p>
      </div>

      <Button className="justify-self-start" disabled={saving || nothingToSave} onClick={() => save(body)}>
        Save changes
      </Button>
    </div>
  )
}
