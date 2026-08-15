import { defineConfig, mergeConfig } from "vitest/config"
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
    },
  }),
)
