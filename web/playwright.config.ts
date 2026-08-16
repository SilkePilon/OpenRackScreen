import { defineConfig } from "@playwright/test"

/**
 * The end-to-end run: one browser, one server, one rack, seven specs in order.
 *
 * **No `webServer` block, on purpose.** Playwright's own server manager starts
 * a process before the run and stops it after, and two of these specs need the
 * server *stopped in the middle* -- that is the whole of what they assert. So
 * the processes belong to `e2e/fixture.ts`, which can start, stop and restart
 * them and can prove afterwards that neither is still running.
 *
 * **`workers: 1` and `fullyParallel: false`**, because the seven specs are
 * seven steps of one story against one database: a password is set, a rack is
 * paired, a screen is added, and the last three do things to the server that a
 * spec running beside them would see. `rack.spec.ts` declares itself serial as
 * well, so the ordering is stated where it is relied on rather than only here.
 *
 * **No retries.** A retry would re-run the story from the beginning against a
 * fresh temp directory, which would work -- and would turn a flake into a green
 * run, which is exactly the thing this layer exists to refuse. A spec that
 * needs a second attempt is a defect in the spec or in the product, and either
 * way it should be read rather than retried.
 */
export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: [["list"]],
  /**
   * Per spec. The long one is the wizard: a probe holds the panel lit for the
   * daemon's full `PROBE_HOLD_BUDGET` of five seconds and the reply comes when
   * the bus is let go, and the interface's socket backs off for up to a few
   * seconds after the server it was talking to goes away. Everything here waits
   * on a condition, so a passing run spends a fraction of this.
   */
  timeout: 120_000,
  expect: { timeout: 20_000 },
  // No `use.baseURL`: the origin is a port the kernel hands out at run time,
  // and only the fixture knows it. An end-to-end run that named 8080 here would
  // collide with the developer's own server on the machine it is most likely to
  // be run on.
  //
  // No `use.trace` either, and that is not an omission: these specs share one
  // page built from `browser.newContext()`, which does not read the `use`
  // block, so a `trace` setting here would be a promise nothing keeps. The
  // fixture starts the tracing itself and writes it to `test-results/`.
})
