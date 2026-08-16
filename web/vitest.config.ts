import { configDefaults, defineConfig, mergeConfig } from "vitest/config"
import viteConfig from "./vite.config.ts"

// Merged rather than standalone: a bare `vitest.config.ts` *replaces*
// `vite.config.ts`, so the react plugin and the `@` alias would have to be kept
// in sync by hand and a later `define` or plugin would reach the build without
// reaching the tests.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      // Testing Library only registers its auto-cleanup when the globals are
      // present; without it every render in a file stacks up in one document.
      globals: true,
      environment: "jsdom",
      setupFiles: ["./tests/setup.ts"],
      css: false,
      /**
       * `e2e/` is Playwright's, and vitest must not touch it.
       *
       * Not tidiness -- it happened. Vitest's default `include` is
       * `**\/*.{test,spec}.?(c|m)[jt]s?(x)`, so `e2e/rack.spec.ts` matched it
       * on the day it was written: vitest loaded a file that imports
       * `@playwright/test`, ran it under jsdom, and `pnpm test` went red with
       * three DOM exceptions about a canvas. The two runners are different
       * runners, and `pnpm test` must stay the fast one that boots no servers
       * -- a `pnpm test` that starts a rack is a `pnpm test` nobody runs. The
       * end-to-end run has its own command, `pnpm test:e2e`.
       *
       * Spread from `configDefaults.exclude` rather than written out, because
       * assigning this key *replaces* the defaults, and dropping
       * `node_modules` and `dist` from them would put every dependency's own
       * tests back in the run.
       */
      exclude: [...configDefaults.exclude, "e2e/**"],
    },
  }),
)
