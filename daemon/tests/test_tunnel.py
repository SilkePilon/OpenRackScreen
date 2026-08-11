import threading

from ors_daemon.tunnel import Tunnel
from ors_schema.daemon import TunnelConfig

CONFIG = TunnelConfig(
    kubeconfig="/tmp/kubeconfig", namespace="monitoring", remote_port=9090, local_port=19090
)


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode

    def die(self):
        self._returncode = 1


class Harness:
    def __init__(self, probe_results, service="prometheus"):
        self.processes = []
        self.argvs = []
        self.probe_results = list(probe_results)
        self.service = service
        self.discoveries = 0

    def launcher(self, argv):
        self.argvs.append(argv)
        process = FakeProcess()
        self.processes.append(process)
        return process

    def discoverer(self, config):
        self.discoveries += 1
        return self.service

    def probe(self, url):
        return self.probe_results.pop(0) if self.probe_results else True


def make(harness):
    return Tunnel(
        config=CONFIG,
        stop=threading.Event(),
        probe=harness.probe,
        launcher=harness.launcher,
        discoverer=harness.discoverer,
        sleeper=lambda seconds: None,
    )


def test_base_url_points_at_the_local_port():
    assert make(Harness([True])).base_url == "http://localhost:19090"


def test_the_first_tick_launches_kubectl_with_the_configured_arguments():
    harness = Harness([True])
    tunnel = make(harness)
    tunnel.tick()

    argv = harness.argvs[0]
    assert argv[0] == "kubectl"
    assert "--kubeconfig" in argv and "/tmp/kubeconfig" in argv
    assert "-n" in argv and "monitoring" in argv
    assert "svc/prometheus" in argv
    assert "19090:9090" in argv


def test_ready_is_set_only_once_a_probe_succeeds():
    harness = Harness([False, True])
    tunnel = make(harness)

    tunnel.tick()
    assert tunnel.ready.is_set() is False
    tunnel.tick()
    tunnel.tick()
    assert tunnel.ready.is_set() is True


def test_two_failed_probes_tear_the_tunnel_down_and_relaunch_it():
    harness = Harness([True, False, False, True])
    tunnel = make(harness)
    for _ in range(5):
        tunnel.tick()

    assert harness.processes[0].terminated is True
    assert len(harness.processes) >= 2, "a dead tunnel must be relaunched, not left alive"


def test_a_dead_process_clears_ready_and_relaunches():
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    tunnel.tick()
    assert tunnel.ready.is_set() is True

    harness.processes[0].die()
    tunnel.tick()
    assert tunnel.ready.is_set() is False
    assert len(harness.processes) == 2


def test_a_service_name_is_discovered_once_and_then_reused():
    harness = Harness([True] * 6)
    tunnel = make(harness)
    for _ in range(3):
        tunnel.tick()

    assert harness.discoveries == 1


def test_an_explicit_service_name_skips_discovery():
    harness = Harness([True, True])
    tunnel = Tunnel(
        config=CONFIG.model_copy(update={"service": "prom-server"}),
        stop=threading.Event(),
        probe=harness.probe,
        launcher=harness.launcher,
        discoverer=harness.discoverer,
        sleeper=lambda seconds: None,
    )
    tunnel.tick()

    assert harness.discoveries == 0
    assert "svc/prom-server" in harness.argvs[0]


def test_a_service_that_cannot_be_discovered_does_not_launch_or_crash():
    harness = Harness([True])
    harness.service = None
    tunnel = make(harness)
    tunnel.tick()

    assert harness.argvs == []
    assert tunnel.ready.is_set() is False


def test_stopping_terminates_the_subprocess():
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    tunnel.shutdown()

    assert harness.processes[0].terminated is True
