import subprocess
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


def make(harness, **overrides):
    kwargs = {
        "config": CONFIG,
        "stop": threading.Event(),
        "probe": harness.probe,
        "launcher": harness.launcher,
        "discoverer": harness.discoverer,
        "sleeper": lambda seconds: None,
    }
    return Tunnel(**(kwargs | overrides))


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


# --- teardown really frees the local port ------------------------------------


class StubbornProcess(FakeProcess):
    """A kubectl that ignores SIGTERM, like one wedged in the API-server dial.

    `wait` raises until something actually kills it, which is what
    `subprocess.Popen.wait(timeout=...)` does: it raises `TimeoutExpired`.
    """

    def __init__(self):
        super().__init__()
        self.waits = 0

    def terminate(self):
        self.terminated = True  # ... and nothing else happens

    def wait(self, timeout=None):
        self.waits += 1
        if self._returncode is None:
            raise subprocess.TimeoutExpired("kubectl", timeout)
        return self._returncode


class ExplodingProcess(FakeProcess):
    """Every way of stopping it fails. The tunnel must not end up holding it."""

    def terminate(self):
        raise OSError("no such process")

    def kill(self):
        raise OSError("no such process")

    def wait(self, timeout=None):
        raise OSError("no such process")


def launching(harness, factory):
    """The harness's launcher, but handing out `factory()` instead of a `FakeProcess`."""

    def launcher(argv):
        harness.argvs.append(argv)
        process = factory()
        harness.processes.append(process)
        return process

    return launcher


def test_a_process_that_ignores_sigterm_is_killed_and_reaped():
    harness = Harness([True])
    tunnel = make(harness, launcher=launching(harness, StubbornProcess))
    tunnel.tick()
    tunnel.shutdown()

    process = harness.processes[0]
    assert process.killed is True
    # Twice: once for the SIGTERM that timed out, once to reap the kill. The
    # second is what frees the local port before the next launch tries to bind
    # it -- SIGKILL only queues the death, it does not wait for it.
    assert process.waits == 2


def test_a_process_that_cannot_be_stopped_at_all_is_still_let_go_of():
    harness = Harness([True, True])
    tunnel = make(harness, launcher=launching(harness, ExplodingProcess))
    tunnel.tick()
    harness.processes[0].die()
    tunnel.tick()

    assert len(harness.processes) == 2, "a process it cannot kill must not block a relaunch"


# --- nothing gets out of a supervision cycle ---------------------------------


def boom(*_args):
    raise RuntimeError("boom")


def test_a_launcher_that_raises_is_survived_and_retried():
    harness = Harness([True, True])
    launches = []

    def launcher(argv):
        launches.append(argv)
        if len(launches) == 1:
            raise OSError("kubectl: not found")
        return harness.launcher(argv)

    tunnel = make(harness, launcher=launcher)
    tunnel.tick()
    assert tunnel.ready.is_set() is False

    tunnel.tick()
    tunnel.tick()
    assert tunnel.ready.is_set() is True


def test_a_probe_that_raises_counts_as_a_failed_probe():
    harness = Harness([])
    tunnel = make(harness, probe=boom)
    for _ in range(3):
        tunnel.tick()

    assert tunnel.ready.is_set() is False
    assert harness.processes[0].terminated is True


def test_a_discoverer_that_raises_does_not_launch_or_crash():
    harness = Harness([True])
    tunnel = make(harness, discoverer=boom)
    tunnel.tick()

    assert harness.argvs == []
    assert tunnel.ready.is_set() is False


def test_run_leaves_no_tunnel_behind_when_it_is_stopped():
    harness = Harness([True, True])
    stop = threading.Event()
    tunnel = make(harness, stop=stop, sleeper=lambda seconds: stop.set())
    tunnel.run()

    assert harness.processes[0].terminated is True
    assert tunnel.ready.is_set() is False


def test_run_survives_a_sleeper_that_raises():
    harness = Harness([True, True])
    stop = threading.Event()

    def sleeper(seconds):
        stop.set()  # so the fallback wait returns at once and this test never sleeps
        raise RuntimeError("sleeper failed")

    tunnel = make(harness, stop=stop, sleeper=sleeper)
    tunnel.run()

    assert harness.processes[0].terminated is True
