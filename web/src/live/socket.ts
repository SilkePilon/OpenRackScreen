/**
 * The one connection to `/ws/ui`: who is online, and the panels this tab watches.
 *
 * One responsibility, and the boundary is worth stating because everything
 * downstream of it belongs to somebody else. This module keeps a socket up,
 * remembers what this tab asked to watch, and hands whole parsed messages to two
 * callbacks. It holds no rendering state, decodes no images, and imports no
 * React. What a frame *means* -- which panel draws it, what a backwards `seq`
 * says about a rack that restarted, when a still image has gone stale -- is the
 * frame store's, and none of it can be decided from inside a socket.
 *
 * Three rules here come from the server rather than from this file, and each is
 * quoted at the line that keeps it.
 */

/** What the connection is doing, for an interface that has to say so. */
export type LiveState =
  /** Nothing is open and nothing is being attempted: before `connect`, after `close`. */
  | "closed"
  /** The first handshake of this client is in flight; nothing has ever arrived. */
  | "connecting"
  /** Up. Subscriptions go out as they are made. */
  | "open"
  /** It was up and is not: waiting out the backoff, or dialling again. */
  | "reconnecting"

/**
 * What came back from asking whether this browser still has a session.
 *
 * The two values are "the question was answered" and "it was not" -- deliberately
 * **not** "the session ended" and "it did not". An ended session is a 401, and
 * this interface has exactly one place a 401 goes; a client that reported the
 * answer here would be the second copy of that rule. All this module does with
 * an answer is stop asking.
 *
 * `"unanswered"` is every case where nothing that knows about sessions replied:
 * a request that reached no server at all, and equally a 502 from a proxy in
 * front of a server that is not running. Treating that as an answer would put a
 * client behind a reverse proxy back where this rule found it -- refused
 * forever, with nothing anywhere saying why.
 */
export type SessionAnswer = "answered" | "unanswered"

/** One panel's image, exactly as the rack encoded it. */
export type LiveFrame = {
  screenId: number
  /**
   * The rack's own counter for this screen, passed through untouched.
   *
   * Not compared to anything here. A `seq` lower than the last one means the
   * daemon restarted, which is a fact about a stream and not about a message,
   * and the store that holds the stream is the only place that can act on it.
   */
  seq: number
  /**
   * The decoded WebP. See `decode` for why there is no substitution in it.
   *
   * `Uint8Array<ArrayBuffer>` rather than a bare `Uint8Array`, whose buffer
   * TypeScript types as `ArrayBufferLike` -- which includes `SharedArrayBuffer`
   * and so is not a `BlobPart`. This one is `decode`'s own freshly allocated
   * buffer, never shared with anything, and saying so is what lets the frame
   * store hand it straight to a `Blob` without a copy or a cast.
   */
  bytes: Uint8Array<ArrayBuffer>
}

/**
 * As much of `WebSocket` as this module touches.
 *
 * Narrow on purpose: it is what a test can implement without a server, and a
 * real `WebSocket` satisfies it without a wrapper. `onerror` is not in it
 * because it earns nothing -- the protocol fires `close` after every `error`,
 * including a handshake that was refused, so the reconnect has exactly one door
 * to come in through instead of two that must agree.
 */
export type SocketLike = Pick<WebSocket, "send" | "close" | "onopen" | "onclose" | "onmessage">

export type LiveSocket = {
  /** Dial, and keep dialling until `close`. Doing it twice does nothing. */
  connect(): void
  /** Stop, for good. No reconnect follows this one. */
  close(): void
  /** Watch a screen now if the link is up, and after every reconnect. */
  subscribe(screenId: number): void
  /** Stop watching it, here and on the wire. */
  unsubscribe(screenId: number): void
  readonly state: LiveState
}

const FIRST_DELAY_MS = 1000
/**
 * The longest this client will ever wait before trying again.
 *
 * Half a minute because the thing on the other end is a server somebody is
 * restarting or a network somebody is fixing, and because a tab left open on a
 * second monitor must come back on its own within the time it takes to notice
 * it has not. Uncapped doubling reaches hours by the end of a working day.
 */
const MAX_DELAY_MS = 30_000
/**
 * How much of a delay the jitter may take off it, at most.
 *
 * Subtracted rather than added, so `MAX_DELAY_MS` stays the truth about the
 * worst case and the schedule is a ceiling rather than an average. A quarter is
 * enough to break up a wall of tabs that all lost the same server in the same
 * second -- a rack dashboard is exactly the page somebody has open four times --
 * without making the first retry so early that it lands before the server has
 * finished starting.
 */
const JITTER = 0.25
/**
 * How long a connection must last to count as one that worked.
 *
 * The backoff is reset by a connection that *stayed up*, not by one that merely
 * opened, and this is the difference. A server restarting in a loop, or a proxy
 * in front of a server that is not there, accepts the handshake and drops it
 * again -- and a client that reset its counter on `open` would dial that once a
 * second for as long as the tab is open, which is the hammer the backoff exists
 * to prevent. The threshold is the cap itself: a link that held for as long as
 * the longest wait this client would have imposed has proved the server is
 * there, and anything shorter is a flap that should go on climbing.
 *
 * The cost, named rather than left to be rediscovered: a link that flaps every
 * twenty seconds but *works* in between never clears the counter, so it settles
 * at half-minute gaps where a reset-on-open would be back in a second. That is
 * the trade being made, and it is made this way on purpose -- the failure it
 * accepts is a slow recovery on a link that is already broken, and the failure
 * it refuses is a tab hammering a restarting server once a second for hours.
 * The second one takes a server down; the first one takes a few seconds.
 */
const STABLE_MS = MAX_DELAY_MS

/** What one message from the server may name, before any of it is believed. */
type Envelope = {
  type?: unknown
  online?: unknown
  screen_id?: unknown
  seq?: unknown
  webp?: unknown
}

export function createLiveSocket({
  url,
  onDaemons,
  onFrame,
  probeSession,
  now = () => performance.now(),
  random = Math.random,
  openSocket = (target) => new WebSocket(target),
}: {
  url: string
  /**
   * The racks the server holds a socket for, ascending, as it sent them.
   *
   * Passed through unsorted and uncompared: the server sorts, and says it sorts
   * so that two consecutive messages saying the same thing look the same. A
   * client that sorted again would be the second implementation of one rule.
   */
  onDaemons: (online: number[]) => void
  onFrame: (frame: LiveFrame) => void
  /**
   * Ask, once, whether this browser still has a session -- because a dial was
   * refused and a browser is told nothing about why.
   *
   * **The one thing this module cannot see.** A handshake that is rejected gives
   * the page no status code: `onerror` carries nothing and the close code is
   * 1006, which is the same 1006 a server that is not running produces. So a
   * dial that closed without ever opening is *either* a dead server or a dead
   * session, and only something that speaks HTTP can tell them apart.
   *
   * Injected rather than done here, and the split is the point: this module owns
   * **when** the question is worth asking, because that is a fact about the dial
   * lifecycle and the backoff it rides on; the caller owns **what asking means**,
   * because that is a route, an API client and a 401 rule -- none of which
   * belongs in a socket. See `LiveProvider`, which supplies the only
   * implementation that ships.
   *
   * Optional, because a client with no way to ask is exactly task 5's client:
   * it retries and says nothing, which is the honest behaviour when there is
   * nobody to ask.
   */
  probeSession?: () => Promise<SessionAnswer>
  /** Monotonic milliseconds. Only ever used to measure how long a link held. */
  now?: () => number
  /** `[0, 1)`. Injected so a test can pin a schedule that is deliberately not fixed. */
  random?: () => number
  openSocket?: (url: string) => SocketLike
}): LiveSocket {
  /**
   * The screens this tab wants, in the order it asked for them.
   *
   * Kept here rather than left to the server because the server does not keep
   * it either -- not across a socket. A dropped connection takes this tab's
   * whole subscription with it, so the set has to be re-stated on the next
   * `open` or the panels stay frozen until somebody reloads the page, which is
   * the failure the spec's table names against "`/ws/ui` drops".
   */
  const wanted = new Set<number>()
  let socket: SocketLike | null = null
  let state: LiveState = "closed"
  let timer: ReturnType<typeof setTimeout> | null = null
  /** Consecutive connections that failed or did not last. Drives the delay. */
  let attempt = 0
  /** When the current connection opened, or `null` if it never did. */
  let openedAt: number | null = null
  /**
   * Whether the session question has been asked *and answered* since a
   * connection last opened. What stops the asking from becoming a poll.
   */
  let answered = false
  /** Whether one is in flight, so two refused dials cannot make two questions. */
  let asking = false
  /**
   * Which question an answer arriving now belongs to.
   *
   * A probe is an HTTP request and takes as long as it takes; the reconnect
   * behind it is a second away. So a connection can open while an answer is
   * still on its way, and applying that answer would latch the question shut on
   * the strength of something that was true one connection ago -- which is a
   * client that never asks again, for the rest of the tab's life. Bumped by
   * everything that makes an earlier answer out of date; an answer whose era has
   * moved on is dropped.
   */
  let era = 0

  /** Whatever was known about the session is old news; ask again if refused. */
  function reopenTheQuestion(): void {
    era += 1
    answered = false
  }

  /**
   * Ask whether the session is what refused that handshake -- at most once.
   *
   * Called only from a close that never opened. Everything that keeps this from
   * becoming a poll is here or in the schedule it rides on: one at a time, never
   * again once something answered, and never more often than the backoff dials.
   * A session that really ended costs exactly one of these.
   *
   * An answer of `"unanswered"` deliberately latches nothing. That is the
   * server-is-down case, and it is the ordinary order of events -- the server
   * goes away first and comes back with this tab's session forgotten second -- so
   * a rule that asked only once would have spent its one question on the outage
   * and never noticed the refusal that followed.
   *
   * A `probeSession` that *throws* is treated as an answer of nothing, for the
   * same reason and one more: leaving `asking` true would disable the question
   * for good, silently, which is this defect again in a smaller room.
   */
  function askWhetherTheSessionEnded(): void {
    if (probeSession === undefined || asking || answered) return
    asking = true
    const asked = era
    void probeSession().then(
      (answer) => {
        asking = false
        if (answer === "answered" && era === asked) answered = true
      },
      () => {
        asking = false
      },
    )
  }

  function nextDelay(): number {
    const ceiling = Math.min(MAX_DELAY_MS, FIRST_DELAY_MS * 2 ** attempt)
    attempt += 1
    // Rounded because a fractional millisecond is a delay no timer can keep and
    // a number nothing can be asserted about.
    return Math.round(ceiling * (1 - JITTER * random()))
  }

  function tell(action: "subscribe" | "unsubscribe", screenId: number): void {
    // Exactly two fields, and never a third. `_Request` is `extra="forbid"`,
    // so a message carrying anything else is refused whole -- and the tab, which
    // hears nothing back either way, goes on believing it subscribed.
    socket?.send(JSON.stringify({ action, screen_id: screenId }))
  }

  /**
   * Whether this is something the server could act on, before it is sent.
   *
   * `screen_id` is a SQLite rowid: at least 1, and small enough that a JS number
   * still is one. Refused here rather than left to the server because of what
   * the server does with the ones it refuses -- it logs and skips, and there is
   * no answer on this socket, so a `NaN` from a URL that did not parse (which
   * `JSON.stringify` writes as `null`) would leave a panel silently unwatched
   * with nothing anywhere saying why. `Number.isSafeInteger` is stricter than
   * the server's `2**63 - 1` on purpose: past 2**53 a JS number is not the row
   * id any more, it is the nearest one it could represent.
   */
  function isRowId(screenId: number): boolean {
    // `isSafeInteger` is false for `true`, which is the point: `bool` is an
    // `int` in Python and pydantic's lax mode reads `true` as 1, so a flag sent
    // where an id belongs subscribes to whatever screen has row id 1 and shows
    // one panel another's image. The server refuses it too; this is the half
    // that can say so where somebody will see it.
    return Number.isSafeInteger(screenId) && screenId >= 1
  }

  /**
   * The base64 on this socket, and the one line of it that matters.
   *
   * `atob` and no substitution. `Frame` on the *link* carries
   * `ser_json_bytes="base64"` and pydantic emits the URL-safe alphabet -- `-`
   * and `_` where the standard one has `+` and `/` -- and `atob` throws
   * `InvalidCharacterError` on both. So `ws_ui._frame_message` assembles this
   * message by hand with `base64.b64encode` rather than dumping the model, to
   * guarantee the standard alphabet with its padding, and that guarantee is
   * what this reads. Adding `.replace(/-/g, "+").replace(/_/g, "/")` here would
   * paper over the day the server stopped keeping it, and the failure would be
   * a page showing nothing with every test green: `base64.b64decode` accepts
   * either alphabet unless asked not to, so the Python end cannot see it.
   */
  function decode(webp: string): Uint8Array<ArrayBuffer> {
    const binary = atob(webp)
    const bytes = new Uint8Array(binary.length)
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index)
    }
    return bytes
  }

  /**
   * Read one message, or skip it.
   *
   * Skipped and logged, never closed, and this is the server's own rule
   * mirrored: `_read` skips a request it cannot validate because closing turns
   * one bad message into a reconnect loop. The same is true in this direction
   * and worse -- the build of the SPA in the tab may be older than the server,
   * and a message type added since would otherwise take the whole live view
   * down on a page that was working a minute ago.
   */
  function receive(data: unknown): void {
    if (typeof data !== "string") return skip("a message that was not text", typeof data)
    let message: unknown
    try {
      message = JSON.parse(data)
    } catch {
      return skip("a message that was not JSON", data.slice(0, 120))
    }
    if (typeof message !== "object" || message === null) {
      return skip("a message that was not an object", data.slice(0, 120))
    }
    const envelope = message as Envelope
    if (envelope.type === "daemons") {
      if (!isIdList(envelope.online)) {
        return skip("a daemons message without a list of ids", data.slice(0, 120))
      }
      onDaemons(envelope.online)
      return
    }
    if (envelope.type === "frame") {
      const { screen_id: screenId, seq, webp } = envelope
      if (typeof screenId !== "number" || typeof seq !== "number" || typeof webp !== "string") {
        return skip("a frame missing a field", data.slice(0, 120))
      }
      let bytes: Uint8Array<ArrayBuffer>
      try {
        bytes = decode(webp)
      } catch {
        // Named rather than swallowed, and the head of the payload is logged
        // with it: the only way to get here is the server having changed which
        // alphabet it sends, and the first characters of what it sent are the
        // whole answer. Sliced like every other skip above, because this is the
        // one branch that fires on *every* frame once it fires at all -- four
        // panels at 2 fps is some 80 KB/s of strings the console holds on to,
        // and devtools that have to be closed are no use for the incident this
        // line exists to diagnose. See `decode`, and `_frame_message` in
        // `ws_ui.py`.
        return skip(
          `a frame whose base64 this browser cannot read, for screen ${screenId}`,
          webp.slice(0, 120),
        )
      }
      onFrame({ screenId, seq, bytes })
      return
    }
    skip("a message of a type it does not know", envelope.type)
  }

  function skip(what: string, detail: unknown): void {
    console.warn(`the live socket skipped ${what}`, detail)
  }

  /**
   * Whether that really is a list of rack ids.
   *
   * Spelled as a predicate rather than inline, because `Array.isArray` on an
   * `unknown` narrows it to `any[]` -- and an `any` handed to `onDaemons` is
   * this module promising the rest of the interface a `number[]` it never
   * looked at.
   */
  function isIdList(value: unknown): value is number[] {
    return Array.isArray(value) && value.every((id) => typeof id === "number")
  }

  function dial(): void {
    timer = null
    openedAt = null
    const dialled = openSocket(url)
    socket = dialled

    // Every handler checks that it is still the current socket first. A
    // `close()` mid-flight, or a browser that fires `close` after the page has
    // already moved on, would otherwise reconnect a client that was told to
    // stop -- and a message from a socket this client has let go of would be
    // delivered as if it were live.
    dialled.onopen = () => {
      if (socket !== dialled) return
      state = "open"
      openedAt = now()
      // The server took this tab back, so whatever was learned about the session
      // while it would not is no longer the answer to anything.
      reopenTheQuestion()
      // The whole point of keeping the set. The server has no memory of this
      // tab across a socket: `_let_go` takes every subscription out of the hub
      // when the last one ended, so a panel that is still on screen is watched
      // by nobody until this says so again.
      for (const screenId of wanted) tell("subscribe", screenId)
    }

    dialled.onmessage = (event) => {
      if (socket !== dialled) return
      receive(event.data)
    }

    dialled.onclose = () => {
      if (socket !== dialled) return
      socket = null
      const neverOpened = openedAt === null
      if (openedAt !== null && now() - openedAt >= STABLE_MS) attempt = 0
      state = "reconnecting"
      timer = setTimeout(dial, nextDelay())
      // A connection that opened and then went away says nothing about the
      // session -- it was good a moment ago. One that *never opened* is the
      // ambiguous case: a refused handshake and an absent server look identical
      // from here. Asked after the reconnect is armed, because recovering is
      // this client's job and the question is somebody else's.
      if (neverOpened) askWhetherTheSessionEnded()
    }
  }

  return {
    connect() {
      // A socket in hand or a dial already armed means this is a second call,
      // and a second call must not leave the first connection unreferenced and
      // still open -- one per call, forever, each with its own reconnect.
      if (socket !== null || timer !== null) return
      state = "connecting"
      attempt = 0
      // With the backoff: a client being started again is a client that knows
      // nothing yet, and an answer left over from before the close belongs to a
      // connection nobody holds.
      reopenTheQuestion()
      dial()
    },

    close() {
      state = "closed"
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      const dialled = socket
      // Cleared *before* the close, so the `onclose` this provokes takes the
      // not-my-socket door above and arms nothing. A deliberate close is not a
      // drop to recover from, and a provider unmounting must not leave a client
      // dialling a server nobody is looking at.
      socket = null
      dialled?.close()
    },

    subscribe(screenId) {
      if (!isRowId(screenId)) {
        return skip("a subscribe to something that is not a screen id", screenId)
      }
      // Already held. The server calls a repeat a no-op at the hub and still
      // pays for it -- a whole-rack recomputation and two SQLite reads on the
      // event loop -- so the cheapest place to not send it is here.
      if (wanted.has(screenId)) return
      wanted.add(screenId)
      if (state === "open") tell("subscribe", screenId)
    },

    unsubscribe(screenId) {
      // Both halves or neither: a screen dropped from the set but not from the
      // wire goes on costing a rack the WebP encode until the socket closes,
      // and one dropped from the wire but not from the set comes back by itself
      // on the next reconnect.
      if (!wanted.delete(screenId)) return
      if (state === "open") tell("unsubscribe", screenId)
    },

    get state() {
      return state
    },
  }
}
