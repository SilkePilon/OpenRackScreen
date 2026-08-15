import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { Panel } from "../src/components/Panel"
import { createFrameStore, frameStore } from "../src/live/frames"
import { recordCanvas, stubDecoder } from "./paint"

// Screen ids, seq numbers and rack ids are drawn from three ranges that do not
// meet, and no id is its own index in any array below. This project has already
// lost two bugs to a fixture where a screen id was its own position, so a store
// that returned the wrong screen's bitmap, or delivered by index, cannot pass
// anything here by coincidence.
//
// Screens: 41, 12, 58, 31, 77, 63, 96, 84, 19. Seqs: 104 and up, plus one that
// goes backwards. Racks: 7 and 23.
const bytesFor = (mark: number) => new Uint8Array([0xff, 0xd8, mark, 0x00, mark])

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("the frame store", () => {
  it("delivers a frame only to the panel that asked for it", async () => {
    const decoder = stubDecoder()
    const store = createFrameStore()
    const watching41: unknown[] = []
    const watching12: unknown[] = []
    const release41 = store.subscribe(41, (bitmap) => watching41.push(bitmap))
    const release12 = store.subscribe(12, (bitmap) => watching12.push(bitmap))

    store.push({ screenId: 41, seq: 104, bytes: bytesFor(41) })
    await decoder.settleAll()

    expect(watching41).toEqual([decoder.made[0]])
    // The panel next to it asked for a different screen. A store that fanned
    // every frame out to every subscriber would show two panels one image --
    // which is the failure the whole `Map<screenId, Set<cb>>` exists to avoid,
    // and which no amount of looking at one panel would reveal.
    expect(watching12).toEqual([])

    // And a frame for a screen nobody is watching is dropped before it costs a
    // decode. The socket only asks for screens with a panel on them, so this is
    // the window between a panel unmounting and the unsubscribe reaching the Pi.
    store.push({ screenId: 58, seq: 104, bytes: bytesFor(58) })
    expect(decoder.blobs).toHaveLength(1)

    release41()
    release12()
  })

  it("closes the bitmap it replaces, so a tab does not grow", async () => {
    const decoder = stubDecoder()
    const store = createFrameStore()
    // Two panels on one screen -- the rack canvas and the inspector showing the
    // same screen, which is exactly what the Screens page does. This is why the
    // *store* closes the bitmap it replaces rather than each panel closing the
    // one it drew: with two subscribers, closing after the first one drew would
    // hand the second a bitmap that is already gone.
    const canvasDrew: unknown[] = []
    const inspectorDrew: unknown[] = []
    const releaseCanvas = store.subscribe(31, (bitmap) => canvasDrew.push(bitmap))
    const releaseInspector = store.subscribe(31, (bitmap) => inspectorDrew.push(bitmap))

    for (const seq of [104, 105, 106]) {
      store.push({ screenId: 31, seq, bytes: bytesFor(31) })
      await decoder.settleAll()
    }

    expect(decoder.made).toHaveLength(3)
    // Both panels drew all three, and drew the same object each time: one
    // decode serves every panel on a screen.
    expect(canvasDrew).toEqual(decoder.made)
    expect(inspectorDrew).toEqual(decoder.made)
    // The two it replaced are closed; the one on screen is not. Eight frames a
    // second for as long as a tab is open is why: without this the tab holds
    // every bitmap it ever decoded.
    expect(decoder.made.map((bitmap) => bitmap.closed)).toEqual([true, true, false])

    // The last panel goes. The store forgets the screen, and with it the last
    // bitmap -- so every bitmap it ever made has been closed exactly once, and
    // nothing is retained for a stream that has stopped.
    releaseCanvas()
    expect(decoder.made[2].closed).toBe(false)
    releaseInspector()
    expect(decoder.made.map((bitmap) => bitmap.closes)).toEqual([1, 1, 1])
  })

  it("treats a backwards seq as a new stream rather than as frames to drop", async () => {
    // The daemon restarted. Resetting is right; dropping would freeze the panel.
    const decoder = stubDecoder()
    const store = createFrameStore()
    const drew: unknown[] = []
    const release = store.subscribe(58, (bitmap) => drew.push(bitmap))

    store.push({ screenId: 58, seq: 118, bytes: bytesFor(58) })
    await decoder.settleAll()
    // Sequence 6 after 118: a rack that came back up and started counting
    // again. Read as an out-of-order frame this is dropped, and the panel holds
    // a picture from before the reboot until somebody reloads the page.
    store.push({ screenId: 58, seq: 6, bytes: bytesFor(58) })
    await decoder.settleAll()

    expect(drew).toEqual(decoder.made)
    expect(drew).toHaveLength(2)
    release()
  })

  it("re-renders nothing above the panel", async () => {
    // The whole reason this store exists. Four panels at 2 fps is eight frames
    // a second; through React state that is eight renders a second of the page
    // that owns them, forever, while a tab is open.
    const canvas = recordCanvas()
    const decoder = stubDecoder()
    let rackRenders = 0

    function Rack() {
      rackRenders += 1
      return <Panel screenId={77} daemonId={23} size={120} />
    }

    // No `StrictMode` here, and deliberately: it would double the count and
    // this test's number has to mean renders, not mounts. `panel.test.tsx`
    // mounts everything under StrictMode, which is where a double mount is
    // proved not to leak.
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Rack />
      </QueryClientProvider>,
    )
    expect(rackRenders).toBe(1)

    for (let frame = 0; frame < 8; frame += 1) {
      frameStore.push({ screenId: 77, seq: 200 + frame, bytes: bytesFor(77) })
      await decoder.settleAll()
    }

    // One render, and eight frames really did land -- without the second
    // assertion this passes just as well against a store that delivered
    // nothing at all.
    expect(rackRenders).toBe(1)
    expect(canvas.only().drawn).toHaveLength(8)
  })

  it("decodes what the socket handed it, as the WebP a browser expects", async () => {
    // The one line of this module the browser actually runs, and the only test
    // that can see it: everything above asserts what came *out* of the decoder,
    // which a store passing the wrong bytes, or no mime type, would still do.
    const decoder = stubDecoder()
    const store = createFrameStore()
    const bytes = new Uint8Array([0x52, 0x49, 0x46, 0x46, 0x00, 0x57, 0x45, 0x42, 0x50])
    const release = store.subscribe(63, () => {})

    store.push({ screenId: 63, seq: 104, bytes })
    const blob = decoder.blobs[0]

    expect(blob.type).toBe("image/webp")
    expect([...new Uint8Array(await blob.arrayBuffer())]).toEqual([...bytes])

    await decoder.settleAll()
    release()
  })

  it("hands a panel that arrives late the image already on screen", async () => {
    // The inspector opens on a screen the rack canvas has been streaming for a
    // minute. Without this it is blank until the next frame -- and on a rack
    // that has just gone quiet, blank forever.
    const decoder = stubDecoder()
    const store = createFrameStore()
    const releaseFirst = store.subscribe(96, () => {})
    store.push({ screenId: 96, seq: 104, bytes: bytesFor(96) })
    await decoder.settleAll()

    const late: unknown[] = []
    const releaseLate = store.subscribe(96, (bitmap) => late.push(bitmap))

    expect(late).toEqual([decoder.made[0]])
    // Handed out, not handed over: the store still owns it, and a second panel
    // arriving must not have cost a second decode.
    expect(decoder.made[0].closed).toBe(false)
    expect(decoder.blobs).toHaveLength(1)

    releaseFirst()
    releaseLate()
  })

  it("keeps the newest frame when two decodes finish out of order", async () => {
    // `createImageBitmap` decodes off the main thread, so two frames in flight
    // can land in either order. Drawing the older one would leave the panel a
    // frame behind for as long as the stream runs.
    const decoder = stubDecoder()
    const store = createFrameStore()
    const drew: unknown[] = []
    const release = store.subscribe(84, (bitmap) => drew.push(bitmap))

    store.push({ screenId: 84, seq: 104, bytes: bytesFor(84) })
    store.push({ screenId: 84, seq: 105, bytes: bytesFor(84) })
    await decoder.settle(1)
    await decoder.settle(0)

    expect(drew).toEqual([decoder.made[1]])
    // And the one that lost the race is closed rather than left to the garbage
    // collector, which is the same rule as the one it would have replaced.
    expect(decoder.made[0].closes).toBe(1)
    expect(decoder.made[1].closed).toBe(false)

    release()
  })

  it("skips a frame the browser cannot decode and keeps the one on screen", async () => {
    // A truncated WebP rejects the decode. Unhandled, that is a promise
    // rejection out of a socket callback; handled the wrong way, it is a panel
    // that goes blank because one frame in a thousand was short.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const decoder = stubDecoder()
    const store = createFrameStore()
    const drew: unknown[] = []
    const release = store.subscribe(19, (bitmap) => drew.push(bitmap))

    store.push({ screenId: 19, seq: 104, bytes: bytesFor(19) })
    await decoder.settleAll()
    store.push({ screenId: 19, seq: 105, bytes: new Uint8Array([0x00]) })
    await decoder.reject(1, "the source image cannot be decoded")

    expect(drew).toEqual([decoder.made[0]])
    expect(decoder.made[0].closed).toBe(false)
    expect(warn).toHaveBeenCalledTimes(1)
    release()
  })

  it("tells the socket which screens have a panel on them, and which no longer do", () => {
    // The one seam between the store and the connection. React mounts a child's
    // effects before its parent's, so the provider that owns the socket
    // registers *after* the panels have already subscribed -- if this did not
    // replay what is already watched, every panel on the first page load would
    // be subscribed by nobody and every canvas would stay black.
    const store = createFrameStore()
    const announced: [number, boolean][] = []
    const releaseCanvas = store.subscribe(96, () => {})

    const stopListening = store.onWatchedChange((screenId, watched) =>
      announced.push([screenId, watched]),
    )
    expect(announced).toEqual([[96, true]])

    // A second panel on the same screen is not a second subscription: the
    // server pays a whole-rack recomputation per subscribe, and the socket
    // already refuses a repeat.
    const releaseInspector = store.subscribe(96, () => {})
    const releaseOther = store.subscribe(84, () => {})
    expect(announced).toEqual([
      [96, true],
      [84, true],
    ])

    releaseCanvas()
    expect(announced).toHaveLength(2)
    releaseInspector()
    expect(announced).toEqual([
      [96, true],
      [84, true],
      [96, false],
    ])

    stopListening()
    releaseOther()
    expect(announced).toHaveLength(3)
  })
})
