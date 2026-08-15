import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api, detailFrom } from "./client"

/** One key per resource. Everything that touches the session invalidates this. */
export const sessionKey = ["session"] as const

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
