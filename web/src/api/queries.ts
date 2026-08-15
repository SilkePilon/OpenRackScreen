import { skipToken, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api, detailFrom } from "./client"
import type { components } from "./schema"

/** One key per resource. Everything that touches the session invalidates this. */
export const sessionKey = ["session"] as const

/** One rack, as `GET /api/daemons` reports it. Generated, never written here. */
export type Daemon = components["schemas"]["DaemonView"]

/** The racks. Fetched by the pages that list them; patched by the live socket. */
export const daemonsKey = ["daemons"] as const

/**
 * The racks as this tab already knows them -- read from the cache, never fetched.
 *
 * `skipToken` rather than a query function, and that is the whole point of this
 * hook. Two things read the racks without being the reason to ask the server
 * for them: the header's status strip, which is on every page, and each panel,
 * which needs to know whether its own rack is still there. Neither should turn
 * a page that shows a panel into a page that fetches the daemon list, and
 * `GET /api/daemons` is not free -- it assembles a snapshot per rack on the
 * event loop, which is time the server owes to relaying frames.
 *
 * So this subscribes to the cache entry and nothing else. It re-renders its
 * component when the entry changes -- which is what the `daemons` message
 * writing through `setQueryData` is for -- and answers `undefined` until
 * something that *does* fetch has filled it in. `undefined` means "this
 * interface has not been told", which is deliberately not the same as "there
 * are no racks" and must never be drawn as "the rack is gone".
 */
export function useCachedDaemons(): Daemon[] | undefined {
  return useQuery<Daemon[]>({ queryKey: daemonsKey, queryFn: skipToken }).data
}

/**
 * The racks, asked of the server.
 *
 * The other half of `useCachedDaemons`, on the same key: this is the hook that
 * *fills* the entry, and the Daemons page is the page that legitimately asks.
 * A page that only needs to know who is up mounts the cached one instead --
 * `GET /api/daemons` assembles a snapshot per rack on the event loop, and
 * that is time the server owes to relaying frames.
 *
 * Nothing polls. What keeps `online` true between fetches is the `daemons`
 * message writing this same entry through `setQueryData` in `LiveProvider`,
 * which is the only other source of it there is.
 */
export function useDaemons() {
  return useQuery<Daemon[]>({
    queryKey: daemonsKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/daemons")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the racks could not be read")
      }
      return data
    },
  })
}

/** A rack and the token that pairs it. Answered once, by two routes, and never listed. */
export type DaemonCreated = components["schemas"]["DaemonCreated"]

/** What `POST /api/daemons/{id}/push` answers: the version it minted, and whether it left. */
export type Pushed = components["schemas"]["Pushed"]

/** What a DELETE answers. No route in M3a answers 204, this one included. */
export type Deleted = components["schemas"]["Deleted"]

/** One line of a rack's recent history. */
export type DaemonEvent = components["schemas"]["EventView"]

/** Per rack, because the route is per rack and so is the ring behind it. */
export const eventsKey = (daemonId: number) => ["events", daemonId] as const

/**
 * How many events a rack's panel asks for.
 *
 * Well under the server's `MAX_EVENTS_PER_DAEMON` of 200, on purpose: the panel
 * is a glance at what just happened, and a rack that has been flapping fills
 * two hundred rows with connects and disconnects that say nothing a person can
 * act on beyond the first few.
 */
export const RECENT_EVENTS = 20

/** A rack's recent events, newest first, as the route answers them. */
export function useEvents(daemonId: number) {
  return useQuery<DaemonEvent[]>({
    queryKey: eventsKey(daemonId),
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/events", {
        params: { query: { daemon_id: daemonId, limit: RECENT_EVENTS } },
      })
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the events could not be read")
      }
      return data
    },
  })
}

/** One panel, as `GET /api/screens` reports it. Generated, never written here. */
export type Screen = components["schemas"]["ScreenView"]

/**
 * Every rack's screens, on one key.
 *
 * One entry rather than one per rack, because the route that reorders them can
 * name more than one rack's screens in a single call -- `POST
 * /api/screens/reorder` reads the affected set from the rows -- so an
 * invalidation would have to guess which per-rack entries a write touched. The
 * list is a handful of rows; the page groups it by rack itself.
 */
export const screensKey = ["screens"] as const

/**
 * The screens, in the order the server puts them.
 *
 * `ORDER BY position, id`, server-side, and this hook does not re-sort. The
 * tiebreak matters: two panels sharing a position is a state the schema allows,
 * and it is the same tiebreak `snapshot._screens` uses, so what the canvas draws
 * and what the rack renders are in the same order. A client sorting by id would
 * draw a rack nobody wired.
 */
export function useScreens() {
  return useQuery<Screen[]>({
    queryKey: screensKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/screens")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the screens could not be read")
      }
      return data
    },
  })
}

/** One template, as `GET /api/templates` reports it. */
export type Template = components["schemas"]["TemplateView"]

/** What one of a template's parameters is, and what an editor should offer for it. */
export type ParamSpec = components["schemas"]["ParamSpec"]

/** The templates. Not per rack: a template is the server's, and any screen may name one. */
export const templatesKey = ["templates"] as const

export function useTemplates() {
  return useQuery<Template[]>({
    queryKey: templatesKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/templates")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the templates could not be read")
      }
      return data
    },
  })
}

/**
 * One integration, as `GET /api/integrations` reports it.
 *
 * There is no credential field at all: `has_credential` is a boolean, so the
 * interface can say one is stored and offer to replace it without the plaintext
 * ever travelling.
 */
export type Integration = components["schemas"]["IntegrationView"]

/** Per rack, because a screen can only bind to readings its own rack polls. */
export const integrationsKey = (daemonId: number) => ["integrations", daemonId] as const

export function useIntegrations(daemonId: number) {
  return useQuery<Integration[]>({
    queryKey: integrationsKey(daemonId),
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/integrations", {
        params: { query: { daemon_id: daemonId } },
      })
      if (!data) {
        throw new ApiError(
          response.status,
          detailFrom(error) ?? "the integrations could not be read",
        )
      }
      return data
    },
  })
}

/** The server's own settings: the timezone and the night window a screen overrides. */
export type Settings = components["schemas"]["SettingsView"]

export const settingsKey = ["settings"] as const

export function useSettings() {
  return useQuery<Settings>({
    queryKey: settingsKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/settings")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the settings could not be read")
      }
      return data
    },
  })
}

export type Session = {
  authenticated: boolean
  password_set: boolean
}

/**
 * The two states a server can be in before anyone is signed in, as the server
 * reports them.
 *
 * `GET /api/auth/me` is open and answers 200 either way -- it is how the
 * interface tells "no password has ever been set on this rack" from "there is a
 * password and this browser has no session", without guessing from a status
 * code. It is the one route that never 401s.
 */
export function useSession() {
  return useQuery({
    queryKey: sessionKey,
    // `retry: false`: the answer decides which screen is legal to show, so a
    // server that cannot answer has to say so now rather than after a backoff.
    retry: false,
    queryFn: async (): Promise<Session> => {
      const { data, error, response } = await api.GET("/api/auth/me")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the server did not answer")
      }
      // The response model is `dict[str, bool]`, so the generated type is an
      // index signature and every key is `boolean` whether the server sent it
      // or not. Read the two the interface needs by name, and treat a missing
      // one as false rather than as undefined leaking into a branch.
      return {
        authenticated: data.authenticated === true,
        password_set: data.password_set === true,
      }
    },
  })
}

/**
 * Claim the admin password on a first run.
 *
 * The server's claim is atomic, so a second browser racing this one is refused
 * with 409 rather than quietly overwriting the first one's password.
 */
export function useClaimPassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (password: string) => {
      const { error, response } = await api.POST("/api/auth/setup", { body: { password } })
      if (!response.ok) {
        throw new ApiError(response.status, detailFrom(error) ?? "the password was not set")
      }
    },
    // Removed rather than invalidated, here and below: the cached answer was
    // taken before the thing that just changed, and an invalidated query still
    // hands its stale value to the next render while it refetches -- which is a
    // guard deciding where to send you from a session that no longer exists.
    onSuccess: () => queryClient.removeQueries({ queryKey: sessionKey }),
  })
}

/** Exchange the password for a session cookie. The cookie is the browser's. */
export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (password: string) => {
      const { error, response } = await api.POST("/api/auth/login", { body: { password } })
      if (!response.ok) {
        throw new ApiError(response.status, detailFrom(error) ?? "the sign-in failed")
      }
    },
    onSuccess: () => queryClient.removeQueries({ queryKey: sessionKey }),
  })
}
