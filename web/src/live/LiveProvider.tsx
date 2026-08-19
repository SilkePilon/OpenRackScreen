import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useEffect, type ReactNode } from "react"

import { api } from "@/api/client"
import { claimsKey, daemonsKey, type Daemon } from "@/api/queries"
import { frameStore } from "@/live/frames"
import { createLiveSocket, type SessionAnswer } from "@/live/socket"

/**
 * The one `/ws/ui` connection, and the two places what arrives on it goes.
 *
 * This is the only module that holds both ends. The socket knows nothing about
 * panels or about a query cache; the frame store knows nothing about a
 * connection; a panel knows about neither. Here they are wired to each other,
 * three lines each, and the wiring is the whole of what this component is -- it
 * renders its children and nothing of its own, and it never re-renders them,
 * because nothing that arrives on the socket is state it holds.
 *
 * Frames go to the store, which hands them to panels through a ref. `daemons`
 * goes into the Query cache with `setQueryData`, so the online dots and every
 * panel's own sense of whether its rack is there update without a refetch --
 * `GET /api/daemons` assembles a snapshot per rack on the event loop, and
 * asking it for something the socket just said would spend the server's
 * frame-relay time on it.
 *
 * A third wire, added later and running the other way: when a dial is *refused*,
 * the socket asks a question it cannot answer itself, and this is where the
 * question is turned into a request. See `askWhetherTheSessionEnded`.
 */

/**
 * Where `/ws/ui` is: on the origin this interface was served from.
 *
 * One origin by design -- the server mounts the built assets below its own API
 * -- so the session cookie needs no CORS and this socket needs no second host,
 * and in development Vite proxies `/ws` to the same place.
 *
 * The scheme is a substitution rather than a branch: `location.origin` is
 * always `scheme://host`, so an anchored `^http` turns `http` into `ws` and
 * `https` into `wss` and can touch nothing else. A conditional would be a
 * second path that only a page served over TLS ever takes, which is to say the
 * one path no test in a jsdom on `http://localhost` could reach.
 */
function liveUrl(): string {
  return `${window.location.origin.replace(/^http/, "ws")}/ws/ui`
}

/**
 * Patch the racks the cache already holds with who is up.
 *
 * Only `online`, and only for rows that are already there. The `daemons`
 * message is a list of ids: it says which racks the server holds a socket for,
 * not what any of them is. A client that added a row for an id it had never
 * fetched would be inventing server state -- a rack with no name, no versions
 * and no status, for the Daemons page to render -- and no server state is
 * invented by this client. A cache nobody has filled stays empty; the page that
 * fetches the list gets `online` from the server in the same answer.
 */
function withOnline(online: number[]) {
  return (racks: Daemon[] | undefined): Daemon[] | undefined =>
    racks?.map((rack) => {
      const up = online.includes(rack.id)
      return rack.online === up ? rack : { ...rack, online: up }
    })
}

/**
 * Turn a refused handshake into the 401 it really is, on a route that can say so.
 *
 * **The whole point is what this function does not do**: it does not decide
 * anything. It makes one ordinary request through the ordinary client, and the
 * `setUnauthorizedHandler` middleware in `api/client.ts` -- the one place in this
 * interface where "a 401 returns to /login once" is written down -- does the
 * rest, exactly as it does for a query on any page. A refused handshake is a
 * 401; it just needed something able to carry one.
 *
 * **Why `GET /api/settings` and not `GET /api/auth/me`.** `me` is open and
 * answers 200 either way, on purpose -- it is how the interface tells "no
 * password has ever been set" from "there is one and this browser has no
 * session". It would report `authenticated: false` and trigger nothing, so
 * acting on it would mean reading a body and deciding what it means: a second
 * copy of the 401 rule, beside the first and free to disagree with it. The
 * request has to be one that is genuinely refused, so it has to be guarded.
 *
 * **Why this guarded route.** `GET /api/daemons` is out by §4.4 -- it assembles a
 * snapshot per rack on the event loop, which is the server's frame-relay time.
 * `GET /api/integrations` needs a `daemon_id`, and a probe that invents a rack id
 * can be refused for a reason that is not the session. `/api/settings` takes no
 * parameters, belongs to no rack, and reads two named keys of one table: the
 * smallest answer the guarded router has. Its body is discarded -- what is being
 * asked about is the session, not the settings.
 *
 * **What counts as an answer.** A 2xx (the session is fine) or a 401 (it is not,
 * and the middleware has already acted). Nothing else: a 502 from a proxy in
 * front of a stopped server is a server that is down wearing an HTTP status, and
 * calling it an answer would leave a client behind a reverse proxy refused
 * forever with nothing saying why -- which is the defect this exists to end.
 */
async function askWhetherTheSessionEnded(): Promise<SessionAnswer> {
  try {
    const { response } = await api.GET("/api/settings")
    return response.ok || response.status === 401 ? "answered" : "unanswered"
  } catch {
    // openapi-fetch builds the Request and awaits `fetch`, which rejects when
    // nothing answered at all. That is the server-is-down case, and it is not an
    // answer to anything: nothing here is said, nothing is navigated, and the
    // panels go on saying what they were saying.
    return "unanswered"
  }
}

/**
 * Re-ask for the racks waiting to join, because something moved on the server.
 *
 * Design spec S7 requires that "a rack that appears while the page is open
 * appears without a reload", and names this socket as what does it. **What
 * carries it is the `daemons` message, because that is the only message this
 * socket has that is not a picture of one panel.** There is no `claim` frame:
 * `ws_ui.py` encodes exactly two types, and the route a rack files a claim
 * through is an unauthenticated `POST` that touches no hub and wakes no browser.
 *
 * So this is honest about its reach and it is worth writing down rather than
 * discovering later. What it covers is every moment the server's set of
 * connected racks changes -- which includes the end of this very flow, a rack
 * that was approved collecting its key and dialling in, and every reconnect on
 * a site with more than one rack. What it does not cover is a claim filed on a
 * network where nothing else moves at all; that entry waits for the next
 * `daemons` message, or for this tab to be focused again, which is when React
 * Query re-asks a stale query on its own.
 *
 * **Invalidated rather than patched**, for `useMutate`'s reason: no server state
 * is invented by this client. The message says which racks are online and
 * nothing whatever about what is waiting, so the only correct response to it is
 * to go and ask. It costs nothing on the pages that do not show the list --
 * `invalidateQueries` refetches observed queries, and the claims query is
 * mounted only by the Daemons page.
 */
function theClaimsAreStale(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: claimsKey })
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  useEffect(() => {
    const live = createLiveSocket({
      url: liveUrl(),
      onDaemons: (online) => {
        queryClient.setQueryData(daemonsKey, withOnline(online))
        // And the claims, which this message does not carry and cannot: see
        // `theClaimsAreStale`.
        theClaimsAreStale(queryClient)
      },
      onFrame: (frame) => frameStore.push(frame),
      probeSession: askWhetherTheSessionEnded,
    })

    /**
     * The screens with a panel on them, which is not the same as what is on the
     * wire.
     *
     * The store announces what is *on the page*; the socket is told what this
     * tab wants *now*, and a hidden tab wants nothing. Kept here rather than
     * asked of either side: the store is about panels and has no idea a tab can
     * be hidden, and the socket's own set is the answer to "what did I ask for",
     * which is exactly what a hide empties.
     */
    const onScreen = new Set<number>()
    const hidden = () => document.visibilityState === "hidden"

    // Registering replays the screens that already have a panel on them --
    // React runs a child's effects before its parent's, so every panel below
    // this one has already subscribed by now -- and the client holds what it is
    // asked for until the handshake finishes, so those go out on `open` with
    // the rest. The replay is load-bearing; without it every panel on a first
    // page load would be watched by nobody.
    //
    // Before the dial rather than after, though nothing can currently tell:
    // what has to be true is that the set is complete before the socket
    // *opens*, and no browser opens one synchronously from `new WebSocket`. So
    // this order is the one that needs no argument about when `onopen` can
    // fire, not one a test can distinguish -- a mutant that swaps the two lines
    // survives the suite, and is disclosed as equivalent rather than papered
    // over with a test that would only be asserting this line's position.
    const stopWatching = frameStore.onWatchedChange((screenId, watched) => {
      if (!watched) {
        onScreen.delete(screenId)
        live.unsubscribe(screenId)
        return
      }
      onScreen.add(screenId)
      // Not while the tab is hidden. A panel can mount with the tab in the
      // background -- a query settling, a socket message, anything that renders
      // -- and subscribing there is the same encode for nobody that hiding the
      // tab just stopped. It goes out on the way back, from the set.
      if (!hidden()) live.subscribe(screenId)
    })

    /**
     * A hidden tab watches nothing, and picks it all up again on the way back.
     *
     * Spec §5.4: "Frames flow only while someone is watching. Subscribe on
     * mount, unsubscribe on unmount and on tab-hide. A closed tab that kept its
     * subscription would leave the Pi encoding WebP for nobody." A backgrounded
     * tab is four panels' worth of WebP a rack encodes twice a second for a page
     * nobody can see, and the Pi is the machine paying for it.
     *
     * On the wire and nowhere else. The panels stay mounted and the store keeps
     * every bitmap it holds, so a tab coming back shows its last picture at once
     * rather than a black canvas -- the store's "forget the screen with its last
     * panel" rule is about a panel going away, and no panel has gone away here.
     * The staleness a hidden tab accumulates is true and is left alone: nothing
     * was arriving, and the panel says so until the first frame back.
     */
    const onVisibility = () => {
      for (const screenId of onScreen) {
        if (hidden()) live.unsubscribe(screenId)
        else live.subscribe(screenId)
      }
    }
    document.addEventListener("visibilitychange", onVisibility)
    live.connect()

    return () => {
      document.removeEventListener("visibilitychange", onVisibility)
      stopWatching()
      // For good: no reconnect follows this one. StrictMode mounts this effect
      // twice, and a client left dialling would be a second connection to the
      // same server with nobody holding it.
      live.close()
    }
  }, [queryClient])

  return children
}
