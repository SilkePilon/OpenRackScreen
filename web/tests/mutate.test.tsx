import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import { http, HttpResponse } from "msw"
import { StrictMode, type ReactNode } from "react"
import { afterEach, describe, expect, it } from "vitest"

import { ApiError, api, detailFrom, setUnauthorizedHandler } from "../src/api/client"
import { type Saved, useMutate } from "../src/api/mutate"
import type { components } from "../src/api/schema"
import { server } from "./msw"

type ScreenView = components["schemas"]["ScreenView"]

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
  return {
    handler: http.get("/api/screens", () => {
      asked += 1
      return HttpResponse.json(listed)
    }),
    asked: () => asked,
    answerWith: (next: ScreenView[]) => {
      listed = next
    },
  }
}

const screensKey = ["screens"] as const

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

function mounted() {
  // No `defaultOptions` at all, deliberately. `renderApp`'s blanket
  // `retry: false` would mask what two of these tests measure -- that a write
  // is attempted once and not retried, which for a POST is the difference
  // between one screen and two.
  const queryClient = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <StrictMode>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </StrictMode>
  )
  return renderHook(useSaving, { wrapper })
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
    const { result } = mounted()
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
    const { result } = mounted()
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()
    list.answerWith([clock, renamedCpu])

    let saved: Saved<ScreenView> | undefined
    await act(async () => {
      saved = await result.current.rename.mutateAsync({ id: 11, name: "gauge" })
    })

    expect(saved?.unservable).toEqual([])
    // What was asked for is what went out: the variables reach the caller's
    // `send` rather than the mutation being fired with nothing in it.
    expect(sent).toEqual({ name: "gauge" })
    // Asked again, and asked before the mutation was settled -- this count is
    // read the moment `mutateAsync` resolves, which is what makes it an
    // assertion about `onSuccess` and not about some later render.
    expect(list.asked()).toBe(askedBefore + 1)
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
    const { result } = mounted()
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
    const { result } = mounted()
    await quiet(() => result.current.screens)

    let saved: Saved<ScreenView[]> | undefined
    await act(async () => {
      saved = await result.current.reorder.mutateAsync([11, 12])
    })

    expect(saved?.unservable).toEqual([4, 15])
    await waitFor(() => expect(result.current.reorder.isSuccess).toBe(true))
  })

  it("surfaces a refusal, refetches nothing, and does not send the write twice", async () => {
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
    const { result } = mounted()
    await quiet(() => result.current.screens)
    const askedBefore = list.asked()

    await act(async () => {
      await expect(
        result.current.rename.mutateAsync({ id: 11, name: "gauge" }),
      ).rejects.toThrow(/does not have/)
    })

    await waitFor(() => expect(result.current.rename.error).toBeInstanceOf(ApiError))
    expect((result.current.rename.error as ApiError).status).toBe(422)
    // Nothing was written, so nothing in the cache is out of date -- and a
    // refetch here would replace what the user is looking at, and their unsaved
    // edit with it, on the one screen where they need to read the refusal.
    expect(list.asked()).toBe(askedBefore)
    // A mutation is not a query: a retried PATCH is a second write.
    expect(attempts).toBe(1)
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
    const { result } = mounted()
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
    const { result } = mounted()
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
    const { result } = mounted()
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
