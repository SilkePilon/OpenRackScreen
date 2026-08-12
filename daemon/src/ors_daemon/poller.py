from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ors_daemon.clock import Clock
from ors_daemon.integrations import Integration, IntegrationError
from ors_daemon.snapshot import SnapshotStore

log = logging.getLogger(__name__)


class Poller(threading.Thread):
    """Owns everything an integration deliberately does not: interval, backoff, health.

    One thread per integration, touching nothing but its own integration and the
    snapshot. Because all the policy lives here, a second integration is one
    class and a config model -- the loop and its semantics are already written.
    """

    def __init__(
        self,
        integration: Integration,
        store: SnapshotStore,
        interval: float,
        stop: threading.Event,
        clock: Clock,
        backoff_cap: float = 60.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(name=f"poller-{integration.name}", daemon=True)
        self._integration = integration
        self._store = store
        self._interval = interval
        self._stop_event = stop
        self._clock = clock
        self._backoff_cap = backoff_cap
        # Waiting on the stop event rather than sleeping is what makes SIGTERM
        # feel immediate: a minute-long backoff would otherwise have to be sat
        # out in full before the loop noticed it had been asked to leave.
        self._sleeper = sleeper or stop.wait
        self._backing_off = False
        self.next_delay = interval
        self.heartbeat = 0.0

    def poll_once(self) -> None:
        """One cycle: fetch, publish or record the failure, and set the next delay.

        Total with respect to the integration -- every way `open` and `poll` can
        fail ends as health on the snapshot -- but not with respect to the
        store, whose failures are `run`'s problem. That keeps this usable as a
        one-shot (a CLI dry run) where a broken store should be heard, not
        quietly logged.
        """
        self.heartbeat = started = time.monotonic()
        try:
            # Idempotent, and `poll` calls it too; doing it here is what puts a
            # failed connect -- M5's qBittorrent login against a service that
            # has not come up yet -- on the same path as a failed fetch, rather
            # than off the end of the thread.
            self._integration.open()
            fields = self._integration.poll()
        except IntegrationError as exc:
            self._failed(str(exc))
        except Exception as exc:  # an integration bug must not take the daemon down
            log.exception("integration raised", extra={"integration": self._integration.name})
            self._failed(f"{type(exc).__name__}: {exc}")
        else:
            latency_ms = (time.monotonic() - started) * 1000.0
            self._store.put(self._integration.name, fields, latency_ms, self._clock())
            self._backing_off = False
            self.next_delay = self._interval

    def run(self) -> None:
        """Poll until stopped. Nothing gets out of here.

        An exception escaping `run` would end this integration until the process
        restarts, and nothing would report it: the supervisor's watchdog reads
        heartbeats, and a dead thread's heartbeat looks exactly like a wedged
        one only after the timeout -- for a poller, which nothing watches, not
        even that. So the loop body, the wait and the close are each guarded.
        """
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as exc:
                    # Only the store can get here. Try to record it as a failure
                    # anyway: a store that publishes once and then breaks would
                    # otherwise leave health reading `healthy` with a frozen
                    # version forever -- a panel showing confident numbers that
                    # stopped updating, with nothing on the snapshot saying so.
                    name = self._integration.name
                    log.exception("poll cycle failed", extra={"integration": name})
                    try:
                        self._failed(f"{type(exc).__name__}: {exc}")
                    except Exception:
                        # The store is refusing both paths. Back off anyway, so a
                        # broken store cannot pin the loop at its fastest pace.
                        self.next_delay = min(
                            self._backoff_cap,
                            self.next_delay * 2 if self._backing_off else self._interval,
                        )
                        self._backing_off = True
                try:
                    self._sleeper(self.next_delay)
                except Exception:
                    # A caller-supplied sleeper is not covered by any contract, and
                    # the default `stop.wait` cannot raise. Losing the pace is
                    # survivable; losing the thread is not.
                    log.exception("sleeper failed", extra={"integration": self._integration.name})
                    self._stop_event.wait(self.next_delay)
        finally:
            try:
                self._integration.close()
            except Exception:  # contract says it raises nothing; shutdown believes nobody
                log.warning(
                    "closing the integration failed",
                    extra={"integration": self._integration.name},
                )

    def _failed(self, reason: str) -> None:
        self._store.fail(self._integration.name, reason, self._clock())
        # The first retry after any success is prompt -- one plain interval --
        # and only a failure that follows a failure doubles. Doubling the
        # already-capped delay rather than raising the interval to a power keeps
        # this bounded: a source down overnight would otherwise reach an
        # exponent that cannot be turned into a float at all.
        delay = self.next_delay * 2 if self._backing_off else self._interval
        self._backing_off = True
        self.next_delay = min(self._backoff_cap, delay)
        log.warning(
            "poll failed",
            extra={
                "integration": self._integration.name,
                "reason": reason,
                "retry_in": self.next_delay,
            },
        )
