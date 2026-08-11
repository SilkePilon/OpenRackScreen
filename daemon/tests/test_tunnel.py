import logging
import subprocess
import threading

import pytest
from ors_daemon.tunnel import _STOP_TIMEOUT as STOP_TIMEOUT
from ors_daemon.tunnel import Tunnel, select_service
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


def test_one_failed_probe_is_not_enough_to_tear_a_tunnel_down():
    """A single miss is a slow cluster, not a dead tunnel: relaunching on it
    would cost the local port and a kubectl start every interval."""
    harness = Harness([False, True, True])
    tunnel = make(harness)
    for _ in range(3):
        tunnel.tick()

    assert harness.processes[0].terminated is False
    assert len(harness.processes) == 1


def test_two_failed_probes_tear_the_tunnel_down_and_relaunch_it():
    harness = Harness([True, False, False, True])
    tunnel = make(harness)
    for _ in range(5):
        tunnel.tick()

    assert harness.processes[0].terminated is True
    assert len(harness.processes) >= 2, "a dead tunnel must be relaunched, not left alive"


def test_a_dead_process_is_relaunched():
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    tunnel.tick()
    assert len(harness.processes) == 1

    harness.processes[0].die()
    tunnel.tick()
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


class TimingProcess(StubbornProcess):
    """A stubborn kubectl that also records how long each wait was given."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        return super().wait(timeout)


def test_a_shutdown_left_to_itself_gives_each_signal_the_full_stop_timeout():
    harness = Harness([True])
    tunnel = make(harness, launcher=launching(harness, TimingProcess))
    tunnel.tick()
    tunnel.shutdown()

    assert harness.processes[0].timeouts == [STOP_TIMEOUT, STOP_TIMEOUT]


def test_a_hurried_shutdown_spends_no_more_than_it_was_given():
    """A caller under a deadline this knows nothing about, which is the ordinary
    case: the supervisor still has four panels to blank when this returns, and
    `TimeoutStopSec` is counting the whole time. A `kubectl` that outlives the
    daemon by a moment holds a port nobody is about to bind; a panel left lit
    holds the rack until someone unplugs it."""
    harness = Harness([True])
    tunnel = make(harness, launcher=launching(harness, TimingProcess))
    tunnel.tick()
    tunnel.shutdown(timeout=1.0)

    process = harness.processes[0]
    assert sum(process.timeouts) <= 1.0, "both waits together, not each"
    assert process.killed is True, "and it is still killed, not merely asked"


def test_a_shutdown_with_nothing_left_still_signals_the_child():
    """What a blown deadline costs is the *confirmation* that kubectl died, not
    the signals: both are sent, and neither is waited on."""
    harness = Harness([True])
    tunnel = make(harness, launcher=launching(harness, TimingProcess))
    tunnel.tick()
    tunnel.shutdown(timeout=0.0)

    process = harness.processes[0]
    assert (process.terminated, process.killed) == (True, True)
    assert process.timeouts == [0.0, 0.0]


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
    assert harness.processes == [], "the launch that raised handed back nothing"

    tunnel.tick()
    tunnel.tick()
    assert len(harness.processes) == 1, "and the next cycle tried again"


def test_a_probe_that_raises_counts_as_a_failed_probe():
    harness = Harness([])
    tunnel = make(harness, probe=boom)
    for _ in range(3):
        tunnel.tick()

    assert harness.processes[0].terminated is True


def test_a_discoverer_that_raises_does_not_launch_or_crash():
    harness = Harness([True])
    tunnel = make(harness, discoverer=boom)
    tunnel.tick()

    assert harness.argvs == []


def test_run_leaves_no_tunnel_behind_when_it_is_stopped():
    harness = Harness([True, True])
    stop = threading.Event()
    tunnel = make(harness, stop=stop, sleeper=lambda seconds: stop.set())
    tunnel.run()

    assert harness.processes[0].terminated is True


def test_run_survives_a_sleeper_that_raises():
    harness = Harness([True, True])
    stop = threading.Event()

    def sleeper(seconds):
        stop.set()  # so the fallback wait returns at once and this test never sleeps
        raise RuntimeError("sleeper failed")

    tunnel = make(harness, stop=stop, sleeper=sleeper)
    tunnel.run()

    assert harness.processes[0].terminated is True


def test_run_keeps_turning_when_a_cycle_raises():
    # `tick` absorbs what the injected callables do; this is what it cannot --
    # a launcher that handed back an object whose `poll` raises. An exception
    # out of `run` would end the supervision of this tunnel until the process
    # restarted, with nothing anywhere saying why, so the loop goes round again.
    class PollExploder(FakeProcess):
        polls = 0

        def poll(self):
            self.polls += 1
            if self.polls >= 2:
                raise OSError("no such process")
            return self._returncode

    harness = Harness([True] * 4)
    stop = threading.Event()
    seen = []

    def sleeper(seconds):
        seen.append(len(harness.processes))
        if len(seen) >= 3:
            stop.set()

    tunnel = make(harness, stop=stop, sleeper=sleeper, launcher=launching(harness, PollExploder))
    tunnel.run()

    assert seen == [1, 1, 1], "the raising cycle was survived, not answered with a relaunch"


# --- reaching the right cluster ----------------------------------------------

# Real `kubectl get svc -o name` output. The release name is the first segment
# and the chart names the rest, which is why "contains prom" is not a rule: on
# both of the mainstream charts the *alertmanager* sorts first among the names
# containing "prom".

KUBE_PROMETHEUS_STACK = """\
service/alertmanager-operated
service/kps-grafana
service/kps-kube-prometheus-stack-alertmanager
service/kps-kube-prometheus-stack-operator
service/kps-kube-prometheus-stack-prometheus
service/kps-kube-state-metrics
service/kps-prometheus-node-exporter
service/prometheus-operated
"""

# The same chart under `helm install prometheus ...`, where the release name is
# itself "prometheus" -- the form every kube-prometheus-stack README shows.
KUBE_PROMETHEUS_STACK_DEFAULT_RELEASE = """\
service/alertmanager-operated
service/prometheus-grafana
service/prometheus-kube-prometheus-alertmanager
service/prometheus-kube-prometheus-operator
service/prometheus-kube-prometheus-prometheus
service/prometheus-kube-state-metrics
service/prometheus-operated
service/prometheus-prometheus-node-exporter
"""

PROMETHEUS_CHART = """\
service/prometheus-alertmanager
service/prometheus-alertmanager-headless
service/prometheus-kube-state-metrics
service/prometheus-prometheus-node-exporter
service/prometheus-prometheus-pushgateway
service/prometheus-server
"""


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (KUBE_PROMETHEUS_STACK, "kps-kube-prometheus-stack-prometheus"),
        (KUBE_PROMETHEUS_STACK_DEFAULT_RELEASE, "prometheus-kube-prometheus-prometheus"),
        (PROMETHEUS_CHART, "prometheus-server"),
        ("service/prometheus\n", "prometheus"),
        # A hand-rolled Deployment plus Service, the third common shape.
        ("service/grafana\nservice/prometheus-server\n", "prometheus-server"),
    ],
)
def test_the_prometheus_server_is_picked_out_of_a_real_listing(listing, expected):
    assert select_service(listing) == expected


@pytest.mark.parametrize(
    "listing",
    [
        "",
        "service/grafana\nservice/loki\n",
        # Alertmanager alone is not a Prometheus, and forwarding it is worse than
        # forwarding nothing: its `/` answers 200 and every query 404s.
        "service/prometheus-alertmanager\nservice/prometheus-alertmanager-headless\n",
        # `-server` is a chart's word for its main service, not Prometheus's own:
        # a namespace of Loki and Mimir has nothing to forward here.
        "service/loki-server\nservice/mimir-server\nservice/grafana\n",
    ],
)
def test_a_listing_with_no_prometheus_server_picks_nothing(listing):
    assert select_service(listing) is None


def test_the_headless_operated_service_is_only_a_last_resort():
    # `prometheus-operated` is the operator's headless service for the StatefulSet.
    # It works, but the named one is what the chart documents, so it is taken only
    # when the listing offers nothing else.
    assert select_service("service/prometheus-operated\n") == "prometheus-operated"


def test_the_chosen_and_rejected_candidates_are_logged(caplog):
    with caplog.at_level(logging.INFO, logger="ors_daemon.tunnel"):
        select_service(PROMETHEUS_CHART)

    record = next(r for r in caplog.records if getattr(r, "chosen", None))
    assert record.chosen == "prometheus-server"
    assert "prometheus-alertmanager" in record.rejected


def test_discovery_is_retried_after_a_namespace_offers_nothing():
    # Only a *successful* pick is cached, so a chart installed after the daemon
    # started is picked up on the next cycle rather than needing a restart.
    harness = Harness([True, True])
    harness.service = None
    tunnel = make(harness)
    tunnel.tick()
    tunnel.tick()

    assert harness.discoveries == 2
    assert harness.argvs == []


def test_the_kubeconfig_reaches_kubectl_with_its_home_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/home/rack")
    harness = Harness([True])
    tunnel = make(harness, config=CONFIG.model_copy(update={"kubeconfig": "~/k8s-monitor.yaml"}))
    tunnel.tick()

    argv = harness.argvs[0]
    assert "/home/rack/k8s-monitor.yaml" in argv
    assert "~/k8s-monitor.yaml" not in argv


def test_a_dead_process_logs_the_exit_code_it_died_with(caplog):
    # Without this, a kubectl that dies at launch -- bad kubeconfig, RBAC denial,
    # local port already bound -- looks exactly like every other failure: one
    # info line a cycle, saying nothing about why.
    harness = Harness([True, True])
    tunnel = make(harness)
    tunnel.tick()
    harness.processes[0].die()

    with caplog.at_level(logging.WARNING, logger="ors_daemon.tunnel"):
        tunnel.tick()

    dead = [r for r in caplog.records if getattr(r, "returncode", None) is not None]
    assert [r.returncode for r in dead] == [1]
    assert dead[0].levelno == logging.WARNING


def test_a_probe_failure_relaunches_in_the_same_cycle_a_death_would():
    # The dead-process branch tears down and relaunches in one tick; this one used
    # to tear down and wait a whole interval, so recovering from a wedged tunnel
    # cost strictly more than recovering from a dead one.
    harness = Harness([True, False, False])
    tunnel = make(harness)
    for _ in range(4):  # launch, probe ok, probe fails, probe fails -> relaunch
        tunnel.tick()

    assert harness.processes[0].terminated is True
    assert len(harness.processes) == 2


def test_a_shutdown_during_a_run_does_not_relaunch():
    # A supervisor that calls `shutdown()` and joins must get a stopped thread,
    # not one that forwards again on the next tick and orphans a kubectl still
    # holding the local port when the interpreter exits.
    harness = Harness([True] * 6)
    stop = threading.Event()
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            tunnel.shutdown()
        if len(sleeps) >= 4:
            stop.set()  # a fuse, so a `shutdown` that does not stop cannot hang this test

    tunnel = make(harness, stop=stop, sleeper=sleeper)
    tunnel.run()

    assert len(harness.processes) == 1
    assert harness.processes[0].terminated is True
    assert stop.is_set() is True


def test_shutdown_is_safe_before_a_start_and_twice_over():
    harness = Harness([True])
    tunnel = make(harness)
    tunnel.shutdown()
    tunnel.shutdown()

    assert harness.processes == []


def test_a_tunnel_can_be_joined():
    # See the matching poller test: storing the stop event as `self._stop` shadows
    # `threading.Thread._stop`, which `join` calls, and every join raises.
    harness = Harness([True])
    stop = threading.Event()
    stop.set()
    tunnel = Tunnel(
        config=CONFIG,
        stop=stop,
        probe=harness.probe,
        launcher=harness.launcher,
        discoverer=harness.discoverer,
        sleeper=lambda seconds: None,
    )
    tunnel.start()
    tunnel.join(timeout=5.0)

    assert not tunnel.is_alive()
