/**
 * A real server, a real rack, and the proof that neither is left behind.
 *
 * This is the only file in the repository that starts `ors-server` and
 * `ors-daemon` as processes. Everything the seven specs assert is asserted
 * against those two, through a browser, over HTTP and a WebSocket -- which is
 * the point of the layer: the base64 trap that shipped in M3a passed a green
 * Python test *and* a green component test, because both ends of it were
 * stubbed by the same assumption. Only a real server talking to a real browser
 * has no assumption to share.
 *
 * **Nothing here binds a fixed port.** The port is taken from the kernel and
 * handed to the server, to the interface (as the origin the browser loads) and
 * to the daemon (as the URL it pairs with), so two runs on one machine -- or a
 * developer with something already on 8080 -- cannot collide.
 *
 * **Nothing here sleeps to wait for time to pass.** Every wait is a condition
 * with a deadline: a health check that answers, a rack that reports itself
 * online, a process that has exited. A spec that needed a sleep would be a spec
 * that flakes in CI on a slower machine.
 *
 * **Teardown is verified, and it fails loudly.** A leftover process corrupted
 * twenty minutes of another task's test runs in M3a: a daemon still holding a
 * pairing, still writing PNGs, still dialling a port the next run then bound.
 * So `stop` does not merely send a signal -- it waits for the exit, then asks
 * the operating system whether that pid is gone, and then asks the port whether
 * anything still answers on it. Any of the three failing throws, and a throw in
 * a fixture's teardown fails the run.
 */

import { execFileSync, spawn, type ChildProcess } from "node:child_process"
import { randomBytes } from "node:crypto"
import { existsSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs"
import { createServer } from "node:net"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { request, test as base, type APIRequestContext, type Page } from "@playwright/test"

const HERE = dirname(fileURLToPath(import.meta.url))
/** `web/`, and its parent, which is the checkout. */
const WEB = resolve(HERE, "..")
const REPO = resolve(WEB, "..")

/**
 * The interpreter both processes run under: the checkout's own virtual
 * environment, where `ors-server` and `ors-daemon` are installed as editable
 * packages. Resolved rather than assumed, and a missing one is said in one
 * sentence -- a spawn of a path that does not exist is an `ENOENT` naming a
 * file, which tells nobody that `uv sync` has not been run.
 */
const VENV = join(REPO, ".venv", "bin")

/**
 * The password the whole narrative uses. Set through the interface in the first
 * spec, and used by this fixture to open an API session of its own for the
 * arranging that no page does.
 */
export const PASSWORD = "a-password-for-the-rack"

/** The rack the daemon runs, and the one that never connects. See `arrange`. */
export const RACK = "pi-cellar"
export const SPARE_RACK = "pi-spare"

/**
 * How long the server may take to answer its first health check.
 *
 * Generous, and spent only by a failure: a server that is coming up answers in
 * well under a second, and the whole of this budget is what a loaded CI machine
 * importing FastAPI, pydantic and the render engine is allowed.
 */
const START_BUDGET_MS = 30_000

/**
 * How long a process may take to leave after SIGTERM.
 *
 * The daemon's own `SHUTDOWN_BUDGET` is ten seconds and it joins its link and
 * frame pump afterwards, so this is that plus room. Past it, the process is
 * killed *and the run fails*: a rack that would not stop is the thing this
 * fixture exists to notice, not something to paper over with a SIGKILL and a
 * green tick.
 */
const STOP_BUDGET_MS = 20_000

/** One managed child process, with its output kept for when something fails. */
class Managed {
  readonly label: string
  readonly pid: number
  private readonly child: ChildProcess
  private readonly output: string[] = []
  private gone = false
  private readonly departure: Promise<void>

  constructor(label: string, command: string, args: string[], environment: NodeJS.ProcessEnv) {
    this.label = label
    // No `shell: true`, deliberately: a shell would be the process this fixture
    // holds the pid of, and the server would be its child -- so a SIGTERM here
    // would kill the shell, leave the server running, and every check below
    // would pass while the port stayed bound.
    const child = spawn(command, args, {
      cwd: REPO,
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    })
    if (child.pid === undefined) {
      throw new Error(`${label}: could not be started (${command})`)
    }
    this.child = child
    this.pid = child.pid
    const keep = (chunk: Buffer) => {
      this.output.push(chunk.toString())
      // Bounded, because the daemon logs a line per applied configuration and
      // the server one per request; a run that hangs must not eat the machine.
      if (this.output.length > 500) this.output.splice(0, this.output.length - 500)
    }
    child.stdout.on("data", keep)
    child.stderr.on("data", keep)
    this.departure = new Promise<void>((done) => {
      child.on("exit", () => {
        this.gone = true
        done()
      })
    })
    child.on("error", (error) => {
      this.output.push(`${label}: ${error.message}\n`)
    })
  }

  get hasExited(): boolean {
    return this.gone
  }

  /** Everything this process has said, for a failure message that explains itself. */
  log(): string {
    return this.output.join("")
  }

  async waitForExit(budgetMs: number): Promise<boolean> {
    if (this.gone) return true
    let timer: ReturnType<typeof setTimeout> | undefined
    const expiry = new Promise<void>((done) => {
      timer = setTimeout(done, budgetMs)
    })
    await Promise.race([this.departure, expiry])
    if (timer !== undefined) clearTimeout(timer)
    return this.gone
  }

  signal(name: NodeJS.Signals): void {
    if (!this.gone) this.child.kill(name)
  }
}

/**
 * Stop a process, and prove it is gone rather than assume it.
 *
 * Three separate facts, in order, because each can be true while the next is
 * false: the child reported an exit, node has reaped it so the pid is no longer
 * addressable, and -- for the server -- nothing answers on its port. The middle
 * one is the one that catches a `shell: true` mistake or a double-forking
 * daemon, and `ESRCH` from `kill(pid, 0)` is the only answer accepted as proof.
 */
async function stopAndProveGone(managed: Managed): Promise<void> {
  managed.signal("SIGTERM")
  const left = await managed.waitForExit(STOP_BUDGET_MS)
  if (!left) {
    managed.signal("SIGKILL")
    await managed.waitForExit(5_000)
    throw new Error(
      `${managed.label} (pid ${managed.pid}) ignored SIGTERM for ${STOP_BUDGET_MS} ms and had ` +
        `to be killed. Its output was:\n${managed.log()}`,
    )
  }
  try {
    process.kill(managed.pid, 0)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ESRCH") return
    throw new Error(
      `${managed.label} (pid ${managed.pid}) could not be checked after it exited: ` +
        `${String(error)}`,
    )
  }
  throw new Error(
    `${managed.label} (pid ${managed.pid}) reported an exit and is still a process on this ` +
      `machine. Something is holding it open.`,
  )
}

/** A port the kernel says is free, right now. Never a number chosen by hand. */
async function ephemeralPort(): Promise<number> {
  return await new Promise<number>((done, fail) => {
    const probe = createServer()
    probe.on("error", fail)
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address()
      if (address === null || typeof address === "string") {
        probe.close()
        fail(new Error("the kernel gave no port"))
        return
      }
      const { port } = address
      probe.close(() => done(port))
    })
  })
}

/** Poll a condition until it holds, or say what was still false when time ran out. */
async function until(what: string, budgetMs: number, holds: () => Promise<boolean>): Promise<void> {
  const deadline = Date.now() + budgetMs
  for (;;) {
    if (await holds()) return
    if (Date.now() > deadline) throw new Error(`${what}: still not true after ${budgetMs} ms`)
    // A poll interval, not a wait for time to pass: the condition is asked
    // again as soon as this returns, and a fast machine spends none of it.
    await new Promise((tick) => setTimeout(tick, 100))
  }
}

async function answers(origin: string): Promise<boolean> {
  try {
    const response = await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(2_000) })
    return response.ok
  } catch {
    return false
  }
}

export type Rack = {
  /** Where the server is, and therefore where the interface is. */
  readonly origin: string
  /** Where the virtual panels write their PNGs. */
  readonly panelDir: string
  /**
   * Start the server, and return once it answers. Restartable, on the same
   * port, the same data directory and the same secret -- which is what specs 6
   * and 7 are made of.
   */
  startServer(): Promise<void>
  /** Stop it, and prove it is gone and that its port answers nothing. */
  stopServer(): Promise<void>
  /** Pair and run a real daemon with the token the interface just minted. */
  startDaemon(token: string): Promise<void>
  /** An API session of the fixture's own, for arranging what no page arranges. */
  api(): Promise<APIRequestContext>
  /**
   * The world the specs assert against: a second rack that never connects,
   * carrying two screens, and the night window turned off.
   */
  arrange(): Promise<void>
  /** When a virtual panel's PNG was last written, or null if it never was. */
  panelWrittenAt(screenName: string): number | null
  /** What the daemon has said, for a failure that would otherwise be a mystery. */
  daemonLog(): string
  serverLog(): string
}

type Fixtures = {
  rack: Rack
  /**
   * One page for the whole narrative.
   *
   * Worker-scoped rather than per-test, and that is not a convenience: spec 1
   * signs in and spec 7 asserts the interface recovered **without a reload**,
   * which is a claim about one document surviving all seven. A fresh page per
   * spec would sign the session out from under the story and would make "did
   * not reload" unaskable.
   */
  ui: Page
}

/**
 * No test-scoped fixtures of our own: both of these are worker-scoped, so the
 * first type argument is empty. Written as a named type rather than `{}`
 * because a bare `{}` is a type that means "anything but null", which is not
 * what is meant here and is what the lint rule against it is for.
 */
type NoPerTestFixtures = Record<never, never>

export const test = base.extend<NoPerTestFixtures, Fixtures>({
  rack: [
    // Playwright reads a fixture's dependencies out of its first parameter and
    // requires that parameter to be a destructuring pattern -- it throws
    // "First argument must use the object destructuring pattern" for anything
    // else -- so a fixture that depends on nothing has to be written with an
    // empty one. The rule is right in general and cannot be satisfied here.
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const port = await ephemeralPort()
      const origin = `http://127.0.0.1:${port}`
      const root = mkdtempSync(join(tmpdir(), "ors-e2e-"))
      const dataDir = join(root, "data")
      const spiRoot = join(root, "dev")
      const panelDir = join(root, "panels")
      const linkPath = join(root, "link.json")
      const configPath = join(root, "rack.yaml")
      for (const directory of [dataDir, spiRoot, panelDir]) mkdirSync(directory, { recursive: true })

      // Two devices, so that choosing the right one in the wizard is a choice
      // and not the only option -- and neither of them is SPI0.0, whose two
      // numbers are `DisplayConfig`'s own defaults and would therefore be
      // indistinguishable from a field nobody set. `enumerate_panels` matches
      // on the name, so an empty file is a device as far as detection is
      // concerned; nothing opens these.
      for (const device of ["spidev2.0", "spidev3.1"]) writeFileSync(join(spiRoot, device), "")

      // A rack with no screens: valid, and what a Pi that has been paired but
      // never pushed to runs from. Everything on the glass afterwards comes
      // from the server's snapshot, which is what the specs are about.
      writeFileSync(configPath, "version: 1\ntimezone: UTC\nscreens: []\n")

      const python = join(VENV, "python")
      const serverBin = join(VENV, "ors-server")
      for (const binary of [python, serverBin]) {
        if (!existsSync(binary)) {
          throw new Error(`${binary} is missing. Run \`uv sync\` in ${REPO} first.`)
        }
      }

      // Built before the server is started, because `ORS_WEB_DIR` is read at
      // startup and a server pointed at a directory with no build in it serves
      // the API alone -- which is a run where every spec fails on a blank page
      // for a reason none of them names.
      const builtAt = Date.now()
      execFileSync("pnpm", ["build"], { cwd: WEB, stdio: "pipe" })
      const webDir = join(WEB, "dist")
      if (!existsSync(join(webDir, "index.html"))) {
        throw new Error(`pnpm build left no ${join(webDir, "index.html")}`)
      }
      process.stdout.write(`  [fixture] pnpm build: ${Date.now() - builtAt} ms\n`)

      const serverEnv: NodeJS.ProcessEnv = {
        ...process.env,
        ORS_DATA_DIR: dataDir,
        // Minted once per run and then **fixed across restarts**, which is the
        // load-bearing half: the secret signs the session cookie, so a server
        // that generated a new one when spec 7 restarted it would sign the
        // browser out, and "it recovered without a reload" would be untestable
        // for a reason that has nothing to do with the socket.
        //
        // A Fernet key, and the encoding is not a detail: `load_or_create_key`
        // runs it through `urlsafe_b64decode`, which needs the URL-safe
        // alphabet *and* its padding -- node's own `base64url` drops the
        // padding and the server refuses to start. Generated rather than
        // written down, so this repository holds no key at all.
        ORS_SECRET_KEY: randomBytes(32)
          .toString("base64")
          .replaceAll("+", "-")
          .replaceAll("/", "_"),
        ORS_HOST: "127.0.0.1",
        ORS_PORT: String(port),
        ORS_WEB_DIR: webDir,
        ORS_LOG_LEVEL: "INFO",
      }
      const daemonEnv: NodeJS.ProcessEnv = {
        ...process.env,
        ORS_E2E_SPI_ROOT: spiRoot,
        ORS_E2E_PANEL_DIR: panelDir,
      }

      // One mutable record rather than three `let`s: every one of these is
      // assigned from inside a closure, and TypeScript's flow analysis does not
      // follow that -- a `let` initialised to `null` here reads as `null` for
      // the whole of the teardown below, narrowing the checks to `never` and
      // making them compile while asserting nothing.
      const running: {
        server: Managed | null
        daemon: Managed | null
        session: APIRequestContext | null
      } = { server: null, daemon: null, session: null }

      const startServer = async () => {
        if (running.server !== null && !running.server.hasExited) {
          throw new Error("the server is already running")
        }
        const started = new Managed("ors-server", serverBin, [], serverEnv)
        running.server = started
        await until(`the server answered on ${origin}`, START_BUDGET_MS, async () => {
          if (started.hasExited) {
            throw new Error(`ors-server exited before it answered:\n${started.log()}`)
          }
          return await answers(origin)
        })
      }

      const stopServer = async () => {
        if (running.server === null) throw new Error("no server to stop")
        await stopAndProveGone(running.server)
        // The third fact: the pid is gone *and* the port it held answers
        // nothing. A restart in the next spec binds this port again, and a
        // half-closed listener would make that fail in a way that reads as the
        // interface's problem rather than as this fixture's.
        await until(`nothing answers on ${origin} any more`, 10_000, async () => {
          return !(await answers(origin))
        })
        running.server = null
      }

      const api = async (): Promise<APIRequestContext> => {
        if (running.session === null) {
          const client = await request.newContext({ baseURL: origin })
          const signedIn = await client.post("/api/auth/login", { data: { password: PASSWORD } })
          if (!signedIn.ok()) {
            throw new Error(`the fixture could not sign in: ${signedIn.status()}`)
          }
          running.session = client
        }
        return running.session
      }

      const arrange = async () => {
        const client = await api()

        // **Off, and this is not tidying.** `NightWindow` defaults to enabled
        // between 23:00 and 07:00, and the server's default timezone is UTC --
        // so an unmodified fresh database puts every panel to sleep for eight
        // hours of every day. Left alone, this whole file would pass all day
        // and fail every night, on the specs that assert pixels, for a reason
        // no assertion in them names.
        const settings = await client.patch("/api/settings", {
          data: { night: { enabled: false, start: "23:00", end: "07:00" } },
        })
        if (!settings.ok()) throw new Error(`could not turn the night window off: ${settings.status()}`)

        // **A second rack, and two screens on it, so that no two numbers in
        // this file coincide.** This project has shipped three bugs hidden by
        // exactly that: a screen with id 1 at position 1, and contiguous ids
        // counted from 1. With this arrangement the rack that runs is daemon 2,
        // the panel the wizard creates is screen 3 at position 1 and array
        // index 0 -- four different numbers, so a component keyed by the wrong
        // one cannot pass by coincidence. `rack.spec.ts` asserts they came out
        // distinct rather than trusting this comment.
        //
        // It is also a negative control the pixel specs need: these two panels
        // belong to a rack nothing is running, so they must stay blank while
        // the real one draws. A spec that asserted "some canvas has pixels"
        // would pass against the wrong panel; these are how it cannot.
        const spare = await client.post("/api/daemons", { data: { name: SPARE_RACK } })
        if (!spare.ok()) throw new Error(`could not create ${SPARE_RACK}: ${spare.status()}`)
        const spareId = (await spare.json()).id as number
        for (const [index, name] of ["Attic", "Garage"].entries()) {
          const made = await client.post("/api/screens", {
            data: {
              daemon_id: spareId,
              name,
              position: index + 1,
              template: "text-only",
              display: { backend: "virtual", out_dir: join(root, "nowhere") },
            },
          })
          if (!made.ok()) throw new Error(`could not create the screen ${name}: ${made.status()}`)
        }
      }

      const startDaemon = async (token: string) => {
        if (running.daemon !== null && !running.daemon.hasExited) {
          throw new Error("the daemon is already running")
        }
        // `connect` opens no socket -- it writes the pairing a `run` dials
        // with -- so it is a synchronous call that either wrote the file or
        // said why on stderr.
        execFileSync(python, [
          join(HERE, "virtual_rack.py"),
          "connect",
          "--server",
          origin,
          "--token",
          token,
          "--link",
          linkPath,
        ], { cwd: REPO, env: daemonEnv, stdio: "pipe" })

        const started = new Managed("ors-daemon", python, [
          join(HERE, "virtual_rack.py"),
          "run",
          "--config",
          configPath,
          "--link",
          linkPath,
          "--status",
          join(root, "status.json"),
        ], daemonEnv)
        running.daemon = started

        const client = await api()
        await until("the rack reported itself online", START_BUDGET_MS, async () => {
          if (started.hasExited) {
            throw new Error(`ors-daemon exited before it connected:\n${started.log()}`)
          }
          const listed = await client.get("/api/daemons")
          if (!listed.ok()) return false
          const racks = (await listed.json()) as { name: string; online: boolean }[]
          return racks.some((each) => each.name === RACK && each.online)
        })
      }

      await startServer()

      await use({
        origin,
        panelDir,
        startServer,
        stopServer,
        startDaemon,
        api,
        arrange,
        panelWrittenAt: (screenName) => {
          const path = join(panelDir, `${screenName}.png`)
          if (!existsSync(path)) return null
          return statSync(path).mtimeMs
        },
        daemonLog: () => running.daemon?.log() ?? "",
        serverLog: () => running.server?.log() ?? "",
      })

      // The daemon first: it dials the server, and stopping the server under it
      // would have it spend its backoff on a socket that is going away.
      const failures: string[] = []
      for (const managed of [running.daemon, running.server]) {
        if (managed === null) continue
        try {
          await stopAndProveGone(managed)
        } catch (error) {
          failures.push(String(error))
        }
      }
      if (running.session !== null) await running.session.dispose()
      if (failures.length > 0) {
        // Left on disk on purpose when something would not stop: the database,
        // the pairing and the PNGs are what somebody would need to find out
        // why, and this is the one path where they are worth more than a tidy
        // /tmp.
        throw new Error(`${failures.join("\n\n")}\n\nleft behind for inspection: ${root}`)
      }
      // Both processes are proven gone by here, so nothing holds this open.
      // Their output is in the failure messages above and the browser's trace
      // is in `test-results/`, so there is nothing here a diagnosis needs.
      rmSync(root, { recursive: true, force: true })
    },
    { scope: "worker", timeout: 120_000 },
  ],

  ui: [
    async ({ browser }, use) => {
      const context = await browser.newContext()
      // Started here rather than configured in `playwright.config.ts`: a
      // context built by hand does not read the `use` block, so this is the
      // only place a trace of this run can be asked for.
      await context.tracing.start({ screenshots: true, snapshots: true })
      const page = await context.newPage()
      await use(page)
      await context.tracing.stop({ path: join(WEB, "test-results", "rack.trace.zip") })
      await context.close()
    },
    { scope: "worker" },
  ],
})

export { expect } from "@playwright/test"
