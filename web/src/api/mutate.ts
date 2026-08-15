import {
  useMutation,
  useQueryClient,
  type QueryKey,
  type UseMutationResult,
} from "@tanstack/react-query"

import { ApiError, detailFrom } from "./client"
import { parseUnservable } from "./unservable"

/**
 * What one call to `api.GET`/`POST`/`PATCH`/`DELETE` answers with.
 *
 * Written structurally rather than imported from openapi-fetch, because what
 * this module needs is the three parts it reads and nothing else: the parsed
 * body, whatever the server said when it refused, and the response itself --
 * which is where the header lives, and the only place it lives.
 */
export type Sent<Body> = {
  data?: Body | undefined
  error?: unknown
  response: Response
}

/** An edit that landed, and the racks that have not been given it. */
export type Saved<Body> = {
  /** What the route answered with. `undefined` for the routes that answer 204. */
  body: Body | undefined
  /**
   * The racks this edit could not be pushed to, ascending, from the header.
   *
   * Empty means every affected rack got it. It does not mean there is nothing
   * wrong with a rack: `config_error` on `GET /api/daemons` is where a rack
   * says it cannot be given a configuration, and that changes without anybody
   * editing anything.
   */
  unservable: number[]
}

/**
 * A write, the queries it makes stale, and the racks it did not reach.
 *
 * Three things every mutating page in this interface has to do, in one place
 * because each of them is a rule that is broken by being forgotten:
 *
 * 1. **Read the header.** `X-Unservable-Daemons` is on 200, 201 and 202 alike,
 *    and the status code cannot stand in for it: `POST /api/screens` answers
 *    201 even when no rack got the new screen, and an edit that reached three
 *    racks out of four has no status code left to say so. 202 is only ever
 *    narrowed from the *default* status, and only when not one affected rack
 *    got the edit -- so it is neither necessary nor sufficient, and this reads
 *    the header on every success instead.
 * 2. **Refetch rather than patch.** There are no optimistic updates here and
 *    there must not be: the server can accept an edit, save it, and never push
 *    it, and a patched cache would draw that as applied. The affected queries
 *    are invalidated on success and the answer comes back from the server.
 * 3. **Turn a refusal into an error** carrying the server's own sentence, so a
 *    page can render it rather than inventing one.
 *
 * What it deliberately does *not* decide: which queries an edit makes stale.
 * That is the caller's, because only the caller knows -- a screen edit touches
 * the screen list, a pairing touches the daemons, a template assignment touches
 * both -- and a rule guessed here would be wrong on the page nobody checked.
 *
 * The invalidation is returned from `onSuccess`, which TanStack Query awaits,
 * so the mutation is not settled until the refetch is done and a caller closing
 * a dialog on success closes it over fresh rows. A refetch that fails does not
 * fail the mutation: `invalidateQueries` swallows it, and it should -- the edit
 * really was saved.
 */
export function useMutate<Body, Variables = void>({
  send,
  invalidates,
}: {
  send: (variables: Variables) => Promise<Sent<Body>>
  invalidates: readonly QueryKey[]
}): UseMutationResult<Saved<Body>, Error, Variables> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (variables: Variables): Promise<Saved<Body>> => {
      const { data, error, response } = await send(variables)
      // `response.ok` rather than the presence of `error`: a 500 from something
      // in front of the server has no `{detail}` in it to parse, and reading
      // success from a body that failed to parse is how a refusal becomes a
      // "saved". 202 is `ok`, which is the whole point -- it is an edit that
      // landed.
      if (!response.ok) {
        throw new ApiError(response.status, detailFrom(error) ?? "the server refused the change")
      }
      return { body: data, unservable: parseUnservable(response.headers) }
    },
    // Only on success. A refusal rolls the whole edit back on the server -- the
    // `change` context manager sees to that -- so there is nothing stale to
    // refetch, and refetching would pull the form the user is still reading the
    // refusal on out from under them.
    onSuccess: () =>
      Promise.all(invalidates.map((queryKey) => queryClient.invalidateQueries({ queryKey }))),
  })
}
