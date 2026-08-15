import type { ReactNode } from "react"
import { Navigate } from "react-router"

import { useSession } from "@/api/queries"
import { Button } from "@/components/ui/button"

/**
 * The guard. Nothing behind it renders until the server has said which of the
 * three states it is in.
 *
 * The server distinguishes "no password has ever been set" from "there is a
 * password and this browser has no session", so this does not have to guess:
 * a rack nobody has configured goes to /setup, where it can be claimed, and a
 * configured one goes to /login. Collapsing those two into one redirect is how
 * a fresh rack becomes unreachable -- it would be sent to a login page whose
 * password does not exist yet.
 */
export function RequireSession({ children }: { children: ReactNode }) {
  const session = useSession()

  if (session.isPending) {
    return (
      <p className="p-6 text-sm text-muted-foreground">Checking the session…</p>
    )
  }

  if (session.isError) {
    // Not a redirect: which redirect would be right is exactly what is unknown.
    return (
      <div className="flex min-h-svh flex-col items-center justify-center gap-4 p-6">
        <p className="text-sm text-muted-foreground">
          The server did not answer. It may be restarting.
        </p>
        <Button variant="outline" onClick={() => void session.refetch()}>
          Try again
        </Button>
      </div>
    )
  }

  if (!session.data.password_set) return <Navigate to="/setup" replace />
  if (!session.data.authenticated) return <Navigate to="/login" replace />

  return <>{children}</>
}
