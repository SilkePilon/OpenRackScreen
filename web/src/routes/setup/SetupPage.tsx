import { useState } from "react"
import { Link, useNavigate } from "react-router"

import { ApiError } from "@/api/client"
import { useClaimPassword } from "@/api/queries"
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

/** First run: there is no admin password yet, and this claims it. */
export function SetupPage() {
  const [password, setPassword] = useState("")
  const claim = useClaimPassword()
  const navigate = useNavigate()

  // Setting the password does not sign anyone in -- the server issues a session
  // from /login and nowhere else -- so this goes on to the login page.
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    claim.mutate(password, { onSuccess: () => void navigate("/login", { replace: true }) })
  }

  const alreadyClaimed = claim.error instanceof ApiError && claim.error.status === 409

  return (
    <main className="flex min-h-svh flex-col items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Set a password</h1>
          </CardTitle>
          <CardDescription>
            This rack has no admin password yet. Whoever sets it first keeps it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            {claim.isError && (
              <Alert variant="destructive">
                <AlertTitle>
                  {alreadyClaimed ? "Somebody got here first" : "The password was not set"}
                </AlertTitle>
                <AlertDescription>
                  {claim.error.message}
                  {alreadyClaimed && (
                    <>
                      {" "}
                      <Link to="/login">Sign in instead.</Link>
                    </>
                  )}
                </AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={password.length === 0 || claim.isPending}>
              Set password
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
