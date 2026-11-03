"""Graceful shutdown of the agent (SIGTERM / SIGINT).

An orchestrator stops a worker with SIGTERM (`docker stop`, systemd, an ECS or Kubernetes
redeploy, an autoscaling scale-in). Killing the agent there would cut a plan or an apply in the
middle — an apply half done, its state lock still held — so the agent drains instead:

1. the first signal **requests** shutdown: the agent stops claiming jobs, and lets the jobs it is
   running finish, for up to `STACKD_SHUTDOWN_TIMEOUT_SECONDS`;
2. past that budget, or on a second signal (**forced**), it **aborts**: every job still running
   is reported failed with the reason, then every command still running is killed (and any
   command a job would start afterwards is killed at once), so the worker exits without leaving a
   run behind that only the worker-lost reaper would end.

The runners register each command's kill function here while it runs (`command()`).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

from agent.logging import get_logger

log = get_logger()


class Shutdown:
    def __init__(self) -> None:
        self._requested = threading.Event()
        self._forced = threading.Event()
        self._aborting = threading.Event()
        self._lock = threading.Lock()
        self._kills: dict[int, Callable[[], None]] = {}
        self._next_id = 0

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def forced(self) -> bool:
        return self._forced.is_set()

    @property
    def aborting(self) -> bool:
        return self._aborting.is_set()

    def request(self) -> None:
        """Stop claiming and drain. A second request forces the stop: the drain ends at once."""
        if self._requested.is_set():
            log.warning(
                "second stop signal: aborting the running jobs",
                extra={"event": "agent.shutdown_forced"},
            )
            self._forced.set()
            return
        log.info("stop requested: draining", extra={"event": "agent.shutdown_requested"})
        self._requested.set()

    def handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        self.request()

    def install(self) -> None:
        """Route SIGTERM and SIGINT here. Main thread only (a `signal` module rule)."""
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

    def wait(self, seconds: float) -> bool:
        """An interruptible sleep: returns True as soon as shutdown is requested."""
        return self._requested.wait(seconds)

    def wait_forced(self, seconds: float) -> bool:
        return self._forced.wait(seconds)

    def abort(self) -> None:
        """Kill every command still running; a command started from now on is killed at once."""
        self._requested.set()
        self._aborting.set()
        with self._lock:
            kills = list(self._kills.values())
        for kill in kills:
            try:
                kill()
            except Exception as exc:  # noqa: BLE001 — one failed kill must not spare the others
                log.warning(
                    "kill on abort failed", extra={"event": "agent.abort_error", "error": str(exc)}
                )

    @contextmanager
    def command(self, kill: Callable[[], None]) -> Iterator[None]:
        """Register a running command's kill function for its lifetime."""
        with self._lock:
            key = self._next_id
            self._next_id += 1
            self._kills[key] = kill
        try:
            if self._aborting.is_set():
                kill()
            yield
        finally:
            with self._lock:
                self._kills.pop(key, None)


# The agent's one instance (a module attribute, so tests can swap it for a fresh one).
SHUTDOWN = Shutdown()
