import { createContext, useContext, useEffect, useState } from "react"

type Theme = "dark" | "light" | "system"

/**
 * What the interface starts as, and the key it remembers a change under.
 *
 * Constants here rather than optional props defaulted here and passed there.
 * They used to be `defaultTheme = "system"` and `storageKey = "vite-ui-theme"`,
 * and all four call sites -- `main.tsx` and three test harnesses -- passed the
 * same two values, which is an option no mutant can see. Worse: `main.tsx` is
 * imported by no test and no e2e spec, and the harnesses restated the literals
 * rather than importing them, so each was asserting its own copy. A `main.tsx`
 * mutated to `defaultTheme="light" storageKey="wrong-key"` passed all 159
 * tests, and the interface it describes starts light with every stored theme
 * orphaned.
 *
 * With one place for each value, `shell.test.tsx`'s "starts dark" and its
 * `localStorage.getItem("ors-theme")` are pins rather than restatements. Those
 * two literals in that file are deliberately literals: reading them from here
 * would assert only that the constant equals itself.
 *
 * Dark because that is what the design chose -- this hangs in a rack room. The
 * key is namespaced because `vite-ui-theme` is the scaffold's, and any other
 * Vite app served from this origin would share it.
 */
export const DEFAULT_THEME: Theme = "dark"
export const THEME_STORAGE_KEY = "ors-theme"

type ThemeProviderProps = {
  children: React.ReactNode
}

type ThemeProviderState = {
  theme: Theme
  setTheme: (theme: Theme) => void
}

const initialState: ThemeProviderState = {
  theme: "system",
  setTheme: () => null,
}

const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

export function ThemeProvider({ children }: ThemeProviderProps) {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem(THEME_STORAGE_KEY) as Theme) || DEFAULT_THEME
  )

  useEffect(() => {
    const root = window.document.documentElement
    root.classList.remove("light", "dark")

    if (theme === "system") {
      const systemTheme = window.matchMedia("(prefers-color-scheme: dark)")
        .matches
        ? "dark"
        : "light"
      root.classList.add(systemTheme)
      return
    }

    root.classList.add(theme)
  }, [theme])

  const value = {
    theme,
    setTheme: (theme: Theme) => {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
      setTheme(theme)
    },
  }

  return (
    <ThemeProviderContext.Provider value={value}>{children}</ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (context === undefined)
    throw new Error("useTheme must be used within a ThemeProvider")

  return context
}
