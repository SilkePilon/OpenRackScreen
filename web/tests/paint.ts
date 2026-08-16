import { vi } from "vitest"

/**
 * The two things jsdom does not paint with, and what stands in for them.
 *
 * jsdom implements neither `createImageBitmap` nor a 2D context: `getContext`
 * answers `null` and logs "without installing the canvas npm package". Both are
 * load-bearing for a panel, so neither can be left out and neither may be a
 * double that quietly swallows what it is handed.
 *
 * What is replaced here is the *environment* -- the browser's decoder and its
 * canvas -- and nothing that this milestone wrote. Both doubles are recorders:
 * every call is kept in order with its arguments, so a test asserts what was
 * drawn rather than that something was. That is what stops them hiding a
 * defect. A double that returned `undefined` from every method would let
 * "blanks the canvas when the socket drops" pass while broken, because the
 * `clearRect` would leave no trace; this one records `clearRect` by name, so a
 * panel that blanks itself is a call in a log a test can see.
 *
 * The recorder deliberately implements more of the 2D context than a panel
 * uses -- `rotate`, `scale`, `setTransform`, `clearRect`, `fillRect`. Those are
 * the calls a *mutant* makes. A double missing them would kill every such
 * mutant with a `TypeError` instead of with the assertion that was supposed to
 * catch it, which is a false kill and tells you nothing about the test.
 */

/** One call on the 2D context, in the order it was made. */
export type Painted = { op: string; args: readonly unknown[] }

/**
 * A 2D context that draws nothing and remembers everything.
 *
 * Not a `Proxy`: the methods are spelled out so that a call to something no
 * browser has is a `TypeError` here too, rather than a silently recorded
 * fiction.
 */
export class RecordingContext {
  readonly calls: Painted[] = []

  /** The call names in order -- the shape most assertions want. */
  get ops(): string[] {
    return this.calls.map((call) => call.op)
  }

  /** Every `drawImage`, with its arguments. */
  get drawn(): Painted[] {
    return this.calls.filter((call) => call.op === "drawImage")
  }

  private record(op: string, ...args: unknown[]): void {
    this.calls.push({ op, args })
  }

  save(): void {
    this.record("save")
  }

  restore(): void {
    this.record("restore")
  }

  beginPath(): void {
    this.record("beginPath")
  }

  arc(x: number, y: number, radius: number, start: number, end: number): void {
    this.record("arc", x, y, radius, start, end)
  }

  clip(): void {
    this.record("clip")
  }

  drawImage(...args: unknown[]): void {
    this.record("drawImage", ...args)
  }

  // Below here: nothing a panel calls, and everything a mutant would.
  clearRect(x: number, y: number, width: number, height: number): void {
    this.record("clearRect", x, y, width, height)
  }

  fillRect(x: number, y: number, width: number, height: number): void {
    this.record("fillRect", x, y, width, height)
  }

  translate(x: number, y: number): void {
    this.record("translate", x, y)
  }

  rotate(angle: number): void {
    this.record("rotate", angle)
  }

  scale(x: number, y: number): void {
    this.record("scale", x, y)
  }

  transform(...args: number[]): void {
    this.record("transform", ...args)
  }

  setTransform(...args: unknown[]): void {
    this.record("setTransform", ...args)
  }

  resetTransform(): void {
    this.record("resetTransform")
  }
}

export type CanvasRecorder = {
  /** Every canvas that asked for a 2D context, in the order they asked. */
  readonly contexts: RecordingContext[]
  /** The context of the one canvas this test rendered. Throws if there is not exactly one. */
  only(): RecordingContext
}

/**
 * Give every canvas in this test a recording 2D context.
 *
 * A spy on the prototype rather than an assignment, so `vi.restoreAllMocks()`
 * puts jsdom's own `getContext` back and no other test file inherits this. The
 * same canvas asked twice gets the same context, as a browser does -- a panel
 * that called `getContext` per frame would otherwise record into a fresh log
 * every time and every assertion about what is on screen would read empty.
 */
export function recordCanvas(): CanvasRecorder {
  const byCanvas = new WeakMap<HTMLCanvasElement, RecordingContext>()
  const contexts: RecordingContext[] = []

  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (
    this: HTMLCanvasElement,
    kind: string,
  ) {
    if (kind !== "2d") return null
    const existing = byCanvas.get(this)
    if (existing !== undefined) return existing as unknown as CanvasRenderingContext2D
    const context = new RecordingContext()
    byCanvas.set(this, context)
    contexts.push(context)
    return context as unknown as CanvasRenderingContext2D
  } as unknown as HTMLCanvasElement["getContext"])

  return {
    contexts,
    only() {
      if (contexts.length !== 1) {
        throw new Error(`expected exactly one canvas, found ${contexts.length}`)
      }
      return contexts[0]
    },
  }
}

/**
 * What `createImageBitmap` resolves to here.
 *
 * A real `ImageBitmap` offers no way to ask whether it has been closed --
 * `close()` is one-way and the only observable consequence is that a later
 * `drawImage` throws. Since "every bitmap this store made was closed exactly
 * once" is the property that keeps a tab from growing, the double is what makes
 * it assertable at all.
 */
export type FakeBitmap = {
  /** 1, 2, 3 ... in the order they were decoded, so a test can name one. */
  readonly serial: number
  closed: boolean
  /** How many times `close()` was called: a second close is a defect, not a no-op. */
  closes: number
  readonly width: number
  readonly height: number
  close(): void
}

export type BitmapDecoder = {
  /** Every blob handed to `createImageBitmap`, in order. */
  readonly blobs: Blob[]
  /** Every bitmap it produced, in the order the decodes were *asked for*. */
  readonly made: FakeBitmap[]
  /** How many decodes have been asked for and not yet answered. */
  pending(): number
  /** Answer the decode at `index` (default: the oldest outstanding one). */
  settle(index?: number): Promise<void>
  /** Answer every outstanding decode, oldest first. */
  settleAll(): Promise<void>
  /** Refuse the decode at `index`, as a browser does for a payload it cannot read. */
  reject(index: number, reason: string): Promise<void>
}

/**
 * Stand in for the browser's decoder, and hand the test the clock on it.
 *
 * Decodes are answered on demand rather than immediately, because two of the
 * rules under test are about *when* a decode lands: a frame whose panel went
 * away while it was decoding, and two decodes that finish in the wrong order.
 * Neither is reachable against a decoder that resolves before the test can look.
 */
export function stubDecoder(): BitmapDecoder {
  const blobs: Blob[] = []
  const made: FakeBitmap[] = []
  const outstanding = new Map<number, { settle: (bitmap: FakeBitmap) => void; fail: (reason: Error) => void }>()

  vi.stubGlobal("createImageBitmap", (blob: Blob) => {
    const index = made.length
    const bitmap: FakeBitmap = {
      serial: index + 1,
      closed: false,
      closes: 0,
      width: 240,
      height: 240,
      close() {
        this.closed = true
        this.closes += 1
      },
    }
    blobs.push(blob)
    made.push(bitmap)
    return new Promise<FakeBitmap>((resolve, reject) => {
      outstanding.set(index, { settle: resolve, fail: reject })
    })
  })

  // Three turns of the microtask queue: the store's own `.then`, whatever it
  // calls from there, and one spare. Nothing here advances a timer, so a test
  // under fake timers settles a decode without moving its clock.
  const flush = async (): Promise<void> => {
    for (let turn = 0; turn < 3; turn += 1) await Promise.resolve()
  }

  const oldest = (): number => {
    const first = [...outstanding.keys()][0]
    if (first === undefined) throw new Error("no decode is outstanding")
    return first
  }

  const take = (index: number) => {
    const answer = outstanding.get(index)
    if (answer === undefined) throw new Error(`decode ${index} is not outstanding`)
    outstanding.delete(index)
    return answer
  }

  return {
    blobs,
    made,
    pending: () => outstanding.size,
    async settle(index) {
      const at = index ?? oldest()
      take(at).settle(made[at])
      await flush()
    },
    async settleAll() {
      for (const index of [...outstanding.keys()]) take(index).settle(made[index])
      await flush()
    },
    async reject(index, reason) {
      take(index).fail(new Error(reason))
      await flush()
    },
  }
}
