# M3c — Install, setup, and joining a rack without a terminal

**Status:** design, approved 2026-08-16. No code written.

**Goal.** A stranger with a Raspberry Pi and four panels can go from nothing to a
lit rack without cloning this repository, without writing a YAML file, and
without pasting a command into the Pi to pair it.

**Why now.** M3b finished the interface and the image that serves it, but the
project is not installable. That is not an exaggeration about documentation
quality: it is a fact about metadata. `daemon/pyproject.toml` resolves
`ors-schema` and `ors-render` through `[tool.uv.sources] { workspace = true }`,
which means something only inside this workspace, and neither package exists on
any index. `pipx install ors-daemon`, `uv tool install ors-daemon`, and
`pip install "git+https://github.com/SilkePilon/OpenRackScreen#subdirectory=daemon"`
all fail on two unresolvable dependencies. The only way anyone has ever
installed the daemon is `git clone` plus `uv sync --all-packages`.

---

## 1. Decisions taken

Each of these was a live option and the alternatives are recorded so a later
reader does not relitigate them.

| Decision | Chosen | Rejected, and why it was tempting |
| --- | --- | --- |
| Distribution | Publish to PyPI | Git-only install needs no namespace but leaves the Pi needing `git` and gives no upgrade path. A `curl \| sh` installer is one line but is a real program written in shell that has to be tested on hardware. |
| Package names | Keep `ors-*` | `openrackscreen-daemon` is more searchable, but the console script is `ors-daemon` and a name that does not match what you run is its own confusion. |
| Config source | `--config` becomes optional | Shipping a starter `rack.yaml` is a smaller change, but then every rack carries a file that is real, is read during a server outage, and is wrong the moment the interface adds a screen. |
| Install blast radius | `install` edits `/boot/firmware/config.txt` by default | Making SPI opt-in is safer — a bad edit to a boot-critical file needs another machine and an SD reader. Overruled deliberately in favour of one command from nothing to a lit rack. Mitigated with a timestamped backup and an idempotent edit. |
| Pairing | Zero-touch: announce and approve | Folding a token into `install` is one honest paste and no new protocol. Rejected because the standing requirement is that pairing happens in the web interface with no CLI, and a line you paste on the Pi is a CLI step wearing a web coat. |
| Scope | One spec, not two | Splitting install from pairing would have let the install half ship this week. Overruled deliberately: pairing should not have an interim token-paste story that is later deleted. |

---

## 2. Publishing and versioning

### 2.1 What gets published

Five distributions, all currently available on PyPI (checked 2026-08-16):

| Distribution | Import name | Why it is published |
| --- | --- | --- |
| `ors-schema` | `ors_schema` | Dependency of every other package. |
| `ors-render` | `ors_render` | Dependency of the daemon and the server. |
| `ors-daemon` | `ors_daemon` | Installed on the Pi. Console script `ors-daemon`. |
| `ors-server` | `ors_server` | Installed anywhere. Console script `ors-server`. |
| `openrackscreen` | — | A squat-blocker on the project's own name. Depends on `ors-daemon` and `ors-server` at the same version and contains no modules. |

The root `pyproject.toml` keeps `[tool.uv]  package = false` and remains the
workspace root; `openrackscreen` is a new one-file package under
`packages/openrackscreen/` so that the root's role does not change.

### 2.2 The metadata already works; the versions do not

`[tool.uv.sources]` is uv-specific and never reaches a wheel. A built
`ors-daemon` already declares `Requires-Dist: ors-schema`, which resolves from
PyPI correctly once `ors-schema` is there. Nothing needs restructuring.

What is wrong is that the requirement carries no version. `dependencies =
["ors-schema"]` permits pip to pair a new daemon with an old schema, and the
link protocol between them is the one thing in this project where a mismatch is
silent — a daemon that parses a snapshot it half-understands draws a rack rather
than refusing.

**All five distributions share one version**, and every intra-project dependency
pins it exactly:

```toml
# daemon/pyproject.toml
dependencies = [
    "ors-schema==0.2.0",
    "ors-render==0.2.0",
    ...
]
```

`tools/version.py <version>` rewrites the `version` field of all five
`pyproject.toml` files and every intra-project pin in one pass. A test in
`tests/test_packaging.py` reads all five files and fails if any version or any
intra-project pin disagrees with the others. The script is a convenience; the
test is the mechanism.

### 2.3 The interface ships inside the server wheel

This is what makes `uv tool install ors-server` mean anything.

`AppSettings.web_dir` defaults to `/app/web` today — a path that exists only in
the container. A pip-installed server would serve `/api/*`, pass its health
check, and return 404 for every page. That exact failure already happened once
on this machine with a stale published image, and it is not a failure anyone
diagnoses quickly, because the server looks healthy from every angle except a
browser.

- The release workflow runs `pnpm install --frozen-lockfile && pnpm build` and
  copies `web/dist/` to `server/src/ors_server/web/` before `uv build`.
- `server/pyproject.toml` gains `[tool.hatch.build.targets.wheel.force-include]`
  so the directory ships as package data. `server/src/ors_server/web/` is
  gitignored: it is a build output, and a committed copy would go stale.
- Resolution order in `ors_server.__main__`: `ORS_WEB_DIR` if set, otherwise the
  `web` directory beside `ors_server/__init__.py`. The existing behaviour of
  `create_app` — warn once and serve the API alone when the directory holds no
  build — is unchanged and remains the ordinary development state.
- `deploy/Dockerfile` keeps setting `ORS_WEB_DIR=/app/web` explicitly. The
  container's path stays visible rather than becoming implicit, and the image
  keeps building the interface in its own Node stage.

### 2.4 Data directory

`ORS_DATA_DIR` defaults to `/var/lib/openrackscreen`, which needs root. A user
who runs `uv tool install ors-server && ors-server` is not root and gets a
`PermissionError` on first boot.

The code default becomes `~/.local/state/openrackscreen`
(`$XDG_STATE_HOME/openrackscreen` when that is set). Both the Dockerfile and the
generated systemd unit set `ORS_DATA_DIR` explicitly, so neither depends on the
default. Nothing about the directory's contents changes, and the existing
auto-generated `ORS_SECRET_KEY` — written 0600, refused if the mode is looser —
already means a fresh server needs no secret handed to it.

### 2.5 Release

- Trigger: a `v*` tag.
- CI builds all five distributions with `uv build` and publishes with **PyPI
  Trusted Publishing** (OIDC). No API token is stored in this repository.
- The same workflow builds and pushes the container image, so the image and the
  wheels always describe the same commit.
- `uv build` runs after the interface build, and a job step asserts the built
  `ors_server` wheel contains `ors_server/web/index.html` before anything is
  uploaded. A wheel without the interface is the failure this whole section
  exists to prevent, and it must not be publishable.

---

## 3. The server, installed

Three supported paths, in the order the README will present them.

**Docker**, unchanged as the recommended path for a server that stays up, with
one fix: `deploy/compose.pi.yaml` and `deploy/compose.remote.yaml` both name
`image: ghcr.io/silkepilon/openrackscreen:latest` *and* a `build:` block, and
compose prefers the image. A checkout therefore starts whatever copy of that tag
happens to be on the machine and never builds itself. The `image:` key moves to
an override file so the default `docker compose up -d` in a checkout builds the
checkout.

**`uv tool install ors-server`**, then `ors-server`. Creates its own data
directory, generates its own key, serves the packaged interface on `:8080`.
Nothing to create, nothing to configure.

**`ors-server install`**, for a server that should survive a reboot without
Docker. Same shape as the daemon's command (§4): system user `openrackscreen`,
data directory, a unit at `/etc/systemd/system/ors-server.service` with
`ORS_DATA_DIR` set explicitly, `daemon-reload`, `enable --now`.

---

## 4. `ors-daemon install`

Requires root. Every step is idempotent and the command is safe to re-run; that
is what makes it usable as an upgrade path rather than a one-shot.

### 4.1 Where the code lives

`sudo uv tool install ors-daemon` installs into **root's**
`~/.local/share/uv/tools`. The unit runs `User=openrackscreen`, which cannot
read it, and the rack comes up dead with a permission error on the interpreter
itself — a failure that looks like nothing in the daemon's own logs, because the
daemon never starts.

So `install` materialises one predictable location:

- Creates a venv at `/opt/openrackscreen` and installs
  `ors-daemon[hardware]==<the running version>` into it from PyPI.
- Points `ExecStart` at `/opt/openrackscreen/bin/ors-daemon`.

One path whatever you invoked `install` from, and one upgrade story
(`ors-daemon install --upgrade`). The cost is that install needs network access
and the code briefly exists twice.

`daemon/examples/openrackscreen.service` currently says
`/opt/openrackscreen/.venv/bin/ors-daemon` — the same directory with a nested
venv, because it was written for a `uv sync` in a checkout. `uv venv
/opt/openrackscreen` puts binaries in `bin/`, so the example unit is updated to
match what `install` generates. The two are compared by a test (§9) precisely so
they cannot drift again.

- `--prefix PATH` puts the venv elsewhere.
- `--use-current-interpreter` skips the venv and points `ExecStart` at
  `sys.executable`. `install` verifies the service user can execute it and
  refuses with a message naming the mode and owner if it cannot.

### 4.2 What it does

1. **User and group.** Creates the `openrackscreen` system user and group if
   absent (`--system`, no login shell, no home) and adds it to `spi` and `gpio`.
   A missing `spi` or `gpio` group is reported, not created: on Raspberry Pi OS
   they come with the udev rules that make them mean anything, and inventing a
   group with no rules behind it produces a rack that comes up with four
   unavailable screens and a plausible-looking configuration.
2. **Directories.** `/etc/openrackscreen` (0755) and `/var/lib/openrackscreen`
   (0700, owned by the service user). `/run/openrackscreen` is left to the
   unit's `RuntimeDirectory=`.
3. **SPI.** See §4.3.
4. **The venv.** See §4.1.
5. **The unit.** Written to `/etc/systemd/system/openrackscreen.service`,
   generated from `daemon/examples/openrackscreen.service`. Every load-bearing
   line is carried over with the comment that explains it:
   `StartLimitIntervalSec=0`, `SupplementaryGroups=spi gpio`, the deliberate
   absence of `PrivateDevices=yes`, `TimeoutStopSec=30`, `RuntimeDirectory=`,
   `StateDirectory=`. `ExecStart` loses `--config`, which is no longer required
   (§5).
6. **Enable.** `systemctl daemon-reload`, then `enable --now`.
7. **Report.** Prints what changed, the short code from §6.2, the URL to approve
   at if one was discovered, and whether a reboot is owed for SPI.

### 4.3 The `config.txt` edit

The single most common reason every panel comes up `unavailable` is that SPI1
was never enabled, and it is invisible from software.

- Target is `/boot/firmware/config.txt`, falling back to `/boot/config.txt`
  pre-Bookworm. If neither exists, `install` says so and continues — that is a
  machine that is not a Raspberry Pi, and the rest of the install is still
  valid.
- Copies the file to `config.txt.ors-<ISO8601>` before touching it.
- Adds `dtparam=spi=on` and `dtoverlay=spi1-2cs` **only if absent**. An existing
  line is left exactly as written, including one that differs from what we would
  have added: someone who tuned their overlay meant it.
- Prints the diff it applied.
- `--no-spi` skips the whole step.
- A reboot is required for the change to take effect, and the final report says
  so rather than leaving it to be discovered.

### 4.4 `ors-daemon uninstall`

Stops and disables the unit, removes it, `daemon-reload`, removes the venv.

**Leaves `/var/lib/openrackscreen` alone.** It holds the pairing and the install
identity; removing it costs a re-approval in the interface, and a command whose
name is "uninstall" should not silently cost that. `--purge` removes it, and
says what that means before doing it.

`config.txt` is never reverted. Disabling SPI is not obviously desirable and the
backup is on disk with a name that says where it came from.

---

## 5. The config file stops being mandatory

`ors-daemon run --config` is required today. `_boot` already prefers the
server's pushed snapshot and falls back to the file, so a paired rack that has
been pushed to never reads the YAML it was forced to supply.

`--config` becomes optional and `_boot` gains an explicit four-way answer:

| State | Behaviour |
| --- | --- |
| `--config` given | Exactly as today: the snapshot cache first, the file as fallback. Standalone racks are untouched. |
| Paired, snapshot cached | Runs from the snapshot. No YAML on the machine. |
| Paired, nothing pushed yet | **Starts with zero screens and waits.** Panels appear when the server's first push lands. |
| Neither paired nor given a file | **Enters the join flow** (§6): browses for a server, files a claim, and waits to be approved. This is the state a freshly installed rack is in, and it is not an error. |
| Neither paired nor given a file, with discovery disabled and no `--server` | Exits non-zero with a message naming every way to fix it. This is the only case with no way to obtain a configuration. Not a traceback. |

The third row is the only genuinely new state. `Supervisor` holds its screens as
a list that `apply` replaces wholesale, so an empty list at boot is structurally
fine — but "structurally fine" is an assumption, and it gets a test that starts
a supervisor on no screens, pushes a snapshot carrying two, and asserts both
open.

---

## 6. Zero-touch pairing

### 6.1 Discovery

The server announces `_openrackscreen._tcp.local.` over mDNS using `zeroconf`
(ships aarch64 wheels, so nothing builds on a Pi). The TXT record
carries the scheme, the port, and the server's version.

An unpaired daemon browses for it on start. `--server URL` skips discovery
entirely, and is **not** a nicety: plenty of networks drop multicast, and
without it those racks would be unpairable by any means. Discovery finding more
than one server is reported as a list and paired with none of them; `--server`
settles it.

### 6.2 The install identity

`install` generates 32 random bytes at `/var/lib/openrackscreen/identity.json`,
mode 0600, owned by the service user.

- **Fingerprint**: a SHA-256 of the secret. This is what the server stores; the
  secret never leaves the Pi.
- **Short code**: the first six base32 characters of the fingerprint. This is
  what a human compares.

The identity survives re-pairing and outlives any single server. Deleting it —
which `uninstall --purge` does — makes the rack a stranger again.

### 6.3 The claim protocol

1. **`POST /api/racks/claims`** — `{hostname, fingerprint, short_code, version,
   public_key}`. **Unauthenticated, necessarily**: a daemon that has not been
   approved has no credential, which is the entire point. `public_key` is an
   ephemeral X25519 public key generated per claim.
2. Server answers **`202`** with a claim id. The claim is stored pending. The
   source address is recorded **by the server from the connection**, not taken
   from the body — a field the claimant fills in is a field the claimant
   chooses.
3. **The interface shows it** (§7) and an authenticated admin approves or
   denies.
4. **`GET /api/racks/claims/{id}`** — the daemon polls, and the claim id itself
   is the bearer credential: `secrets.token_urlsafe(32)`, minted by the server
   in step 2 and returned only in that one `202` response. An earlier draft of
   this step called for the daemon to authenticate each poll with an HMAC over
   the claim id under its identity secret; that is unimplementable, because
   the server never holds the identity secret to key an HMAC verification with
   — it stores only `sha256(secret)` (§6.2), which is one-way by design. A
   256-bit token generated by the server and disclosed only to whoever
   receives the `202` is the credential instead: guessing or observing it is
   exactly as hard as guessing or observing the HMAC key would have been, and
   it needs no secret on either side to check.
5. On approval the response carries the daemon key **exactly once**, encrypted
   to the ephemeral public key from step 1. The server discards its copy after
   handing it over, exactly as the existing token flow does — it keeps only a
   fingerprint. **Forward note for the claim-poll task**: because the key is
   discarded on handoff, a poll that guesses or observes a claim id does not
   merely obtain an undecryptable blob (it lacks the matching private key) —
   it *consumes* the one-shot delivery, and the legitimate daemon's next poll
   then finds nothing left to collect and can never pair. The poll handler
   must therefore either be idempotent (a repeat poll from the same claim
   after delivery re-sends the same encrypted key rather than finding it
   gone), or the server must defer discarding its copy until the daemon
   acknowledges a successful decrypt. Which of the two is a decision for
   whoever implements that task; this spec only records that picking neither
   is a bug.

`connect --token` is **kept**. It works, it is tested, and it is what a scripted
or headless install uses. What changes is which flow the interface leads with.

### 6.4 Security model, stated plainly

**Anyone on the LAN can file a claim.** The only gate is an authenticated
admin's click. That is a deliberate position, not an oversight: the alternative
is a shared secret, which is the token flow this milestone exists to remove.

**The short code is what makes the click meaningful.** It is printed by
`install` and repeated in `journalctl -u openrackscreen`. Approving without
comparing it is approving a stranger's rack onto your server, and the
interface's approve dialog says so in those words. Its 30 bits are a check
against *confusion between the racks an admin is actually choosing among*, not
against a determined attacker: grinding secrets until the first 30 bits of
their SHA-256 collide with a code already observed on someone's screen is
~2^30 hashes, seconds on ordinary hardware, and `identity.py`'s own docstring
says as much — this is that limit stated in the one other place it matters.

**The code cannot be painted on the panels**, which was the first idea and would
have been the best confirmation available. A GC9A01 has no ID register readable
over 4-wire SPI, and DC and RST are plain GPIO pins that nothing on the bus
reports — which is exactly why the M3b probe wizard exists. A daemon that has
not been through that wizard cannot light anything, so there is nothing to paint
on.

**The key is encrypted in transit** to the claim's ephemeral public key. This
adds `cryptography` to the daemon's dependencies — already a server dependency,
publishes `manylinux` aarch64 wheels, and this project is arm64-only since
`linux/arm/v7` cannot build the image at all. Without it the key would cross the
LAN in cleartext over plain HTTP, exactly as today's pasted token does; "no
worse than the thing we are replacing" is not a good enough bar for a protocol
being designed from scratch.

**TLS is out of scope** (§10). The encryption above protects the key itself, not
the rest of the conversation.

### 6.5 Abuse limits

An unauthenticated endpoint is a queue anyone can fill.

| Limit | Value | Reason |
| --- | --- | --- |
| Pending claims | 32 | Beyond this the endpoint answers 429 and the interface says the queue is full. A rack cannot be hidden by flooding. |
| Per-address rate | Reuses the existing login limiter, keyed on `request.client.host` | Already built, already tested, already handles the proxy case. |
| Claim lifetime | 30 minutes | A daemon polling an expired claim files a new one rather than waiting forever. |
| Denied fingerprints | Suppressed for 24 hours | A denied rack that reappears every 5 seconds trains people to click Approve. |

---

## 7. Interface changes

The Daemons page grows a **"Waiting to join"** section above the rack list,
present only when there is at least one pending claim.

Each entry shows hostname, source address, short code, daemon version, and when
it was first seen. Two actions:

- **Approve** — opens a dialog that shows the short code large, states that it
  must match what the Pi printed, and names what approving grants: the right to
  receive this server's configuration and to draw on this rack's panels.
  Confirming creates the rack and mints the key.
- **Deny** — removes the claim and suppresses the fingerprint for 24 hours.

The claim list refreshes itself while the page is open, so a rack that asks to
join appears without a reload. Two things do that, and it is worth being exact
about which does what:

- **A short poll (10 s) on the claim query.** This is what covers the case that
  matters: an admin with the page open, a quiet network, somebody plugging a Pi
  in. Nothing moves on the server at that moment -- `POST /api/racks/claims` is
  unauthenticated, touches no hub and wakes no browser -- so nothing can be
  pushed to the browser about it.
- **The existing `/ws/ui` socket's `daemons` message, which invalidates the same
  query.** That covers the end of this flow: an approved rack collects its key
  and dials in, and the list is re-read at once rather than up to a tick later.

The socket cannot carry the claim itself. `ws_ui.py` encodes exactly two message
types, `frame` and `daemons`, and the `daemons` message must not be repurposed:
it means "these racks are online" and the browser answers it by writing into its
cache of the racks, which is not a write an unauthenticated LAN caller should be
able to provoke.

**When the socket protocol is next opened, the better shape is a bare
`{"type": "claims"}` nudge** -- a message with no payload at all. Nothing about
a pending claim crosses the socket, the browser re-reads over the
session-guarded `GET /api/claims` exactly as it does now, and the poll can go.

The existing "Add a rack" token flow stays reachable, described as the option
for a rack that cannot use discovery.

---

## 8. Failure table

| Situation | What happens |
| --- | --- |
| `install` run without root | Exits 2 naming the one thing it needs. No partial state. |
| `spi` or `gpio` group missing | Reported; the user is added to whichever exists. Not created. |
| `/boot/firmware/config.txt` and `/boot/config.txt` both absent | Says the machine does not look like a Raspberry Pi and continues. |
| `config.txt` already has both lines | Nothing written, no backup taken, and it says so. |
| `--use-current-interpreter` where the service user cannot execute it | Refuses before writing the unit, naming the mode and owner. |
| Daemon starts, unpaired, no config, no server found | Logs what it is looking for and keeps browsing. It does not exit: a server that boots after the Pi is normal. |
| Discovery finds two servers | Both listed, neither used. `--server` settles it. |
| Claim queue full | 429; the daemon retries with backoff; the interface says the queue is full and points at the pending list. |
| Claim expires unapproved | Daemon files a new one. The short code does not change, so the entry looks the same to a human. |
| Approval arrives while the daemon is offline | The key waits with the claim until it expires. A daemon that misses it files a new claim and is approved again. |
| Paired rack, server unreachable at boot | Boots from the cached snapshot, as today. |
| Paired rack, no snapshot, server unreachable | Starts with zero screens and waits. Nothing is drawn, and the status file says why. |
| Server wheel built without the interface | The release workflow fails before publishing. |
| Versions or cross-pins disagree | `tests/test_packaging.py` fails. |

---

## 9. Testing

**Nothing in this milestone may touch the machine running the tests.** Install
is parameterised on its roots — `etc_root`, `boot_root`, `state_root`,
`prefix` — and every test passes `tmp_path`. The `useradd`, `systemctl` and
`uv` invocations go through an injected runner that records calls.

- **Install**: idempotency (a second run changes nothing and says so), the user
  and group step against an existing user, the refusal when not root, the
  `--use-current-interpreter` permission check, and the generated unit compared
  against `daemon/examples/openrackscreen.service` so the two cannot drift.
- **`config.txt`**: absent file, both Bookworm paths, an existing `dtparam` line
  left alone, backup naming, and the diff.
- **Boot order**: all four rows of §5's table, including a supervisor started on
  zero screens that opens two when a snapshot arrives.
- **Claim protocol**: the cap, the rate limit, expiry, deny-suppression, a poll
  rejected when its claim id is wrong, and that a repeat poll of an approved
  claim returns **the same ciphertext** — the grant is idempotent, not
  discard-on-read — with the granted row's own expiry (`CLAIM_LIFETIME_S`
  after `granted_at`) being what ends the delivery window rather than the
  first read. §6.3 step 5 left "idempotent or defer the discard" open to
  whoever implemented the poll route; it was settled as idempotent, because
  the store writes the sealed blob to `claim.granted_key` and never clears
  it, and because discard-on-read is the failure §6.3's own forward note
  names: a poll that merely guessed or observed a claim id would consume the
  one-shot delivery and leave the legitimate daemon permanently unable to
  pair.
- **Discovery**: `zeroconf` stubbed; one server, two servers, none.
- **Packaging**: the five versions and every intra-project pin agree; the built
  server wheel contains `ors_server/web/index.html`; no wheel's `Requires-Dist`
  mentions a workspace path.
- **Interface**: the pending list, the approve dialog's confirmation, deny, and
  a claim arriving over the socket without a reload.
- **E2E**: the virtual rack joins by claim instead of by token.

---

## 10. Non-goals

- **TLS.** The key is encrypted to the claim's ephemeral key; the rest is plain
  HTTP on a LAN. A reverse proxy remains the answer, as `server/README.md`
  already documents.
- **A Windows or macOS daemon.** The daemon drives SPI panels on a Pi.
- **Multi-user accounts, roles, audit trails.** One admin password, as M3b.
- **Auto-update.** `ors-daemon install --upgrade` is a command you run.
- **Publishing the web package to npm.** The interface ships inside the server
  wheel and the container image; it is not a library.

---

## 11. What M4 still owns

Unchanged by this milestone: the Jellyfin, \*arr, qBittorrent and Grafana
integrations; the visual template editor; the workflow builder; `frames_dropped`
reaching the interface; whether `sleep`/`wake`/`reload` become real commands
rather than a 501; and an `arm64` image, which nothing has yet built.
