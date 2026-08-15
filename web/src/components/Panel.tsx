import { useEffect, useRef, useState } from "react"

import { useCachedDaemons } from "@/api/queries"
import { frameStore } from "@/live/frames"
import { cn } from "@/lib/utils"

/**
 * One round panel, drawing what its screen is drawing.
 *
 * It knows about a canvas, a frame store and the racks in the cache, and about
 * no connection at all. Whether there is a socket, whether it is up, and what
 * it is subscribed to are `LiveProvider`'s; this component would draw the same
 * from a recording.
 *
 * **Nothing here rotates or flips the image.** `rotation` and `hflip` describe
 * how a panel is bolted into the rack -- all four of the user's are `270` --
 * and the daemon streams the frame *before* it applies them, so what arrives is
 * already what a person standing at the rack sees. That is why this component
 * is not even given the screen's rotation: rotating again would be wrong twice,
 * and a value that is not in scope cannot be applied by accident.
 */

/**
 * How often a frame is due, in milliseconds.
 *
 * The server never names a rate: `hub.py` builds its `FramesRequest` with
 * `enabled` and `screen_ids` and nothing else, so what each daemon is asked for
 * is `FramesRequest.fps`'s default of 2.0, and a watched panel produces a frame
 * every 500 ms. Not a guess and not a preference: it is the rate the other end
 * was asked for.
 *
 * Two ceilings sit above that default and neither is this number. The schema
 * bounds the field at `MAX_REQUESTED_FPS = 60.0`, and the daemon caps whatever
 * it is handed at its own `MAX_FPS = 5.0`. Both are bounds on a rate nothing in
 * this interface requests, and if the server ever starts asking for one, this
 * constant is where it has to be said again.
 */
export const FRAME_INTERVAL_MS = 500

/**
 * How long a panel may be quiet before it says so.
 *
 * **This number is a choice, made here, and the spec names none.** It is
 * derived rather than picked:
 *
 * - A frame is due every `FRAME_INTERVAL_MS`. One missed frame is ordinary -- a
 *   WebP encode that overran, a garbage collection, a wifi retransmit -- and a
 *   panel that said "stale" for one of those would flicker on a healthy rack,
 *   which teaches the person watching to ignore the word.
 * - A subscription can be held back by the server's own coalescing for up to
 *   `ARM_INTERVAL_S = 0.25` s before frames begin, so a panel that has just
 *   mounted may legitimately wait 750 ms for its first image.
 * - Four frame intervals is two full seconds during which four frames were due
 *   and none came. That is past every hiccup above and short of the point where
 *   somebody has decided for themselves that the picture is frozen.
 *
 * The cost of it being wrong is small in one direction and not the other, which
 * is why it errs long: too short cries wolf on a working rack, too long delays
 * a word about a rack that is *still on screen and still showing its last
 * frame*. Nothing is hidden by waiting -- the picture is there either way.
 */
export const STALE_AFTER_MS = 4 * FRAME_INTERVAL_MS

/** Live, quiet for too long, or on a rack the server says has gone. */
type PanelState = "live" | "stale" | "offline"

/**
 * Put a bitmap on the glass: clipped to a circle, at the size it is drawn at.
 *
 * The dimensions come off the canvas rather than from a prop, so the draw path
 * has no dependency on a React value and a resized panel needs no
 * re-subscription -- which would otherwise drop this screen's last subscriber
 * for an instant and take the picture with it. What a resize *does* need is a
 * repaint, because the browser empties the surface when the attribute is
 * assigned; that is the second effect below.
 */
function paint(context: CanvasRenderingContext2D, canvas: HTMLCanvasElement, bitmap: ImageBitmap) {
  const { width, height } = canvas
  context.save()
  context.beginPath()
  context.arc(width / 2, height / 2, Math.min(width, height) / 2, 0, Math.PI * 2)
  context.clip()
  // No clear before it: the image is opaque and covers the clip, and a panel
  // that cleared would show a flash of nothing between two frames. Nothing in
  // this component clears the canvas at all -- there is no event, the socket
  // dropping least of all, that should leave a rack looking dark.
  context.drawImage(bitmap, 0, 0, width, height)
  context.restore()
}

export function Panel({
  screenId,
  daemonId,
  size,
}: {
  screenId: number
  /** The rack this screen is on. The only thing that can say it has gone. */
  daemonId: number
  /** Both the drawn size and the canvas's own, in CSS pixels. */
  size: number
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [stale, setStale] = useState(false)
  // A ref beside the state, so a frame arriving twice a second on a panel that
  // is already live sets nothing and renders nothing. `useState` bails out on
  // an unchanged value, but it may render once before it does, and once per
  // frame is the cost this whole design exists to avoid.
  //
  // Kept knowing that no test can see it, which the mutation sweep proved: with
  // this line removed, `setStale(false)` runs on every frame and the panel's
  // own render counter does not move. React 19 computes the next state eagerly
  // when a fiber has no other pending update and returns without scheduling if
  // it is `Object.is`-equal, so the extra dispatch is free *there*. That is a
  // heuristic React documents as "may still need to render one more time", not
  // a guarantee, and this line is what makes the property true rather than
  // likely.
  const staleRef = useRef(false)
  const staleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const racks = useCachedDaemons()
  // Offline is what `daemons` says and nothing else -- never inferred from
  // frames stopping, which is a different fact with a different cause. A rack
  // missing from a list this interface *has* is gone; a list it has never
  // fetched says nothing, and `undefined` must not be read as absence.
  const offline = racks !== undefined && !racks.some((rack) => rack.id === daemonId && rack.online)

  useEffect(() => {
    const canvas = canvasRef.current
    // Narrowing, not a guard: React commits the DOM before it runs this effect,
    // so the canvas is here.
    if (canvas === null) return
    // Asked once per mount rather than per frame. `null` is a browser that will
    // not give a 2D context at all; the panel then has nothing to draw on and
    // still has everything else to say.
    const context = canvas.getContext("2d")

    const setStaleness = (next: boolean) => {
      if (staleRef.current === next) return
      staleRef.current = next
      setStale(next)
    }

    /**
     * Say when this panel will have been quiet for too long, and wait for it.
     *
     * A timer armed for exactly that moment rather than a clock polled twice a
     * second: it costs nothing while a stream is healthy, and it changes state
     * at the boundary instead of up to one tick after it. Re-armed on every
     * frame, and on mount from `lastAt` -- so a panel joining a stream that is
     * already running inherits how long it has been quiet, and one that has
     * never seen a frame gets a full interval's grace before it complains.
     */
    const arm = () => {
      if (staleTimer.current !== null) clearTimeout(staleTimer.current)
      const last = frameStore.lastAt(screenId)
      const quietFor = last === null ? 0 : performance.now() - last
      const remaining = STALE_AFTER_MS - quietFor
      if (remaining <= 0) {
        staleTimer.current = null
        setStaleness(true)
        return
      }
      setStaleness(false)
      staleTimer.current = setTimeout(() => {
        staleTimer.current = null
        setStaleness(true)
      }, remaining)
    }

    const release = frameStore.subscribe(screenId, (bitmap) => {
      if (context !== null) paint(context, canvas, bitmap)
      arm()
    })
    arm()

    return () => {
      release()
      if (staleTimer.current !== null) {
        clearTimeout(staleTimer.current)
        staleTimer.current = null
      }
    }
  }, [screenId])

  /**
   * Put the picture back after a resize, without waiting for the next frame.
   *
   * `width` and `height` on a canvas are the drawing surface, not a style, and
   * assigning either -- which React does the moment `size` changes -- resets it
   * to transparent black even when the value is the same. At 2 fps the next
   * frame is up to 500 ms away, so a panel that only drew on arrival would go
   * black for half a second every time the Screens page changed its layout.
   *
   * A second effect rather than `size` in the subscription's dependencies: a
   * re-subscribe would drop this screen's last subscriber for an instant, and
   * the store forgets a screen with its last panel -- closing the bitmap the
   * resize is trying to keep. So the subscription is left alone and the picture
   * is asked for instead.
   *
   * It runs on mount too, where it draws whatever the replay in `subscribe`
   * just drew, one redundant `drawImage` on a screen that already had a
   * picture. Cheap, and cheaper than a ref remembering which run this is.
   */
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas === null) return
    const context = canvas.getContext("2d")
    if (context === null) return
    const bitmap = frameStore.current(screenId)
    if (bitmap !== null) paint(context, canvas, bitmap)
  }, [screenId, size])

  const state: PanelState = offline ? "offline" : stale ? "stale" : "live"

  return (
    <div
      data-testid={`panel-${screenId}`}
      data-state={state}
      className="relative shrink-0"
      style={{ width: size, height: size }}
    >
      <canvas
        ref={canvasRef}
        width={size}
        height={size}
        className={cn(
          "size-full rounded-full bg-black transition-opacity",
          state !== "live" && "opacity-60",
        )}
      />
      {state !== "live" && (
        <span
          className={cn(
            "absolute inset-x-0 bottom-2 mx-auto w-fit rounded-full px-2 py-0.5 text-xs font-medium",
            state === "offline" ? "bg-destructive text-white" : "bg-muted text-muted-foreground",
          )}
        >
          {state === "offline" ? "Offline" : "Stale"}
        </span>
      )}
    </div>
  )
}
