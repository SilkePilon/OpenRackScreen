import { useQueryClient } from "@tanstack/react-query"
import { useEffect, type ReactNode } from "react"

import { daemonsKey, type Daemon } from "@/api/queries"
import { frameStore } from "@/live/frames"
import { createLiveSocket } from "@/live/socket"

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

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  useEffect(() => {
    const live = createLiveSocket({
      url: liveUrl(),
      onDaemons: (online) => queryClient.setQueryData(daemonsKey, withOnline(online)),
      onFrame: (frame) => frameStore.push(frame),
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
