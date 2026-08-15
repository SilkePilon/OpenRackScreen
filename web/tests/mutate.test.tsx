import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  type DefaultOptions,
} from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import { http, HttpResponse } from "msw"
import { StrictMode, type ReactNode } from "react"
import { afterEach, describe, expect, it } from "vitest"

import { ApiError, api, detailFrom, setUnauthorizedHandler } from "../src/api/client"
import { type Saved, useMutate } from "../src/api/mutate"
import type { components } from "../src/api/schema"
import { server } from "./msw"

type ScreenView = components["schemas"]["ScreenView"]
type DaemonView = components["schemas"]["DaemonView"]

// Three racks -- 4, 9 and 15 -- and screens 11, 12 and 7, at positions 1, 3 and
// 2, listed in the order 12 then 11. No id here equals an array index, a
// position, an id of the other resource, or a rack id: a mutation layer that
// handed back positions, indices or screen ids instead of the racks the header
// named would have to say so.
function panel(fields: { id: number; position: number; name: string; rack: number }): ScreenView {
  return {
    id: fields.id,
    daemon_id: fields.rack,
    name: fields.name,
    position: fields.position,
    display: { backend: "virtual", spi_bus: 0, spi_cs: 0, hz: 40_000_000 },
    rotation: 0,
    hflip: false,
    enabled: true,
    template: "clock",
    params: {},
    sleep_override: null,
  }
}

function rack(fields: { id: number; name: string }): DaemonView {
  return {
    id: fields.id,
    name: fields.name,
    status: "online",
    online: true,
    config_version: 5,
    applied_version: 5,
    config_error: null,
    version: "0.1.0",
    capabilities: {},
    last_seen: "2026-08-15T10:00:00Z",
    paired_at: "2026-08-01T09:00:00Z",
    created_at: "2026-08-01T09:00:00Z",
  }
}

const clock = panel({ id: 12, position: 1, name: "clock", rack: 9 })
const cpu = panel({ id: 11, position: 3, name: "cpu", rack: 4 })
const renamedCpu = { ...cpu, name: "gauge" }
const added = panel({ id: 7, position: 2, name: "memory", rack: 15 })

const newScreen: components["schemas"]["NewScreen"] = {
  name: "memory",
  position: 2,
  display: { backend: "virtual", spi_bus: 0, spi_cs: 0, hz: 40_000_000 },
  template: "clock",
  daemon_id: 15,
}

/**
 * `GET /api/screens` answering whatever the test last put in the list.
 *
 * Whatever it *last put there*, rather than a queue of successive answers,
 * because `StrictMode` mounts every hook twice and the query may legitimately
 * be asked more than once before a test does anything -- a queue would then be
 * advanced by the mount and the test would be measuring the wrong thing. The
 * count is read as a delta across the mutation instead, from a point where
 * nothing is in flight.
 */
function listing(rows: ScreenView[]) {
  let asked = 0
  let listed = rows
  let refusal: number | null = null
  return {
    handler: http.get("/api/screens", () => {
      asked += 1
      if (refusal !== null) {
        return HttpResponse.json({ detail: "the screens did not list" }, { status: refusal })
      }
      return HttpResponse.json(listed)
    }),
    asked: () => asked,
    answerWith: (next: ScreenView[]) => {
      listed = next
    },
    /** From here on the list itself fails, which is what a refetch can hit. */
    refuseWith: (status: number) => {
      refusal = status
    },
  }
}

/** The racks, for the one write that makes two different lists stale at once. */
function racking(rows: DaemonView[]) {
  let asked = 0
  let listed = rows
  return {
    handler: http.get("/api/daemons", () => {
      asked += 1
      return HttpResponse.json(listed)
    }),
    asked: () => asked,
    answerWith: (next: DaemonView[]) => {
      listed = next
    },
  }
}

const screensKey = ["screens"] as const
const daemonsKey = ["daemons"] as const

/**
 * A page's worth of writing: the list it reads, and three edits that change it.
 *
 * The three are the three shapes M3a answers a write with -- a create that
 * declares its own 201, an edit that leaves the status to the default and so
 * may be narrowed to 202, and a reorder that can name more than one rack at
 * once. Every one of them invalidates the same list, which is the only thing
 * `useMutate` is told about them.
 */
function useSaving() {
  const screens = useQuery({
    queryKey: screensKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/screens")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the screens did not list")
      }
      return data
    },
  })
  const create = useMutate({
    send: (body: components["schemas"]["NewScreen"]) => api.POST("/api/screens", { body }),
    invalidates: [screensKey],
  })
  const rename = useMutate({
    send: (edit: { id: number; name: string }) =>
      api.PATCH("/api/screens/{screen_id}", {
        params: { path: { screen_id: edit.id } },
        body: { name: edit.name },
      }),
    invalidates: [screensKey],
  })
  const reorder = useMutate({
    send: (ids: number[]) => api.POST("/api/screens/reorder", { body: { ids } }),
    invalidates: [screensKey],
  })
  return { screens, create, rename, reorder }
}

/**
 * Deleting a rack: the one write here that makes two different lists stale.
 *
 * This is why `invalidates` is a list and not a key. `DELETE /api/daemons/{id}`
 * cascades to that rack's screens, so an interface that re-asked only the first
 * key it was given would go on drawing screens on a rack that is gone -- and
 * would draw them as freshly fetched, not as stale.
 */
function useUnracking() {
  const screens = useQuery({
    queryKey: screensKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/screens")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the screens did not list")
      }
      return data
    },
  })
  const daemons = useQuery({
    queryKey: daemonsKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/daemons")
      if (!data) {
        throw new ApiError(response.status, detailFrom(error) ?? "the racks did not list")
      }
      return data
    },
  })
  const unrack = useMutate({
    send: (id: number) =>
      api.DELETE("/api/daemons/{daemon_id}", { params: { path: { daemon_id: id } } }),
    invalidates: [screensKey, daemonsKey],
  })
  return { screens, daemons, unrack }
}

function mounted<T>(hook: () => T, defaultOptions?: DefaultOptions) {
  // No `defaultOptions` unless a test asks, deliberately. `renderApp`'s blanket
  // `retry: false` would mask what two of these tests measure -- that a write
  // is attempted once and not retried, which for a POST is the difference
  // between one screen and two. The one test that passes any turns retries off
  // for *queries* only, so the mutation is still measured with its real policy.
  const queryClient = new QueryClient(defaultOptions ? { defaultOptions } : undefined)
  const wrapper = ({ children }: { children: ReactNode }) => (
    <StrictMode>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </StrictMode>
  )
  // The client comes back too, because one thing worth asserting is in the
  // cache rather than in a render: rendered state is a macrotask behind, the
  // cache is not.
  return Object.assign(renderHook(hook, { wrapper }), { queryClient })
}

/** The list has arrived and nothing is in flight, so a request count is stable. */
async function quiet(screens: () => { isSuccess: boolean; isFetching: boolean }) {
  await waitFor(() => {
    expect(screens().isSuccess).toBe(true)
    expect(screens().isFetching).toBe(false)
  })
}

afterEach(() => setUnauthorizedHandler(null))

describe("saving something, and being told which rack did not get it", () => {
  it("hands back the racks a create could not reach, which its 201 never says", async () => {
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      // 201, not 202: the row exists and its representation is in the body, and
      // the route declared its own status so nothing narrowed it. The header is
      // the only thing that says rack 15 never got it.
      http.post("/api/screens", () =>
        HttpResponse.json(added, { status: 201, headers: { "X-Unservable-Daemons": "15" } }),
      ),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)

    let saved: Saved<ScreenView> | undefined
    await act(async () => {
      saved = await result.current.create.mutateAsync(newScreen)
    })

    expect(saved?.unservable).toEqual([15])
    // A create's caller needs the row it made -- the wizard goes on to select it.
    expect(saved?.body?.id).toBe(7)
    // And the same answer as rendered state, for a page that shows the notice
    // from the hook rather than from the call. `waitFor` and not a bare read:
    // query-core notifies its observers on a `setTimeout`, so the render is one
    // macrotask behind the resolved promise. Nothing here waits for time to
    // pass -- the condition is already true when the first poll runs.
    await waitFor(() => expect(result.current.create.data?.unservable).toEqual([15]))
    expect(result.current.create.isError).toBe(false)
  })

  it("names nobody when every rack got it, and refetches what the edit changed", async () => {
    let sent: unknown
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(renamedCpu)
      }),
    )
    const { result, queryClient } = mounted(useSaving)
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()
    list.answerWith([clock, renamedCpu])

    let saved: Saved<ScreenView> | undefined
    let cachedWhenSettled: unknown
    await act(async () => {
      saved = await result.current.rename.mutateAsync({ id: 11, name: "gauge" })
      // Read *inside* the act block, on the line after the mutation settled and
      // before anything else has had a turn. Read after the block it would say
      // nothing: `act` flushes the queue on the way out, and a refetch nobody
      // waited for would have finished by then too.
      cachedWhenSettled = queryClient.getQueryData(screensKey)
    })

    expect(saved?.unservable).toEqual([])
    // What was asked for is what went out: the variables reach the caller's
    // `send` rather than the mutation being fired with nothing in it.
    expect(sent).toEqual({ name: "gauge" })
    // Asked again, and asked before the mutation was settled -- this count is
    // read the moment `mutateAsync` resolves, which is what makes it an
    // assertion about `onSuccess` and not about some later render.
    expect(list.asked()).toBe(askedBefore + 1)
    // And *answered* before it settled, not merely asked. The invalidation is
    // returned from `onSettled` and TanStack awaits it, so the refetched rows
    // are in the cache the moment `mutateAsync` resolves. A `useMutate` that
    // fired the invalidation without returning it would settle with "cpu" still
    // cached, and a dialog closing on success would close over the row it just
    // edited.
    expect(cachedWhenSettled).toEqual([clock, renamedCpu])
    // And the new name arrived from the server. No optimistic patch put it
    // there: the handler above answers the PATCH, and the list only says
    // "gauge" because it was fetched again afterwards.
    await waitFor(() => expect(result.current.screens.data?.[1]?.name).toBe("gauge"))
  })

  it("takes a 202 as an edit that landed and reached nobody, not as a failure", async () => {
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", () =>
        HttpResponse.json(renamedCpu, {
          status: 202,
          headers: { "X-Unservable-Daemons": "4" },
        }),
      ),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()
    list.answerWith([clock, renamedCpu])

    let saved: Saved<ScreenView> | undefined
    await act(async () => {
      saved = await result.current.rename.mutateAsync({ id: 11, name: "gauge" })
    })

    // It resolved rather than threw, which is already most of the claim.
    expect(saved?.unservable).toEqual([4])
    await waitFor(() => expect(result.current.rename.isSuccess).toBe(true))
    expect(result.current.rename.isError).toBe(false)
    // Saved is saved: the row changed even though rack 4 is still showing what
    // it was showing, so the list is as stale as after any other edit.
    expect(list.asked()).toBe(askedBefore + 1)
    await waitFor(() => expect(result.current.screens.data?.[1]?.name).toBe("gauge"))
  })

  it("names the racks a 200 missed while the others got it", async () => {
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      // A reorder can name screens on more than one rack, so this is the case
      // 202 cannot describe at all: two of the three racks did not get it and
      // one did, which is a 200 with a header.
      http.post("/api/screens/reorder", () =>
        HttpResponse.json([cpu, clock], { headers: { "X-Unservable-Daemons": "4,15" } }),
      ),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)

    let saved: Saved<ScreenView[]> | undefined
    await act(async () => {
      saved = await result.current.reorder.mutateAsync([11, 12])
    })

    expect(saved?.unservable).toEqual([4, 15])
    await waitFor(() => expect(result.current.reorder.isSuccess).toBe(true))
  })

  it("re-asks every list the write was said to make stale, not just the first", async () => {
    const list = listing([clock, cpu])
    const kept = [rack({ id: 15, name: "hall" }), rack({ id: 9, name: "loft" })]
    const racks = racking([...kept, rack({ id: 4, name: "shed" })])
    server.use(
      list.handler,
      racks.handler,
      // Deleting a rack cascades to its screens in M3a's schema, which is why
      // this one write names two keys. `Deleted` with the default 200 -- no
      // route in this API answers 204.
      http.delete("/api/daemons/:daemon_id", () => HttpResponse.json({ deleted: 1 })),
    )
    const { result } = mounted(useUnracking)
    await quiet(() => result.current.screens)
    await quiet(() => result.current.daemons)
    const screensBefore = list.asked()
    const racksBefore = racks.asked()
    // Rack 4 is gone and cpu went with it, so both lists answer differently now.
    list.answerWith([clock])
    racks.answerWith(kept)

    await act(async () => {
      await result.current.unrack.mutateAsync(4)
    })

    // Both, and this is the assertion the single-key tests cannot make: an
    // implementation that invalidated only `invalidates[0]` passes every one of
    // them and leaves the rack list on screen naming a rack that is deleted.
    expect(list.asked()).toBe(screensBefore + 1)
    expect(racks.asked()).toBe(racksBefore + 1)
    await waitFor(() => {
      expect(result.current.screens.data).toHaveLength(1)
      expect(result.current.daemons.data?.map((one) => one.id)).toEqual([15, 9])
    })
  })

  it("surfaces a refusal, re-asks what it holds, and does not send the write twice", async () => {
    let attempts = 0
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", () => {
        attempts += 1
        return HttpResponse.json(
          { detail: "screen 11 names a bus this rack does not have" },
          { status: 422 },
        )
      }),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()

    await act(async () => {
      await expect(
        result.current.rename.mutateAsync({ id: 11, name: "gauge" }),
      ).rejects.toThrow(/does not have/)
    })

    await waitFor(() => expect(result.current.rename.error).toBeInstanceOf(ApiError))
    expect((result.current.rename.error as ApiError).status).toBe(422)
    // Asked again even though the write was refused. The client does not get to
    // decide that the rollback left the cache exactly as it found it -- that is
    // the server's account of the edit, not this client's, and the rule is that
    // no server state is invented here. So it re-asks, and this count is read
    // the moment `mutateAsync` rejects, which makes it a claim about `onSettled`
    // and not about some later render.
    expect(list.asked()).toBe(askedBefore + 1)
    // A mutation is not a query: a retried PATCH is a second write.
    expect(attempts).toBe(1)
  })

  it("hands back the refusal even when the refetch it triggers fails as well", async () => {
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", () =>
        HttpResponse.json(
          { detail: "screen 11 names a bus this rack does not have" },
          { status: 422 },
        ),
      ),
    )
    // Retries off for *queries only*, so the refetch fails once rather than
    // four times over a backoff. The mutation keeps its real policy, which is
    // what the test above measures.
    const { result } = mounted(useSaving, { queries: { retry: false } })
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()
    // Now the whole server is unhappy: the write is refused *and* the list that
    // the refusal invalidates cannot be fetched either.
    list.refuseWith(503)

    let refusal: unknown
    await act(async () => {
      refusal = await result.current.rename
        .mutateAsync({ id: 11, name: "gauge" })
        .then(() => undefined)
        .catch((error: unknown) => error)
    })

    // The error the caller must see is the one its own write produced. The
    // failing refetch does not replace it, does not swallow it and does not
    // surface as an unhandled rejection: `invalidateQueries` catches a query's
    // rejection, so `onSettled` resolves and the mutation rethrows the 422.
    expect(refusal).toBeInstanceOf(ApiError)
    expect((refusal as ApiError).status).toBe(422)
    expect((refusal as ApiError).message).toMatch(/does not have/)
    // It really was attempted, and it really did fail.
    expect(list.asked()).toBe(askedBefore + 1)
    await waitFor(() => expect(result.current.screens.isError).toBe(true))
    // And the mutation is in error, not in success, after all that.
    expect(result.current.rename.isError).toBe(true)
  })

  it("says something readable when the refusal is a validation report", async () => {
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      // FastAPI's own 422: `detail` is a list of field errors, not a sentence.
      http.patch("/api/screens/:screen_id", () =>
        HttpResponse.json(
          { detail: [{ loc: ["body", "name"], msg: "string too short", type: "value_error" }] },
          { status: 422 },
        ),
      ),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)

    await act(async () => {
      await expect(result.current.rename.mutateAsync({ id: 11, name: "" })).rejects.toThrow()
    })

    await waitFor(() => expect(result.current.rename.error).toBeInstanceOf(ApiError))
    const refusal = result.current.rename.error as ApiError
    expect(refusal.message).toMatch(/refus|not saved/i)
    expect(refusal.message).not.toMatch(/object Object|undefined/)
  })

  it("sends a write with the credentials that carry the session cookie", async () => {
    let credentials: RequestCredentials | undefined
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", ({ request }) => {
        credentials = request.credentials
        return HttpResponse.json(renamedCpu)
      }),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)

    await act(async () => {
      await result.current.rename.mutateAsync({ id: 11, name: "gauge" })
    })

    // What this pins and what it does not: `credentials: "omit"` or `"include"`
    // on the client would fail here, and both are wrong -- the first drops the
    // session cookie, the second would send it cross-site. It cannot notice the
    // option being *deleted*, because "same-origin" is also the fetch standard's
    // default for a `Request` and no observable value distinguishes the two.
    expect(credentials).toBe("same-origin")
  })

  it("lets the one 401 handler see a write refused for a dead session", async () => {
    const refused: string[] = []
    setUnauthorizedHandler((url) => refused.push(url))
    const list = listing([clock, cpu])
    server.use(
      list.handler,
      http.patch("/api/screens/:screen_id", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    )
    const { result } = mounted(useSaving)
    await quiet(() => result.current.screens)

    await act(async () => {
      await expect(result.current.rename.mutateAsync({ id: 11, name: "gauge" })).rejects.toThrow()
    })

    // Through the middleware in `client.ts` and nowhere else: a write that
    // discovers an expired session must not grow a second 401 handler of its
    // own, and it does not have to, because it goes out through the same client.
    expect(refused).toHaveLength(1)
    expect(new URL(refused[0]).pathname).toBe("/api/screens/11")
  })
})
