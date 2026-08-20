# ors-daemon

Drives the rack's round GC9A01 panels. One process: a thread per screen, a
thread per integration, a supervisor over them, and an atomic status file so one
`cat` over SSH explains a wrong panel.

A rack runs one of two ways. **Paired**, where the server owns the
configuration and pushes it — which is what `ors-daemon install` and the
approval in the interface set up, and which needs no local YAML at all. Or
**standalone**, from one file, which is what `examples/rack.yaml` is: the config
the author's rack actually runs, validated by CI on every push. Nothing about
the standalone path changed in M3c.

## Install

On a rack, one command sets the machine up and a second one is not needed:

```bash
uv tool install "ors-daemon[hardware]"
sudo ors-daemon install
```

**Neither line works today.** Nothing is on PyPI — see
[Nothing is published yet](../README.md#nothing-is-published-yet) — so the first
fails to resolve, and `install`'s own venv step fails the same way. `install`
handles that without leaving a dark rack behind: it writes the unit and then
**deliberately does not enable or start it**, saying so, because a
`Type=simple` unit whose `ExecStart` does not exist fails asynchronously,
`enable --now` exits 0 anyway, and `StartLimitIntervalSec=0` means it can never
latch into `failed` — it would sit in `activating (auto-restart)` forever with
`systemctl is-failed` answering no, which on a rack is a dark screen with
nothing to find it by. `--use-current-interpreter` is the way past it today:
it skips the venv entirely and points the unit at an `ors-daemon` already on the
machine (installed from a checkout), refusing up front if the service user could
not execute it.

### What `install` does

Eight steps, in this order, and every one of them idempotent — re-running
`install` **is** the upgrade path, which is all `--upgrade` means. It changes no
behaviour and exists to say so at the command line.

1. If `--use-current-interpreter` was asked for, check the service user could
   actually run what is about to be named — *before* anything else is touched.
   `sudo uv tool install ors-daemon` lands in **root's** data directory, which
   `User=openrackscreen` cannot read, and the rack then comes up dead with a
   permission error on the interpreter that appears in no daemon log, because
   the daemon never starts. Refusing here leaves the machine exactly as it was.
2. `/etc/openrackscreen`, and `/var/lib/openrackscreen` at `0700` — the pairing,
   the cached snapshot and this rack's install identity live there.
3. The `openrackscreen` system user (`--system --no-create-home --shell
   /usr/sbin/nologin`), then `usermod -aG spi` and `usermod -aG gpio`. A
   `useradd` exit of 9 means the account already exists and is treated as
   success, which is what makes a second `install` — and a Pi where
   `ors-server install` created the shared account first — work in either order.
4. A venv at `/opt/openrackscreen` (`--prefix` moves it) and
   `uv pip install "ors-daemon[hardware]==<this version>"` into it. Skipped
   entirely under `--use-current-interpreter`.
5. `/etc/systemd/system/openrackscreen.service`, whose `ExecStart` is
   `<prefix>/bin/ors-daemon run --status /run/openrackscreen/status.json` —
   **no `--config`**, because a paired rack's configuration comes from the
   server and a rack `install` just set up has neither yet.
6. `systemctl daemon-reload`, `enable --now`, and `try-restart`. The last is
   `enable --now`'s complement: on an upgrade, where step 4 just replaced the
   code underneath a running daemon, `enable --now` is a no-op and nothing would
   take effect until somebody rebooted by hand. `try-restart` restarts a running
   unit and does nothing to a stopped one, so a first install is unaffected.
7. **SPI**, in `/boot/firmware/config.txt`, behind a timestamped backup — see
   [Enable SPI](#enable-spi) below for what it writes and how to check it.
   `--no-spi` skips this step.
8. This rack's install identity, from which the **short code** is derived.

It prints all of that, and the short code first:

```
short code: K7QF2M
unit: /etc/systemd/system/openrackscreen.service
service user: created
SPI: enabled, reboot needed
reboot needed: yes
```

**`reboot needed: yes` means reboot.** The firmware reads `config.txt` at boot
and there is nothing that can apply an SPI change to a running kernel, so until
the reboot every panel comes up `unavailable` and the daemon is right to say so.
The line reads `no` both when SPI was already on and when `--no-spi` skipped the
step, which is why the `SPI:` line above it distinguishes the three cases —
`enabled, reboot needed`, `unchanged (already enabled)`, `skipped (--no-spi)`,
and `not attempted (no config.txt found under the boot partition)`.

A partial install says `install: PARTIAL` and then reports everything that
really did happen, rather than withholding it: the person debugging a
half-finished install over SSH is the one who needs the detail most. The exit
code is 1. The one failure that reports nothing but its warning is the
`--use-current-interpreter` refusal, because it returns before anything at all
was touched and every other line would be describing work that never happened.

### The short code, and pairing

The short code is six base32 characters derived from this rack's identity. It is
what the interface shows under **"Waiting to join"**, and comparing the two
before clicking Approve is the whole pairing gesture — no token typed anywhere.

Be clear about what it proves: **it binds against confusion, not against
somebody who has already seen it.** It stops you approving the wrong rack when
two were installed the same afternoon, and it stops you approving a stranger's
rack when you only installed one. It is not a secret — both ends display it, and
anyone with the Pi's console or its journal can read it and could file a claim
carrying it.

Losing the terminal does not lose the code. An unpaired rack prints it to stderr
every time `run` starts the join flow, so it is in the journal:

```bash
journalctl -u openrackscreen | grep 'short code'
```

The token flow has not gone anywhere and is not a fallback: a rack that cannot
use discovery — multicast dropped, a different VLAN, a server in a bridged
container — is paired with `ors-daemon connect --server ... --token ...`, below.
`ors-daemon run --server URL` is the third shape: it files a claim against one
named server instead of browsing, so the approval gesture is unchanged where
only discovery is broken. The unit `install` writes carries no `--server`, so
using it means a systemd drop-in, or the token, which needs nothing.

### `uninstall`

```bash
sudo ors-daemon uninstall            # stop, disable, remove the unit
sudo ors-daemon uninstall --purge    # ...and delete /var/lib/openrackscreen
```

Without `--purge`, `/var/lib/openrackscreen` is left alone — the pairing, the
cached snapshot and the identity all survive, so a reinstall rejoins the server
it already belongs to. `--purge` removes the identity with everything else,
which means a new short code and **a fresh approval in the interface**. Neither
form removes the venv at `--prefix`; delete it by hand if you want it gone.

### On a laptop, or in CI

Everything except driving a panel, from a checkout:

```bash
uv sync --all-packages
uv run ors-daemon --help
```

The `hardware` extra pulls `luma.lcd` (SPI/GPIO) and `numpy`, neither of which
builds on x86 CI, which is why it is an extra and not a dependency:

```bash
uv sync --all-packages --extra hardware
```

`numpy` is not optional in practice: packing a 240×240 frame to RGB565 costs
**0.27 ms** with it and **13.9 ms** without, per panel per frame, on one Pi core.
The pure-Python fallback exists so the daemon still starts, not so it can be
lived with.

## Enable SPI

**`ors-daemon install` does this for you**, unless you passed `--no-spi`. This
section is what it writes, and what to check if it did not.

Both buses, in `/boot/firmware/config.txt` (`/boot/config.txt` before Bookworm —
`install` looks for `firmware/config.txt` first and falls back to `config.txt`,
so an upgraded Pi that kept the old file gets the one the firmware actually
reads):

```ini
# SPI0: CE0 on GPIO8, CE1 on GPIO7. Panels 1 and 2, at 40 MHz.
dtparam=spi=on

# SPI1: the auxiliary block. Two chip selects, CE0 on GPIO18 and CE1 on GPIO17,
# with SCLK/MOSI/MISO on GPIO21/20/19. Panels 3 and 4, at 16 MHz.
dtoverlay=spi1-2cs
```

Three things `install` does that a hand edit usually does not. It **backs the
file up first**, to `<config.txt>.ors-<timestamp>`, appending `.1`, `.2` … rather
than overwriting a backup that is already there — this is the file that decides
whether the Pi boots, and a backup that clobbers the previous backup is not one.
It **writes an `[all]` header of its own before the two lines**, because a
`config.txt` filter section applies until the next header and appending after a
trailing `[pi3]` or `[none]` block would add lines this Pi never reads while
`install` reported success — and for the same reason a `dtparam=spi=on` it finds
under any filter other than `[all]` does not count as already present. And it
**adds only what is missing**: a file that already has both is a no-op with no
backup taken, which is what makes `install` safe to re-run. A commented-out
`#dtparam=spi=on` — the state a Pi ships in — is not present.

Then reboot, and check that all four devices exist and that the daemon's user
can reach them:

```bash
ls -l /dev/spidev0.0 /dev/spidev0.1 /dev/spidev1.0 /dev/spidev1.1
groups openrackscreen        # should list spi and gpio
```

If the nodes are missing, the reboot has not happened or the lines are under a
section header that excludes this model. If they exist but every screen is
`unavailable`, it is the groups — `install` adds them with `usermod -aG`, and
that only takes effect for processes started afterwards, which the unit's
restart handles.

Two things about SPI1 that the wiring in `examples/rack.yaml` already accounts
for:

- **It runs at 16 MHz, not 40.** The auxiliary block's clock is derived from the
  core clock rather than from its own divider, so its usable rate moves when the
  core clock does. 16 MHz is the rate this rack is known stable at. If panels 3
  and 4 ever tear or come up garbled while 1 and 2 are fine, suspect this before
  suspecting the panels — pinning the core frequency in `config.txt` is the
  documented remedy for the same dependency elsewhere on the Pi.
- **It consumes GPIO17–21.** None of the DC/RST pins in the shipped config
  (GPIO4, 5, 6, 13, 22, 23, 26, 27) collide with those or with SPI0's GPIO7–11.
  If you rewire, check that first: a DC line quietly shared with a bus pin looks
  exactly like a dead panel.

## The seven commands

`run`, `connect`, `validate`, `render`, `identify`, `install`, `uninstall`.
Four of them take `--config` — `connect`, `install` and `uninstall` do not, and
for the same underlying reason: all three happen before a rack has a
configuration to be given, or instead of one. `--config` is optional even on
`run`; see the boot table below. Two commands need no hardware at all, and the
only two that dial a server are `connect` and `run` — `install` never does; it
starts the unit that runs `run`, and that is what goes looking.

`install` and `uninstall` are documented above, under
[Install](#install) — they are the two that have to run as root, and running
either as an ordinary user prints `ors-daemon <command> has to run as root.` and
exits 2 rather than failing partway through. The other five:

```bash
# Check a config without a rack: parses it, resolves every screen's template,
# and resolves the timezone. Opens no panel and reads no kubeconfig.
ors-daemon validate --config examples/rack.yaml

# Render every screen to PNG through the renderer, never through a panel -- so
# this works on a laptop against the real GC9A01 config. Without --data every
# screen draws `connecting`, which is what a cold rack shows.
ors-daemon render --config examples/rack.yaml --out /tmp/preview
ors-daemon render --config examples/rack.yaml --out /tmp/preview \
    --data /tmp/one-poll.json   # {"prom": {"cpu": 42.4, ...}}

# Paint each panel's `position` on it and hold it there until you press Ctrl-C,
# then blank them. This is how you map a physical panel to a line of the config
# -- it also prints the ordinal, the screen name and the SPI device.
ors-daemon identify --config /etc/openrackscreen/rack.yaml
ors-daemon identify --config /etc/openrackscreen/rack.yaml --hold 30

# Drive the rack. SIGTERM and SIGINT blank, sleep and close every panel first.
ors-daemon run --config /etc/openrackscreen/rack.yaml \
    --status /run/openrackscreen/status.json

# Pair the rack with a server, using a token minted in its interface. This
# writes the pairing and dials nothing: the token is spent by `run`, on the
# first connect, and the server hands back a key that only the daemon can save.
ors-daemon connect --server http://rack-server:8080 --token PASTE-TOKEN-HERE

# It refuses to overwrite a pairing that already works, because the key behind
# it cannot be recovered -- re-pairing means minting a new token.
ors-daemon connect --server http://rack-server:8080 --token NEW-TOKEN --force

# --log-level belongs to the program, not to the subcommand, so it goes BEFORE
# it. DEBUG | INFO (default) | WARNING | ERROR | CRITICAL, one JSON object per
# line on stderr. This is the usual thing to get wrong:
ors-daemon --log-level DEBUG run --config /etc/openrackscreen/rack.yaml
```

`identify` starts no poller and no tunnel: it opens the panels, draws, waits,
and blanks. It is safe to run against a cluster that is down. It exits non-zero
if any panel could not be opened, or opened and would not take the frame — a
panel missing from the printed map is a panel you cannot trust the map about.

`render` and `identify` name their output after each screen's `name`, which the
schema does not force to be unique: two screens called `CPU` write one
`CPU.png` between them. Give them distinct names, or read the ordinals.

**`--config` is optional on `run`.** A paired rack's configuration is the
server's, not a file an operator has to hand-author, and `install` leaves a
machine paired with nothing yet. What `run` boots from is one of five states:

1. `--config` was given and there is no usable pushed snapshot: boots from the
   file.
2. Paired, and the last pushed snapshot is usable: boots from it, `--config`
   or not — the snapshot always outranks the file, because the server is the
   source of truth once a rack is paired.
3. Paired, but nothing has ever been pushed and there is no `--config` to fall
   back to: boots with no screens at all and waits — not an error, and the
   ordinary state of a rack the moment it finishes pairing. The panels appear
   the moment the first push lands.
4. Neither paired nor given `--config`: this is the state a freshly installed
   rack is in. A usable cached snapshot beside the pairing is tried first (a
   corrupt or unreadable link.json is not a reason to give up on a good
   `snapshot.json` next to it — see the permissions note below) and boots the
   rack exactly as row 2 does. Failing that, `run` browses for a server to
   join (`--no-discovery` turns that off; `--server URL` dials one directly
   instead of browsing, and works either way) and blocks until it is paired,
   then continues in this same process rather than needing a restart.
5. The same as row 4 with no way left to reach a server at all —
   `--no-discovery` and no `--server` — and no cache to fall back on: a
   message on stderr naming every way out, and a non-zero exit, not a
   traceback.

These numbers are the ones the source uses. `_boot`'s docstring, `join.py`,
`config.py` and the tests all say "row 4" for the join and "row 5" for the
refusal; the cache is not a row of its own there, because the code tries it
ahead of both rather than instead of either. This list said otherwise until
M3c's final review, so a "row 4" read here and a "row 4" read in the code
named two different states.

A pushed configuration is applied to the running process: only the screens
that actually changed are stopped and reopened, so a redundant push is not a
rack-wide flicker. Integrations are diffed the same way — a source the push
adds is polled from that moment, one it drops is taken down on a thread of its
own so that a `kubectl port-forward` teardown does not hold up the panels, and
one whose configuration moved is replaced. What a push cannot change on a
running rack is the `timezone` — the clock is built once, at boot, from
whatever configuration the rack booted with — and what happens next splits on
whether the pushed name is even one this host can resolve:

- **Unresolvable** (a typo minted server-side, most often — `Europe/Amsterdaam`
  parses exactly as easily as the real zone does, since nothing validates a
  pushed timezone against any particular rack's tzdata): refused outright. A
  `Nack` naming the reason goes back to the server, the person who pushed it
  sees why in the interface, and the rack keeps running the configuration it
  already had. Nothing stops, and nothing needs to.
- **Resolvable, but different from the one this rack booted with**: cannot be
  picked up in place — `NightWindow` is evaluated in whatever zone the clock
  was built in, and swapping it under a running rack is not a change
  `Supervisor.apply` can make — so the rack stops instead of running against
  the wrong clock. `run` exits `10`, not `0`, and prints a line to stderr
  naming the restart before it does. The shipped unit's `Restart=always`
  reads neither the exit code nor the message and restarts on any exit either
  way, clocked correctly from the same snapshot the push just cached; the
  code and the line are for every caller that is *not* it — `Restart=on-failure`,
  a different supervisor, or a person running `ors-daemon run` at a terminal —
  which used to read exit `0` as a clean stop and never re-clock at all.

The pairing and the cached snapshot live in `/var/lib/openrackscreen`, which the
shipped unit's `StateDirectory=` creates. `--link` moves them; the cache follows
it unless `--cache` says otherwise. Every successful `connect` deletes the cache,
`--force` or not: it holds a configuration from whichever server this rack
answered to before, and a reboot before the first successful connect would boot
from it *and claim its version* — which the new server can match, skip its push
over, and leave the old rack on the glass for ever.

**Run `connect` as the user the daemon runs as.** `sudo ors-daemon connect`
writes the pairing root-owned and 0600, the unit runs the daemon as
`User=openrackscreen`, and the daemon then gets `PermissionError` on every read
of it — so the rack runs unpaired and says so in its log while the interface
shows nothing at all. Either

```bash
sudo -u openrackscreen ors-daemon connect --server http://rack-server:8080 --token ...
# or, after a sudo you have already done:
sudo chown openrackscreen: /var/lib/openrackscreen/link.json
```

`connect` prints a note when it notices it is running as root.

One thing `run` does *not* pick up from a push while it keeps running: the
integrations (a poller and its `kubectl port-forward` cannot be replaced inside
the apply's budget, and the daemon logs a warning naming the change) — that
needs a restart, `systemctl restart openrackscreen`. The timezone is different:
a resolvable one that changes stops the rack itself rather than running
against a clock built for the old zone (see above, and its exit code `10`),
and the shipped unit's `Restart=always` is what brings it back — nothing to
run by hand. One that does not resolve at all is refused instead of applied —
also see above — and needs nothing run by hand either.

## Run it under systemd

**`ors-daemon install` writes this unit for you.** What it generates and what
`examples/openrackscreen.service` holds differ only in `ExecStart` — the example
is the hand-authored copy, kept for anyone installing without the command, and
both are commented with the reason for every line that is not obvious. That
claim is compared line for line by
`test_the_generated_unit_and_the_example_differ_only_in_exec_start`; it was
prose alone until M3c's final review, and the two had already drifted apart in
their comments. The
generated one is rewritten in full by every later `install`, so an edit made in
it is an edit lost at the next upgrade: change what the command is given
(`--prefix`), use a systemd drop-in, or take the file over with
`systemctl disable openrackscreen` and hand-author your own.

The parts that matter for surviving a reboot and a cluster outage:

- `WantedBy=multi-user.target` plus `systemctl enable` — comes back after a
  reboot at all.
- `RuntimeDirectory=openrackscreen` — recreates `/run/openrackscreen` owned by
  the daemon's user on every start, so the status path works after a reboot with
  no tmpfiles rule, and a file rewritten every second stays off the SD card.
- `StartLimitIntervalSec=0` — systemd's default gives up permanently after five
  fast restarts. A rack that goes dark until a human runs `reset-failed` is
  worse than one that keeps retrying.
- `SupplementaryGroups=spi gpio`, and **no** `PrivateDevices=yes` — the latter
  hides `/dev/spidev*` and every screen comes up unavailable.
- `TimeoutStopSec=30` — long enough for four panels to be joined, slept and
  closed. If systemd SIGKILLs the daemon instead, the panels stay lit.

Nothing in the unit deals with the cluster being down, because nothing needs to:
the tunnel relaunches `kubectl port-forward` behind a probe, the poller backs off
to a 60 s cap and recovers on its own, and the panels show `connecting` and then
`NO DATA` throughout. A cluster outage must never restart this daemon.

```bash
journalctl -u openrackscreen -f          # one JSON object per line
cat /run/openrackscreen/status.json      # every screen, every integration
```

---

## Hardware checklist

Not runnable in CI — this is the part of the milestone that only exists in front
of the rack. Walk it once per hardware change, and once before calling M2 done.

### Bring-up

- [ ] `dtparam=spi=on` and `dtoverlay=spi1-2cs` are in `config.txt`, and all four
      `/dev/spidev*` nodes exist after a reboot.
- [ ] The daemon's user is in `spi` and `gpio`, and `ors-daemon validate` passes
      as that user.
- [ ] All four panels initialise: `ors-daemon run` shows four screens in
      `status.json` and none of them is `unavailable`.
- [ ] `ors-daemon identify` lights every panel with its own digit, and the
      digits run left to right in rack order. If they do not, fix `position` in
      the config rather than moving the panels.
- [ ] Per-panel `rotation` and `hflip` are right: text reads level and is not
      mirrored on any of the four. This is the one thing no test can check —
      the virtual backend applies the same transform, so a wrong config looks
      correct everywhere except on the glass.
- [ ] Panels 3 and 4 (SPI1, 16 MHz) are stable: no tearing, no garbled rows,
      through at least an hour of live data.
- [ ] `numpy` is installed (`python -c "import numpy"` as the daemon's user).
      Without it every frame costs 13.9 ms of CPU instead of 0.27 ms.

### Behaviour

- [ ] Night: at the window's start (23:00 in the shipped config) all four panels
      go dark and `status.json` reports `asleep`; at 07:00 they wake and draw
      live data, not the previous evening's.
- [ ] `systemctl stop openrackscreen` leaves every panel **dark**, not frozen on
      its last frame. Then `systemctl start` brings them all back.
- [ ] `sudo reboot` — the rack comes back on its own, with no login.
- [ ] Reboot the cluster mid-run. The panels go `NO DATA`, the tunnel relaunches,
      and live readings return without the daemon being restarted. Check
      `journalctl` afterwards: the failure should be a handful of lines, not
      thousands.
- [ ] Unplug one panel's ribbon while it runs. That screen faults and is reported
      `faulted` in `status.json`; the other three keep updating.
- [ ] 24-hour soak: no memory growth (`systemctl status` RSS at the start and the
      end), no wedged worker (no `worker wedged, restarting` in the journal), and
      four panels still showing live data at the end.

### Open question, to answer in front of a panel

- [ ] **Reset timing.** luma's `spi()` defaults `reset_hold_time` and
      `reset_release_time` to `0`, and the GC9A01 init sequence relies entirely
      on that hardware reset — there is no software reset in the table. luma's
      own docs suggest 100 ms of hold and 150 ms of release for panels like this
      one. Both the replaced script and this daemon use 0/0 and work, which is
      consistent with the reset landing inside the panel's own power-on window
      *most* of the time — precisely the shape of a bug that appears on a cold
      boot at 07:00 and never on a warm restart. If a cold boot ever comes up
      garbled, add `reset_hold_time=0.010, reset_release_time=0.150` to the
      `serial_factory(...)` call in `displays/gc9a01.py`. It is a ~160 ms one-off
      at startup and it cannot make a working panel worse.

### Finally

- [ ] **`k8s_monitor.py` is stopped and disabled on the Pi.** Two processes on
      one SPI bus is not a degraded rack, it is a corrupted one — and retiring
      that script is what this milestone is for.
