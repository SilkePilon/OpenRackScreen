# ors-daemon

Drives the rack's round GC9A01 panels from one YAML file. One process: a thread
per screen, a thread per integration, a supervisor over them, and an atomic
status file so one `cat` over SSH explains a wrong panel.

No server is involved. `examples/rack.yaml` is the config the author's rack
actually runs, and CI validates it on every push.

## Install

On anything (laptop, CI) — everything except driving a panel:

```bash
uv sync --all-packages
uv run ors-daemon --help
```

On the Pi, add the `hardware` extra. It pulls `luma.lcd` (SPI/GPIO) and `numpy`,
neither of which builds on x86 CI, which is why it is an extra and not a
dependency:

```bash
uv sync --all-packages --extra hardware
```

`numpy` is not optional in practice: packing a 240×240 frame to RGB565 costs
**0.27 ms** with it and **13.9 ms** without, per panel per frame, on one Pi core.
The pure-Python fallback exists so the daemon still starts, not so it can be
lived with.

## Enable SPI

Both buses, in `/boot/firmware/config.txt` (`/boot/config.txt` before Bookworm):

```ini
# SPI0: CE0 on GPIO8, CE1 on GPIO7. Panels 1 and 2, at 40 MHz.
dtparam=spi=on

# SPI1: the auxiliary block. Two chip selects, CE0 on GPIO18 and CE1 on GPIO17,
# with SCLK/MOSI/MISO on GPIO21/20/19. Panels 3 and 4, at 16 MHz.
dtoverlay=spi1-2cs
```

Reboot, then check that all four devices exist and that the daemon's user can
reach them:

```bash
ls -l /dev/spidev0.0 /dev/spidev0.1 /dev/spidev1.0 /dev/spidev1.1
sudo usermod -aG spi,gpio openrackscreen
```

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

## The five commands

Every command except `connect` takes `--config`. Two of them need no hardware at
all, and only `connect` and `run` have anything to do with a server.

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

Once a rack is paired, the server is where its configuration comes from. `run`
boots from the last snapshot the server pushed if it has a usable one, and from
`--config` otherwise -- so the file is the fallback that keeps a server outage
from darkening anything, and a rack that has never been paired runs exactly as
it did before there was a server at all. A pushed configuration is applied to
the running process: only the screens that actually changed are stopped and
reopened, so a redundant push is not a rack-wide flicker.

The pairing and the cached snapshot live in `/var/lib/openrackscreen`, which the
shipped unit's `StateDirectory=` creates. `--link` moves them; the cache follows
it unless `--cache` says otherwise.

Two things `run` does *not* pick up from a push: the integrations (a poller and
its `kubectl port-forward` cannot be replaced inside the apply's budget, and the
daemon logs a warning naming the change) and the timezone. Both need a restart,
which is `systemctl restart openrackscreen`.

## Run it under systemd

`examples/openrackscreen.service` is the unit, commented with the reason for
every line that is not obvious. The parts that matter for surviving a reboot and
a cluster outage:

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
