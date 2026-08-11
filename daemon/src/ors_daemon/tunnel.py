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


def default_launcher(argv: list[str]) -> Any:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_discoverer(config: TunnelConfig) -> str | None:
    """Find a service whose name looks like the integration it fronts."""
    try:
        output = subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                config.kubeconfig,
                "get",
                "svc",
                "-n",
                config.namespace,
                "-o",
                "name",
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode()
    except Exception as exc:
        log.warning(
            "service discovery failed",
            extra={"namespace": config.namespace, "error": str(exc)},
        )
        return None
    for line in output.splitlines():
        name = line.split("/")[-1].strip()
        if "prom" in name.lower():
            return name
    return None


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
        if self._process is None or self._process.poll() is not None:
            self.ready.clear()
            self._kill()
            self._start()
            return

        if self._probe(f"{self.base_url}/"):
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

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self.tick()
                self._sleeper(self._interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.ready.clear()
        self._kill()

    def _start(self) -> None:
        if self._service is None:
            self._service = self._discover(self._config)
        if not self._service:
            log.warning("no service to forward", extra={"namespace": self._config.namespace})
            return
        argv = [
            "kubectl",
            "--kubeconfig",
            self._config.kubeconfig,
            "port-forward",
            "-n",
            self._config.namespace,
            f"svc/{self._service}",
            f"{self._config.local_port}:{self._config.remote_port}",
        ]
        log.info("starting tunnel", extra={"argv": " ".join(argv)})
        self._process = self._launch(argv)

    def _kill(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                log.warning("could not kill kubectl", extra={"namespace": self._config.namespace})
        finally:
            self._process = None
