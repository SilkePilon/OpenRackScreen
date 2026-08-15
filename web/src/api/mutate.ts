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
  /**
   * What the route answered with, or `undefined` if there was no body to read.
   *
   * Not a 204 case: no M3a route answers 204, `DELETE` included -- it answers
   * `{"deleted": n}` with the default 200. It is optional because that is what
   * openapi-fetch hands back when a *successful* response has no parseable
   * body of the declared content type: a proxy's HTML under a 200, a truncated
   * response, a route that grows an empty body later. A caller that reads a
   * field off it without checking is one deployment quirk from a TypeError, so
   * the type makes it check.
   */
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
 *    are invalidated when the mutation settles -- either way, saved or refused
 *    -- and the answer comes back from the server.
 * 3. **Turn a refusal into an error** carrying the server's own sentence, so a
 *    page can render it rather than inventing one.
 *
 * What it deliberately does *not* decide: which queries an edit makes stale.
 * That is the caller's, because only the caller knows -- a screen edit touches
 * the screen list, a pairing touches the daemons, a template assignment touches
 * both -- and a rule guessed here would be wrong on the page nobody checked.
 *
 * The invalidation is returned from `onSettled`, which TanStack Query awaits on
 * both paths, so the mutation is not settled until the refetch is done and a
 * caller closing a dialog on success closes it over fresh rows. A refetch that
 * fails changes nothing about the mutation's own outcome: `invalidateQueries`
 * swallows a query's rejection, so a saved edit stays saved and a refusal
 * reaches the caller as the `ApiError` it was, not as whatever the refetch hit.
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
    // On settle, so a refusal invalidates too, which is what the spec's failure
    // table says a failed mutation does. The rule underneath it is that no
    // server state is invented by the client: a refusal does roll the whole
    // edit back -- the `change` context manager sees to that -- but "the whole
    // edit" is the server's account of what happened, and treating the cache as
    // already correct because of it is the client deciding what the server
    // holds. So it re-asks instead of assuming, and what it draws afterwards is
    // what the server actually has. A page that must not lose what the user is
    // typing keeps that in form state, which no refetch can reach.
    onSettled: () =>
      Promise.all(invalidates.map((queryKey) => queryClient.invalidateQueries({ queryKey }))),
  })
}
