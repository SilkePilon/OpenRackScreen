import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render } from "@testing-library/react"
import { StrictMode } from "react"
import { BrowserRouter } from "react-router"

import App from "../src/App"
import { ThemeProvider } from "../src/theme/theme-provider"

/**
 * Mount the whole interface at `at`, in the providers `main.tsx` mounts it in.
 *
 * `BrowserRouter` over jsdom's history rather than a `MemoryRouter`, for one
 * reason: a memory history keeps its stack to itself, and two `navigate` calls
 * in the same tick render once, so a redirect that fired twice is
 * indistinguishable from one that fired once by anything a component can see.
 * jsdom counts entries, `pushes()` reads them, and "returns to login *once*"
 * becomes something a test can fail. It is also what runs in the browser.
 *
 * `StrictMode` because `main.tsx` has it: every effect is mounted, torn down and
 * mounted again, which is the only way a test sees the cleanup a later task's
 * subscription (the /ws/ui socket) will live or die by. Nothing here may depend
 * on effects running once.
 *
 * The QueryClient is built fresh per render so no result an earlier test cached
 * can decide what this one sees first.
 */
export function renderApp({ at = "/" }: { at?: string } = {}) {
  // `replaceState`, not `pushState`: arriving at the route under test is not
  // itself a navigation, and it must not be counted as one.
  window.history.replaceState(null, "", at)
  const entriesBefore = window.history.length

  const queryClient = new QueryClient({
    // A stubbed failure has to be a failure now, not three attempts and a
    // backoff from now.
    defaultOptions: { queries: { retry: false } },
  })
  const view = render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider defaultTheme="dark" storageKey="ors-theme">
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </StrictMode>,
  )

  return {
    ...view,
    queryClient,
    /**
     * History entries added since this render. A `replace` adds none.
     *
     * Read it only in tests that go **forward**. `window.history` is jsdom's
     * one history, shared by every test in the file, and `renderApp` only
     * `replaceState`s -- so entries pushed by earlier tests are still on the
     * stack, ahead of the current position. A `pushState` truncates whatever
     * is ahead before appending, so with forward entries present the length
     * can stay the same, or fall, while a navigation really happened. Any test
     * that calls `history.back()` (a popstate round-trip, which the two
     * disclosed 401-cache survivors would need) must not measure with this.
     */
    pushes: () => window.history.length - entriesBefore,
    path: () => window.location.pathname,
  }
}
