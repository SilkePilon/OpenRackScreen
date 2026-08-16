import { useState } from "react"
import { useNavigate } from "react-router"

import { ApiError } from "@/api/client"
import { useLogin } from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

// The server rate-limits sign-ins per client on a rolling 60-second window and
// refuses with 429 *before* it looks at the password. Reporting that as a wrong
// password would be a lie the user acts on -- they would go and change a
// password that was right, or type the right one faster and keep the window
// open. So the two refusals read as the different things they are.
function refusal(error: Error) {
  const status = error instanceof ApiError ? error.status : 0
  if (status === 429) {
    return {
      title: "Too many attempts",
      body: "The server is refusing sign-ins from here for a minute. Wait, then try again — the password you have may well be right.",
    }
  }
  if (status === 401) {
    return { title: "That did not work", body: "Wrong password." }
  }
  return { title: "That did not work", body: error.message }
}

/** There is a password; this exchanges it for a session cookie. */
export function LoginPage() {
  const [password, setPassword] = useState("")
  const login = useLogin()
  const navigate = useNavigate()

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    login.mutate(password, {
      onSuccess: () => void navigate("/daemons", { replace: true }),
    })
  }

  const refused = login.isError ? refusal(login.error) : null

  return (
    <main className="flex min-h-svh flex-col items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Sign in</h1>
          </CardTitle>
          <CardDescription>OpenRackScreen</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            {refused && (
              <Alert variant="destructive">
                <AlertTitle>{refused.title}</AlertTitle>
                <AlertDescription>{refused.body}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={password.length === 0 || login.isPending}>
              Sign in
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
