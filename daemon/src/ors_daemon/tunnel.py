from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import requests
from ors_schema.daemon import TunnelConfig

log = logging.getLogger(__name__)

Launcher = Callable[[list[str]], Any]
Discoverer = Callable[[TunnelConfig], str | None]
Probe = Callable[[str], bool]

_FAILURES_BEFORE_RELAUNCH = 2
_STOP_TIMEOUT = 5.0
"""How long a stopping kubectl gets, per signal, before the next one."""


def default_launcher(argv: list[str]) -> Any:
    # stdout is dropped -- port-forward narrates every connection it proxies --
    # but stderr is *inherited*, so the journal gets kubectl's own diagnosis of a
    # launch that fails: bad kubeconfig, RBAC denial, "unable to listen on port",
    # no such service. Discarding it made every launch failure look identical
    # from out here. Inheriting is also free of the `wait()` deadlock that a
    # `PIPE` would risk: nothing here ever has to drain it.
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL)


# Other components of the same charts. A name matching one of these is not the
# Prometheus server no matter what else it contains -- `prometheus-alertmanager`
# answers 200 on `/` and 404s every `/api/v1/query`, which is worse than
# forwarding nothing because the tunnel then reports healthy forever.
_NOT_THE_SERVER = (
    "alertmanager",
    "pushgateway",
    "operator",
    "exporter",  # node-exporter, blackbox-exporter, snmp-exporter
    "kube-state-metrics",
    "thanos",
    "grafana",
)
# What the two mainstream charts call the server: `-server` on
# prometheus-community/prometheus, `-prometheus` on kube-prometheus-stack
# (`<release>-<chart>-prometheus`).
_SERVER_SUFFIXES = ("-server", "-prometheus")


def _rank(name: str) -> int | None:
    """How much a service name looks like a Prometheus server. Lower is better.

    Order matters: a known-good *suffix* outranks the exclusions, because a chart
    installed under a release name like `prometheus-operator` produces
    `prometheus-operator-kube-p-prometheus` -- a real server whose name carries
    another component's word. The exclusions only decide between the leftovers,
    which is where "contains prom" used to pick the alertmanager.
    """
    name = name.lower()
    if "prom" not in name:
        # `-server` is a chart's word for its own main service, not Prometheus's:
        # without this, a namespace holding Loki and Mimir hands back `loki-server`.
        return None
    if name == "prometheus":
        return 0
    if name.endswith(_SERVER_SUFFIXES):
        return 1
    if any(token in name for token in _NOT_THE_SERVER):
        return None
    if name.endswith("-operated"):
        # The operator's headless service for the StatefulSet. It forwards fine,
        # but it is not what the chart documents, so it loses to a named service
        # and is taken only when the namespace offers nothing else.
        return 3
    return 2


def select_service(output: str) -> str | None:
    """Pick the Prometheus server out of `kubectl get svc -o name` output.

    Returns `None` when nothing in the namespace looks like one, which the caller
    treats as "not yet": no launch this cycle, nothing cached, and the next cycle
    asks again -- so a chart installed after the daemon started is picked up
    without a restart.

    Both the choice and the rejects are logged, because this is a guess about
    someone else's cluster and a wrong one has to be answerable from the journal.
    """
    names = [line.split("/")[-1].strip() for line in output.splitlines() if line.strip()]
    ranked = sorted(
        (rank, name) for rank, name in ((_rank(name), name) for name in names) if rank is not None
    )
    if not ranked:
        log.info(
            "no service here looks like a Prometheus server",
            extra={"candidates": ", ".join(names)},
        )
        return None
    chosen = ranked[0][1]
    log.info(
        "picked a service to forward",
        extra={"chosen": chosen, "rejected": ", ".join(n for n in names if n != chosen)},
    )
    return chosen


def default_discoverer(config: TunnelConfig) -> str | None:
    """Find the service in `namespace` that fronts the integration."""
    try:
        output = subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                config.kubeconfig_path,
                "get",
                "svc",
                "-n",
                config.namespace,
                "-o",
                "name",
            ],
            # Inherited, like the launcher's: `CalledProcessError` says only that
            # kubectl exited 1, while its stderr says "You must be logged in to
            # the server" or "namespace not found". Only stdout is piped here, so
            # there is nothing for a `wait` to deadlock against.
            timeout=10,
        ).decode()
    except Exception as exc:
        log.warning(
            "service discovery failed",
            extra={"namespace": config.namespace, "error": str(exc)},
        )
        return None
    return select_service(output)


def default_probe(url: str) -> bool:
    try:
        return requests.get(url, timeout=3.0).status_code < 500
    except Exception:
        return False


class Tunnel(threading.Thread):
    """Keeps one `kubectl port-forward` alive and *actually working*.

    A liveness check on the subprocess is not enough: after a cluster reboot the
    process usually survives while the tunnel underneath it is dead. So every
    cycle probes the local URL, and repeated probe failures tear the process
    down -- freeing the local port -- rather than waiting for it to exit.
    """

    def __init__(
        self,
        config: TunnelConfig,
        stop: threading.Event,
        probe: Probe | None = None,
        launcher: Launcher | None = None,
        discoverer: Discoverer | None = None,
        sleeper: Callable[[float], None] | None = None,
        interval: float = 5.0,
    ) -> None:
        super().__init__(name=f"tunnel-{config.namespace}", daemon=True)
        self._config = config
        self._stop = stop
        self._probe = probe or default_probe
        self._launch = launcher or default_launcher
        self._discover = discoverer or default_discoverer
        self._sleeper = sleeper or (lambda seconds: stop.wait(seconds))
        self._interval = interval
        self._process: Any | None = None
        self._service: str | None = None if config.service == "auto" else config.service
        self._failures = 0
        self.ready = threading.Event()
        self.base_url = f"http://localhost:{config.local_port}"

    def tick(self) -> None:
        """One supervision cycle: launch, probe, or tear down and relaunch.

        Total with respect to the injected callables. A launcher, discoverer or
        probe that raises is absorbed here rather than in `run`, because each
        one means something different -- no process, no service, no working
        tunnel -- and only here is there enough context to say which.
        """
        process = self._process
        returncode = None if process is None else process.poll()
        if process is None or returncode is not None:
            self.ready.clear()
            if process is not None:
                # The one number that says *why* a launch failed -- 1 for a
                # kubeconfig kubectl cannot read, for an RBAC denial, for a local
                # port already bound. Without it every failure mode is the same
                # info line every interval, forever.
                log.warning(
                    "kubectl exited, relaunching",
                    extra={"namespace": self._config.namespace, "returncode": returncode},
                )
            self._kill()
            self._start()
            return

        if self._probe_ok():
            self._failures = 0
            self.ready.set()
            return

        self._failures += 1
        self.ready.clear()
        if self._failures >= _FAILURES_BEFORE_RELAUNCH:
            log.warning(
                "tunnel probes failing, relaunching",
                extra={"namespace": self._config.namespace},
            )
            self._kill()
            self._failures = 0
            # Relaunched here rather than left to the next cycle, so recovering
            # from a wedged tunnel costs what recovering from a dead one does.
            self._start()

    def run(self) -> None:
        """Supervise until stopped. Nothing gets out of here.

        The same rule as the poller's loop, for the same reason: this is a
        daemon thread and nothing watches it, so an exception escaping `run`
        would leave every integration behind this tunnel polling a dead local
        port forever, with nothing anywhere saying why. `tick` already absorbs
        what the injected callables do; this catches what it cannot -- a
        launcher that returned an object whose `poll` raises, say.
        """
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # `ready` is what releases the integrations behind this tunnel
                    # to poll, and a cycle that blew up is no evidence the tunnel
                    # works. Keeping the previous value would let a pathological
                    # launcher leave it set with nothing behind it.
                    self.ready.clear()
                    log.exception(
                        "tunnel cycle failed", extra={"namespace": self._config.namespace}
                    )
                try:
                    self._sleeper(self._interval)
                except Exception:
                    # A caller-supplied sleeper is under no contract, and the
                    # default `stop.wait` cannot raise. Losing the pace is
                    # survivable; losing the thread is not.
                    log.exception("sleeper failed", extra={"namespace": self._config.namespace})
                    self._stop.wait(self._interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop supervising and take the tunnel down. Idempotent, and safe before `start`.

        The stop event goes first, and it is the whole point: without it a
        `shutdown` on a live `run` only kills the child, and the next cycle
        forwards again. A supervisor that shut this down and joined would get a
        thread still re-forwarding -- and at interpreter exit that daemon thread
        dies where it stands, orphaning a `kubectl` that still holds the local
        port. Setting it also ends the default sleeper's `stop.wait` at once, so
        the join is quick rather than up to an interval long.
        """
        self._stop.set()
        self.ready.clear()
        self._kill()

    def _probe_ok(self) -> bool:
        try:
            return bool(self._probe(f"{self.base_url}/"))
        except Exception as exc:
            # `default_probe` swallows everything itself, but an injected probe
            # is under no contract -- and a probe that blew up is no evidence
            # the tunnel works, so it counts as a failure like any other.
            log.warning(
                "tunnel probe raised",
                extra={"namespace": self._config.namespace, "error": str(exc)},
            )
            return False

    def _start(self) -> None:
        if self._service is None:
            self._service = self._discovered()
        if not self._service:
            log.warning("no service to forward", extra={"namespace": self._config.namespace})
            return
        argv = [
            "kubectl",
            "--kubeconfig",
            self._config.kubeconfig_path,
            "port-forward",
            "-n",
            self._config.namespace,
            f"svc/{self._service}",
            f"{self._config.local_port}:{self._config.remote_port}",
        ]
        log.info("starting tunnel", extra={"argv": " ".join(argv)})
        try:
            process = self._launch(argv)
        except Exception as exc:
            # `kubectl` missing from PATH, a fork refused under memory pressure.
            # The next cycle tries again: nothing here is worth a dead thread.
            log.warning(
                "could not start the tunnel",
                extra={"namespace": self._config.namespace, "error": str(exc)},
            )
            return
        self._process = process

    def _discovered(self) -> str | None:
        try:
            return self._discover(self._config)
        except Exception as exc:  # `default_discoverer` cannot; an injected one can
            log.warning(
                "service discovery raised",
                extra={"namespace": self._config.namespace, "error": str(exc)},
            )
            return None

    def _kill(self) -> None:
        """Stop the subprocess and free the local port. Raises nothing.

        Dropping the reference first is what keeps a failure here from leaving
        the tunnel believing it still has a live process: whatever happens
        below, the next cycle sees `None` and launches a new one.

        SIGTERM, then SIGKILL for a `kubectl` wedged past `_STOP_TIMEOUT`.
        Both waits matter, and the second is the one that is easy to miss:
        `Popen.kill()` only *sends* SIGKILL and returns, so without waiting the
        relaunch can race the dying process for the local port and lose. It
        also reaps the child -- an unwaited one stays a zombie and earns a
        `ResourceWarning` when the `Popen` is collected.
        """
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=_STOP_TIMEOUT)
            return
        except Exception as exc:
            log.warning(
                "kubectl did not stop on SIGTERM, killing it",
                extra={"namespace": self._config.namespace, "error": str(exc)},
            )
        try:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT)
        except Exception as exc:
            # Nothing else to try. The local port may still be held, in which
            # case the next launch fails to bind and says so in kubectl's own
            # words -- which is more than this could say about it.
            log.warning(
                "could not kill kubectl",
                extra={"namespace": self._config.namespace, "error": str(exc)},
            )
