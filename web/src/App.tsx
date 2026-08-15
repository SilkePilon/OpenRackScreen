import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router"

import { setUnauthorizedHandler } from "@/api/client"
import { sessionKey } from "@/api/queries"
import { AppShell } from "@/components/AppShell"
import { RequireSession } from "@/routes/RequireSession"
import { LoginPage } from "@/routes/login/LoginPage"
import { SetupPage } from "@/routes/setup/SetupPage"

// The pages themselves arrive in later tasks; the routes exist now so the guard
// has something to guard and the sidebar has somewhere to link.
function Placeholder({ title }: { title: string }) {
  return (
    <>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="text-muted-foreground">This page arrives in a later task.</p>
    </>
  )
}

function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  // Refs, not state: the handler below is called from a fetch, outside any
  // render, and has to read what is true *now* -- and setting either of these
  // must not re-render the whole route tree.
  const atLogin = useRef(location.pathname === "/login")
  const returning = useRef(false)

  useEffect(() => {
    atLogin.current = location.pathname === "/login"
    // Arriving at /login re-arms it: the next session to expire has somewhere
    // to be sent again.
    if (atLogin.current) returning.current = false
  }, [location.pathname])

  useEffect(() => {
    // One 401 handler for the whole interface, mounted above the routes so it
    // outlives every navigation. Every session-guarded route answers 401 once
    // the cookie is gone, and a page that loads three things loses the session
    // three times at once.
    setUnauthorizedHandler(() => {
      // Already here. A 401 from the login route is a wrong password, not an
      // expired session, and sending it to /login again would only stack up a
      // history entry per attempt.
      if (atLogin.current) return
      // Already on the way. Without this, concurrent 401s each push an entry
      // and the back button walks through them one refused request at a time.
      if (returning.current) return
      returning.current = true
      // Cleared, not invalidated: what is cached says there is a session, and
      // that is the thing that just turned out to be false.
      queryClient.removeQueries({ queryKey: sessionKey })
      navigate("/login")
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate, queryClient])

  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireSession>
            <AppShell>
              <Outlet />
            </AppShell>
          </RequireSession>
        }
      >
        <Route index element={<Navigate to="/daemons" replace />} />
        <Route path="/daemons" element={<Placeholder title="Daemons" />} />
        <Route path="/screens" element={<Placeholder title="Screens" />} />
        <Route path="/templates" element={<Placeholder title="Templates" />} />
        <Route path="/integrations" element={<Placeholder title="Integrations" />} />
        <Route path="/settings" element={<Placeholder title="Settings" />} />
        <Route path="*" element={<Placeholder title="No such page" />} />
      </Route>
    </Routes>
  )
}

export default App
