/**
 * The frames, outside React, because eight renders a second is not a rendering.
 *
 * Four panels at 2 fps is eight frames a second. Through React state that is
 * eight renders a second of whatever page owns those panels, forever, for as
 * long as a tab is open -- so the images do not go through React at all. A
 * panel subscribes by screen id, receives an `ImageBitmap`, and draws it to its
 * own canvas through a ref. Nothing above a panel re-renders when a frame
 * arrives, and nothing in this file imports React or could.
 *
 * Two boundaries meet here and neither is crossed. The socket hands this module
 * whole frames and knows nothing about what they mean; a panel takes bitmaps
 * from it and knows nothing about a connection. The one thing the socket does
 * need back -- which screens have a panel on them, so it can subscribe to those
 * and only those -- is `onWatchedChange`, and `LiveProvider` is the only place
 * the two sides are wired together.
 */

import type { LiveFrame } from "./socket"

/** What a panel is handed, and what it must not close. See `push`. */
export type DrawFrame = (bitmap: ImageBitmap) => void

export type FrameStore = {
  /**
   * Draw this screen's frames until the returned function is called.
   *
   * If a frame is already on screen for that id the callback is invoked at
   * once, synchronously, with the bitmap being shown -- an inspector opening
   * beside a canvas that has been streaming for a minute draws immediately
   * instead of waiting out a frame interval, and on a rack that has just gone
   * quiet, instead of waiting forever.
   */
  subscribe(screenId: number, draw: DrawFrame): () => void
  /** One frame off the wire. Decoded, then handed to that screen's panels. */
  push(frame: LiveFrame): void
  /**
   * When that screen's last frame was drawn, or `null` if none has been.
   *
   * `performance.now()`, so it is a monotonic elapsed time and not a wall clock
   * a laptop waking up or an NTP step can move. Forgotten with the screen when
   * its last panel goes, so a panel remounting an hour later begins its own
   * grace period rather than being born stale.
   */
  lastAt(screenId: number): number | null
  /**
   * Be told which screens have a panel on them.
   *
   * Called with `true` when a screen gains its first subscriber and `false`
   * when it loses its last -- not once per panel, because two panels on one
   * screen are one subscription on the wire, and the server pays a whole-rack
   * recomputation for a repeat it then treats as a no-op.
   *
   * The listener is replayed with every screen already being watched at the
   * moment it registers. That is not a convenience: React mounts a child's
   * effects before its parent's, so the provider that owns the socket registers
   * *after* the panels beneath it have subscribed, and without the replay every
   * panel on a first page load would be watched by nobody.
   */
  onWatchedChange(listener: (screenId: number, watched: boolean) => void): () => void
}

/**
 * A store. One per tab in production -- `frameStore` below -- and one per test.
 *
 * **Who closes a bitmap.** The store does, and a panel never does. The spec
 * says each bitmap is closed after drawing, which is right for one subscriber
 * and wrong for two: the Screens page shows the same screen on the rack canvas
 * and in the inspector at once, and a panel that closed what it drew would hand
 * the next panel a bitmap that no longer exists. So ownership stays here, and
 * the rule is stated as one sentence with no exceptions: **every bitmap this
 * store decodes is closed exactly once, by this store, at the moment it stops
 * being the one on screen.** That moment is one of three -- it was replaced by
 * a newer frame, it lost a decode race to a newer frame, or the last panel
 * watching that screen went away. The spec's intent is kept (nothing is held
 * beyond its use) and its letter is not, because its letter leaks a closed
 * bitmap into a second panel's `drawImage`.
 */
export function createFrameStore(): FrameStore {
  /** Who draws each screen. A screen with no panels has no entry at all. */
  const panels = new Map<number, Set<DrawFrame>>()
  /** The bitmap on screen for each id: the one thing that must not be closed. */
  const showing = new Map<number, ImageBitmap>()
  /** When each screen's last frame was drawn. */
  const drawnAt = new Map<number, number>()
  /**
   * The `seq` of the most recently *pushed* frame for each screen.
   *
   * Not a filter, and this is the rule the spec's failure table states: a `seq`
   * lower than the last one means the daemon restarted, and the panel resets
   * rather than treating old frames as out of order. Nothing is ever dropped
   * for its `seq` -- dropping would freeze a panel on a picture from before the
   * reboot until somebody reloaded the page.
   *
   * What it is for is the other end of the same fact. `createImageBitmap`
   * decodes off the main thread, so two frames in flight can finish in either
   * order, and the one to draw is the one pushed last. Comparing against what
   * was pushed last -- rather than against the larger `seq` -- is what makes
   * that true through a restart as well: after the counter resets, "newest" is
   * still the frame that arrived most recently, and a `seq` that went backwards
   * is the newest there is.
   */
  const newest = new Map<number, number>()
  const watchers = new Set<(screenId: number, watched: boolean) => void>()

  /** Stop showing whatever that screen was showing, and let go of it. */
  function releaseShown(screenId: number): void {
    showing.get(screenId)?.close()
    showing.delete(screenId)
  }

  return {
    subscribe(screenId, draw) {
      let drawers = panels.get(screenId)
      if (drawers === undefined) {
        drawers = new Set()
        panels.set(screenId, drawers)
      }
      const first = drawers.size === 0
      drawers.add(draw)
      if (first) for (const watcher of watchers) watcher(screenId, true)

      const already = showing.get(screenId)
      if (already !== undefined) draw(already)

      return () => {
        // Idempotent: a second release changes nothing, and neither does one
        // that arrives after the screen has already been forgotten.
        if (!drawers.delete(draw)) return
        if (drawers.size > 0) return
        // The last panel on this screen is gone. Everything about it goes with
        // it -- the bitmap, when its last frame was, where its stream had got
        // to -- because the socket unsubscribes too and there is no stream left
        // for any of it to describe. It is also what keeps the store's memory a
        // function of what is on screen rather than of how long the tab has
        // been open.
        panels.delete(screenId)
        releaseShown(screenId)
        drawnAt.delete(screenId)
        newest.delete(screenId)
        for (const watcher of watchers) watcher(screenId, false)
      }
    },

    push({ screenId, seq, bytes }) {
      const drawers = panels.get(screenId)
      // A frame for a screen nobody is watching, which is the window between a
      // panel unmounting and the unsubscribe reaching the Pi. Dropped before it
      // costs a decode.
      if (drawers === undefined || drawers.size === 0) return
      newest.set(screenId, seq)

      // `image/webp` because that is what the daemon encodes and what the
      // browser is being asked to decode; a blob with no type decodes by
      // sniffing, which is a slower path for no reason. The alternatives were
      // considered and rejected in the spec: an object URL needs disciplined
      // revocation or it leaks, and a data URL allocates a fresh base64 string
      // eight times a second forever.
      createImageBitmap(new Blob([bytes], { type: "image/webp" }))
        .then((bitmap) => {
          // Two ways this decode is no longer wanted, and both end the same:
          // close it here, because a bitmap nobody draws is still memory.
          if (newest.get(screenId) !== seq) return bitmap.close()
          const stillWatching = panels.get(screenId)
          if (stillWatching === undefined || stillWatching.size === 0) return bitmap.close()

          releaseShown(screenId)
          showing.set(screenId, bitmap)
          // Timed at the draw rather than at the push: staleness is about what
          // a person is looking at, and until it is decoded there is nothing to
          // look at.
          drawnAt.set(screenId, performance.now())
          for (const draw of stillWatching) draw(bitmap)
        })
        .catch((reason: unknown) => {
          // A frame the browser refused to decode: a truncated WebP, or an
          // encoder that produced something it cannot read. Skipped and logged,
          // like every other unreadable message on this path, and the panel
          // keeps the picture it has -- one short frame in a thousand must not
          // blank a rack. Unhandled, this would surface as a promise rejection
          // with no owner, from inside a socket callback.
          console.warn(`the frame store could not decode a frame for screen ${screenId}`, reason)
        })
    },

    lastAt(screenId) {
      return drawnAt.get(screenId) ?? null
    },

    onWatchedChange(listener) {
      watchers.add(listener)
      for (const screenId of panels.keys()) listener(screenId, true)
      return () => {
        watchers.delete(listener)
      }
    },
  }
}

/**
 * The one store this tab's panels draw from.
 *
 * A module singleton rather than a context, for the same reason the socket is
 * not one: it holds no React state, nothing re-renders when it changes, and a
 * context would put a provider between every panel and the frames it draws for
 * no gain. It is safe to share because it holds nothing per-screen once the
 * last panel on that screen unmounts.
 */
export const frameStore = createFrameStore()
