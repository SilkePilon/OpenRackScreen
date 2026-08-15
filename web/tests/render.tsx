import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render } from "@testing-library/react"
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
    <QueryClientProvider client={queryClient}>
      <ThemeProvider defaultTheme="dark" storageKey="ors-theme">
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>,
  )

  return {
    ...view,
    queryClient,
    /** History entries added since this render. A `replace` adds none. */
    pushes: () => window.history.length - entriesBefore,
    path: () => window.location.pathname,
  }
}
