# Core M2 — Standalone Daemon Design

**Date:** 2026-08-11
**Status:** Approved for planning
**Scope:** Milestone M2 of Core (phase 1). Extends `docs/superpowers/specs/2026-08-10-openrackscreen-core-design.md`, which remains the authority for anything this document does not restate.

---

## 0. Working rules for whoever implements this

**Research before writing code. Assume nothing.** Spec §0 of the Core design applies unchanged: every external interface named here is a starting hypothesis to confirm against current upstream documentation, and where research contradicts this document, the research wins — raise it, update the spec, then implement.

For this milestone specifically, confirm before implementing:

- **luma.lcd / luma.core** — current `spi()` signature, framebuffer API, and whether a maintained GC9A01 device class now exists upstream. The existing `k8s_monitor.py` subclasses `luma.core.device.device` by hand; that init sequence is known-working on this hardware and is the reference. Changes to it must be justified.
- **Raspberry Pi SPI** — how SPI1 and its chip-selects are enabled via `dtoverlay`, the maximum reliable clock per bus, and GPIO conflicts. The existing wiring (SPI0.0/SPI0.1 at 40 MHz, SPI1.0/SPI1.1 at 16 MHz) is known-working.
- **GC9A01** — MADCTL (0x36) bit meanings, and the timing requirements around sleep-in/out (0x10/0x11) and display-off/on (0x28/0x29). The datasheet requires ≥120 ms after sleep-out before the next command.
- **Prometheus HTTP API** — `/api/v1/query` request and response shapes, error shapes, and the correct handling of `scalar` vs `vector` vs `matrix` result types. Prometheus emits `NaN`; confirm how it appears on the wire.
- **kubectl port-forward** — behaviour on connection loss, and whether the Kubernetes Python client's portforward is a better fit than shelling out.
- **Python** — the version Raspberry Pi OS currently ships, before pinning anything.
- **`zoneinfo`** — whether `tzdata` must be installed explicitly on Raspberry Pi OS.

**Also:** test-driven, failing test first. Everything except the hardware checklist (§8) runs in CI on x86 with no Pi attached. §9 lists what M2 explicitly excludes.

---

## 1. Problem

M1 delivered `ors-render`: scene JSON in, PIL image out, with seven built-in templates that reproduce all four screens of `k8s_monitor.py`. Nothing drives hardware, nothing fetches data, nothing runs unattended.

## 2. Goal

A daemon that runs on the Pi from a hand-written config file and replaces `k8s_monitor.py` outright — polling Prometheus, rendering the four screens, driving the panels over SPI, and sleeping at night — with no server involved.

M2 is complete when the rack runs from that config file and `k8s_monitor.py` is stopped on the Pi.

## 3. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Concurrency | **Threads** — one per integration, one per screen, one supervisor | Matches the proven script; SPI writes and HTTP calls both block, and both thread cleanly. asyncio would need a thread executor for SPI anyway, giving two models instead of one. |
| Config format | **YAML, identical to the server's future snapshot**, modelled in `ors-schema` | One schema for the whole project. Hand-authoring it in M2 is the honest test of whether that schema is usable before M3 depends on it. |
| Display backend contract | **Minimal: `show`, `sleep`, `wake`, `close`** | The backend is pure transport. Rotation and flip happen before it, so the virtual backend cannot lie about what the glass shows. Brightness and partial update get added when a second panel type actually needs them. |
| Integrations in M2 | **Prometheus only**, with the contract stated explicitly rather than inferred | Per the Core spec's milestone table. The known risk — an interface designed against one example — is mitigated by the poller owning all policy, so an integration is a pure fetcher with nothing to get wrong. |
| Frame pacing | **Render on change, with a heartbeat floor** | ~0.8 renders/s across four panels on a static cluster instead of ~12. The floor is the safety net against a change-detection bug freezing a screen. |
| Night mode | **Global window + per-screen override**, timezone from config | Matches the Core spec. The timezone is explicit because a wrong host TZ is the classic way this feature misfires. |
| K8s access | **Direct URL and the kubeconfig tunnel, both in M2** | Without the tunnel, M2 cannot run on the rack it is written for, and running on the rack is the milestone's own exit criterion. |
| Observability | **Structured logs + an atomic status file** | One `cat` over SSH explains a wrong panel. M3's link client reports the same structure upstream rather than inventing a second one. |

## 4. Architecture

One process, one package, consuming M1 unchanged.

```
┌─ ors-daemon (systemd, on the Pi) ─────────────────────────────┐
│                                                               │
│  supervisor ── thread lifecycle, watchdog, SIGTERM            │
│      │                                                        │
│      ├── tunnel thread(s)      kubectl port-forward + probe   │
│      ├── poller thread / integration   interval, backoff,     │
│      │        │                        health, retries        │
│      │        └── Integration (pure fetch)  prometheus.py     │
│      │                                                        │
│      ├──────► snapshot ◄──────┐   lock-protected namespaces   │
│      │        version, health │   + monotonic version         │
│      │                        │                               │
│      └── screen thread ───────┘   select scene, render,       │
│               │                   rotate/flip, show           │
│               ▼                                               │
│          DisplayBackend       gc9a01.py │ virtual.py          │
│                                                               │
│  status.py ── atomic JSON status file, ~1 Hz                  │
└───────────────────────────────────────────────────────────────┘
        imports ors-render (render_screen, load_builtin_templates)
                ors-schema (config + scene models)
```

### 4.1 Package layout

```
daemon/
  pyproject.toml                 ors-daemon
  src/ors_daemon/
    __main__.py                  CLI: run | render | identify | validate
    config.py                    YAML load, validate, resolve template refs
    supervisor.py                thread lifecycle, watchdog, signals
    snapshot.py                  namespaces, version counter, health
    poller.py                    poller thread: interval, backoff, health
    integrations/
      __init__.py                Integration protocol + type registry
      prometheus.py
    tunnel.py                    kubectl port-forward supervisor
    screen.py                    screen worker thread
    displays/
      __init__.py                DisplayBackend protocol + registry
      gc9a01.py                  SPI driver, extracted from k8s_monitor.py
      virtual.py                 writes PNGs to a directory
    status.py                    atomic status-file writer
    clock.py                     injectable now() + night-window arithmetic
    logging.py                   structured logging setup
  tests/
  examples/rack.yaml             the author's real config, validated in CI
```

### 4.2 Threading and the one shared structure

The **snapshot** is the only thing threads share. It holds a dict of namespaces (`{"prom": {...}}`), a monotonic `version` integer bumped on every successful write, and per-integration health. It is guarded by one lock, exposes `read() -> Snapshot` returning an immutable copy, and provides a `threading.Condition` that screen workers wait on.

Everything else is thread-confined: a poller touches only its own integration and the snapshot; a screen worker touches only its own backend and the snapshot.

The **supervisor** starts and stops threads, watches per-thread heartbeats, restarts a wedged worker, and on SIGTERM blanks every panel and puts it to sleep before exiting.

### 4.3 `DisplayBackend`

```python
show(image: Image.Image) -> None     # 240x240 RGB, already rotated and flipped
sleep() -> None                       # display-off, then sleep-in
wake() -> None                        # sleep-out, wait, display-on
close() -> None
```

Two implementations: `GC9A01SPI`, holding the extracted driver and its init sequence, and `VirtualDisplay`, which writes PNGs to a directory. Backend selection is a config field, so the whole daemon runs on a laptop.

Rotation and horizontal flip are applied by the screen worker before `show`, never inside the backend — the same rule M1 established for scenes.

## 5. Config schema

Modelled in `ors-schema` so M3's server pushes the identical structure. Authored as YAML for M2.

```yaml
version: 1
timezone: Europe/Amsterdam
night: { enabled: true, start: "23:00", end: "07:00" }

integrations:
  - name: prom
    type: prometheus
    poll_interval: 5
    url: http://localhost:19090
    timeout: 4
    tunnel:                                    # optional
      kubeconfig: ~/k8s-monitor.yaml
      namespace: monitoring
      service: auto                            # or an exact service name
      remote_port: 9090
      local_port: 19090
    fields:
      cpu:
        query: '100-avg(rate(node_cpu_seconds_total{mode="idle"}[2m]))*100'
      cpu_hot:
        query: '100-avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[2m]))*100'
        reduce: top
        label: instance
        strip: last_octet
      nodes_ready:
        query: 'sum(kube_node_status_condition{condition="Ready",status="true"}) or vector(0)'

screens:
  - name: CPU
    position: 1
    display:
      backend: gc9a01                          # or: virtual
      spi_bus: 0
      spi_cs: 0
      dc: 6
      rst: 5
      hz: 40000000
    rotation: 270                              # 0 | 90 | 180 | 270
    hflip: false
    enabled: true
    template: ring-gauge
    params:
      title: CPU
      value: "{{prom.cpu}}"
      big: "{{prom.cpu | round:0}}%"
      subtitle: cluster avg
      hint: "peak: {{prom.cpu_hot.node}} {{prom.cpu_hot.value | round:0}}%"
      palette: cyan
    sleep_override: null                       # or its own {enabled, start, end}
```

**Field reduction.** A Prometheus field is a PromQL query plus an optional `reduce`:

- `scalar` (default) — the first result's value, as a float
- `top` — the highest-valued series, returned as `{label, value}`, where `label` names the label to read and `strip: last_octet` turns `192.168.1.5:9100` into `.5`

That is the whole Prometheus surface M2 needs; it is what makes `peak: .5 71%` work. Anything richer waits for a real requirement.

**Templates** are referenced by name and resolved against `load_builtin_templates()`. A config may also define inline templates under a `templates:` key for a screen that no built-in covers; those are validated exactly like built-ins.

**Params** are bound through `Template.bind_params`, the M1 bridge, so defaults and overrides merge in one place rather than three.

## 6. Data path

### 6.1 The `Integration` contract

```python
name: str
open() -> None                       # build sessions, log in, nothing more
poll() -> dict[str, Any]             # namespace fields; raises on failure
close() -> None
```

An integration is a pure fetcher. It owns no interval, no retry policy, no health state and no threading. Raising is how it reports failure.

The **poller thread** owns all of that: it calls `poll()` on the configured interval, applies exponential backoff on failure (capped, jittered), tracks health, and writes results into the snapshot. A second integration in M5 is therefore one class plus a config model, with the loop and its semantics already tested.

### 6.2 Health and staleness

Three states per integration:

- `connecting` — never succeeded since start
- `healthy` — last poll succeeded
- `unhealthy(reason)` — last poll failed, with a human-readable reason

After 3 consecutive failed cycles the namespace is marked **stale**. Screens referencing a stale or `connecting` namespace fall to the corresponding system scene (§7.2).

### 6.3 The tunnel

A tunnel is its own thread, ported from `k8s_monitor.py`'s `PortForward` with its health-probe restart intact — the behaviour that fixes "kubectl process alive, tunnel dead after a cluster reboot". It supervises the subprocess, actively probes the local URL, and tears down and relaunches when probes keep failing, freeing the local port.

It exposes a `ready` event and a base URL. An integration configured with `tunnel:` takes its base URL from the tunnel and stays `connecting` until the probe passes. Nothing else about the integration differs between tunnelled and direct.

## 7. The screen worker

### 7.1 Loop and pacing

Each screen thread waits on the snapshot's condition variable with a timeout set to its next deadline — the heartbeat floor (default 5 s) or the next night boundary, whichever is sooner.

It renders when any of these holds:

- the snapshot version changed
- the selected scene changed
- the heartbeat floor elapsed
- an `identify` was requested

On a static cluster that is roughly 0.8 renders per second across four panels, against ~12 for the current script. The floor guarantees a screen cannot silently freeze if change detection is wrong.

### 7.2 Two-stage scene selection

1. **Health.** If any namespace the screen depends on is `connecting`, the `connecting` system scene renders. If stale, the `stale` scene. These are selected **by name** from the `system` template, because system scenes carry no `when` — a fact M1's review established and this design depends on.

   A screen's dependencies are derived once at config load, not per frame: walk the screen's bound params and its template's scenes, collect every `namespace.` reference, and intersect with the configured integration names. A screen whose params reference no integration — a static label, say — depends on nothing and never falls to a system scene.

   Two string kinds have to be read differently, and both count. A **binding** is braced, and the namespace need not be its first token — `{{100 - prom.cpu}}` and `{{prom.a + qbit.b}}` depend on what they name, so the whole `{{...}}` body is scanned, not just its opening. A **`when` condition** carries no braces at all: `prom.nodes_ready == prom.nodes_total and prom.alerts == 0` is a bare expression. Missing that second kind is not hypothetical — the built-in `torrent` template mentions `prom` only in its scene-level `when`, so a scan that skipped conditions would show that screen `stale` on a cold start where `connecting` belongs. Both scene-level and element-level `when` are scanned, at any nesting depth.

   The derivation errs toward declaring a dependency rather than missing one: an over-broad match costs a screen the `connecting` scene it would have shown anyway, while a missed one puts a panel's template in front of data that has never arrived.
2. **Condition.** Otherwise `select_scene` runs over the screen's template scenes as normal, and the first matching `when` wins.

### 7.3 Night mode

The current time comes from an injected `now()` and the config timezone via `zoneinfo` — never the host's local time. On entering the window each panel gets `sleep()` and its worker stops rendering entirely; pollers keep running so a wake shows live data immediately. On leaving, `wake()` then a render.

A window whose start is later than its end wraps midnight. A per-screen `sleep_override` replaces the global window for that screen, including disabling it.

## 8. Failure handling and observability

Per the Core spec §9, made concrete:

- **Config** — validated in full before anything starts; a config that fails validation is rejected with a message naming the field, and on reload the previous config keeps running.
- **Poller** — failure is scoped to one integration. Backoff, a health flag with a reason, and staleness after 3 misses.
- **Render** — an exception paints the `error` scene on that panel with a short message; the worker survives; the error is logged once per distinct message, not per frame.
- **SPI** — a write failure re-initialises that device up to 3 times with backoff, then marks the screen faulted and skips it. Siblings are unaffected.
- **Watchdog** — each worker publishes a heartbeat; the supervisor restarts a wedged one.
- **Shutdown** — SIGTERM blanks and sleeps every panel before exit.

**Logging** is structured, to stdout, for journald.

**Status file** is JSON, rewritten atomically (temp file, fsync, rename) at about 1 Hz:

```json
{
  "uptime_s": 3512,
  "config_version": 1,
  "screens": [
    {"name": "CPU", "scene": "default", "state": "awake",
     "last_render": "2026-08-11T21:14:02+02:00", "renders": 4218}
  ],
  "integrations": [
    {"name": "prom", "state": "healthy", "stale": false, "latency_ms": 42.0,
     "last_success": "2026-08-11T21:14:00+02:00", "last_error": null}
  ]
}
```

M3's link client reports this structure upstream verbatim.

Timestamps carry the daemon's configured offset rather than a `Z` suffix — the
clock is zoned by config, so `+02:00` is what a rack in Amsterdam actually
emits, and a consumer matching on `Z` alone would drop every one of them.
`stale` is present on every integration, always: a source can be `unhealthy`
without yet being stale, and that difference decides whether a panel keeps
showing its last good reading or falls to `NO DATA`.

`renders` is a lifetime counter, not a rate. A rate needs history the daemon
deliberately does not keep, and a lifetime average would read as a current
figure while being dominated by whatever the panel did hours ago — two samples
of a counter give a true rate to anyone who wants one, including M3.

## 9. Non-goals for M2

- The server, the link protocol and pairing (M3)
- Any web UI (M4)
- qBittorrent (M5) — the interface is designed for it, but no client ships
- Frame streaming upstream
- Automatic daemon updates
- Multiple daemons, or any awareness that another exists
- Panels other than GC9A01 240×240 — the backend interface exists, one implementation ships

## 10. Testing

Test-driven throughout. Everything except the hardware checklist runs in CI on x86 with no Pi.

| Area | Approach |
|---|---|
| Config | Schema round-trip; an invalid config rejected with a message naming the field; `examples/rack.yaml` validates in CI |
| Snapshot | Version bumps, concurrent read/write, health transitions |
| Poller | Fake integration: backoff, three health states, staleness after 3 misses, recovery |
| Prometheus | Mocked HTTP — scalar, `reduce: top`, error shapes, timeout, malformed JSON, and `NaN` |
| Tunnel | Fake `kubectl` on PATH: launch, probe failure → relaunch with the port freed, `service: auto` discovery, cluster-reboot simulation |
| Screen worker | Virtual backend + injected clock: pacing renders exactly when expected and not more; two-stage scene selection; night transitions including the midnight-wrapping window; the error scene on a render exception; a faulted screen after repeated SPI failure |
| Supervisor | SIGTERM blanks and sleeps panels; the watchdog restarts a killed worker |
| Status file | Atomicity (no partial read is ever observable), stable shape |
| End-to-end | Config in → PNGs out through the virtual backend against a fake Prometheus, asserting all four screens select the right scenes |
| **Hardware (manual, on the Pi)** | All four panels initialise; per-panel rotation and flip correct; SPI1 stable at 16 MHz; night sleep and wake at the boundary; 24-hour soak with no memory growth and no wedged worker; the cluster rebooted mid-run and the tunnel recovers |

**No test may sleep to wait for time to pass.** The clock is injected; night transitions, backoff and pacing are all tested by advancing it.

## 11. Definition of done for M2

- `uv run pytest` passes from a clean checkout with no hardware; `ruff check` and `ruff format --check` pass; CI green.
- `examples/rack.yaml` validates, and is the config the author's rack actually runs.
- The daemon renders all four screens correctly through the virtual backend against a fake Prometheus.
- The daemon runs on the Pi from that config, driving all four panels, with no server involved.
- Night mode sleeps and wakes the panels at the configured boundary.
- The hardware checklist in §10 has been walked, including the 24-hour soak.
- **`k8s_monitor.py` is stopped on the Pi.**

## 12. What M3 picks up

M3 (server and link) consumes: the config models in `ors-schema` — the server produces the same structure it pushes down; the status structure in §8, reported upstream verbatim; and the daemon's `config.py` entry point, which gains an "apply this snapshot atomically" path alongside "load this file".

No M2 code should need changing for M3 to exist. If M3 finds it does, that is a signal this interface was wrong and should be raised, not patched around.
