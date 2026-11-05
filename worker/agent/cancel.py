"""Stopping a job on request (SPECS §4.1, §7.1).

The API asks through the heartbeat (`cancel_job`); the heartbeat thread flips the job's
`CancelToken`; the job's own thread — which owns the subprocess — is interrupted. The stop is
graceful: SIGINT to the tool (tofu finishes the operation in flight, releases its state lock and
exits), and only after a grace period SIGKILL. Whatever ends the job then raises `JobCanceled`, which
`_run_job` reports as `job_canceled`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# After the interrupt, how long the tool gets to stop by itself before it is killed. tofu needs
# time to finish a resource it is creating; killing it earlier risks a partial resource nobody
# recorded.
DEFAULT_GRACE_SECONDS = 120


class JobCanceled(BaseException):  # noqa: N818 — not an error
    """Raised in the job's thread once it was stopped on request. A `BaseException` on purpose: the
    many `except Exception` around hooks and stages must not turn a cancel into a failure."""


class CancelToken:
    """One per job. `request()` comes from the heartbeat thread, everything else from the job's."""

    def __init__(self, grace: float = DEFAULT_GRACE_SECONDS) -> None:
        self._grace = grace
        self._lock = threading.Lock()
        self._requested = False
        self._disarmed = False
        self._interrupted = False
        self._interrupt: Callable[[], None] | None = None
        self._kill: Callable[[], None] | None = None
        self._timer: threading.Timer | None = None

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def should_stop(self) -> bool:
        """A stop was asked for and still applies (see `disarm`)."""
        with self._lock:
            return self._requested and not self._disarmed

    @property
    def interrupted(self) -> bool:
        """The signal reached a live process: its result is a stop, not an outcome."""
        return self._interrupted

    def request(self) -> None:
        with self._lock:
            if self._requested:
                return
            self._requested = True
            if not self._disarmed:
                self._signal_locked()

    def disarm(self) -> None:
        """The point of no return passed (the apply succeeded): what is left is bookkeeping the run
        needs finished — recording the outputs — so a later request no longer stops the job."""
        with self._lock:
            self._disarmed = True

    def attach(self, interrupt: Callable[[], None], kill: Callable[[], None]) -> None:
        """A process is now running: how to interrupt it and how to kill it. A stop already asked
        for is delivered at once."""
        with self._lock:
            self._interrupt, self._kill = interrupt, kill
            if self._requested and not self._disarmed:
                self._signal_locked()

    def detach(self) -> None:
        """The process is over (call before reaping it, so a late signal can't reach a reused pid)."""
        with self._lock:
            self._interrupt = self._kill = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _signal_locked(self) -> None:
        if self._interrupt is None or self._interrupted:
            return
        self._interrupted = True
        self._interrupt()
        kill = self._kill
        if kill is not None:
            self._timer = threading.Timer(self._grace, self._escalate, args=(kill,))
            self._timer.daemon = True
            self._timer.start()

    def _escalate(self, kill: Callable[[], None]) -> None:
        with self._lock:
            if self._kill is kill:  # still the same process: it ignored the interrupt
                kill()


_local = threading.local()


def bind(token: CancelToken | None) -> None:
    """Make `token` the current job's (per thread: each job runs on its own)."""
    _local.token = token


def current() -> CancelToken | None:
    return getattr(_local, "token", None)


class JobRegistry:
    """The jobs this worker is executing, by id, so a `cancel_job` finds its job. A cancel can beat
    the job's registration (the claim answered, the heartbeat thread was faster): it is remembered
    and applied when the job registers."""

    _EARLY_TTL = 3600.0

    def __init__(self, grace: float = DEFAULT_GRACE_SECONDS) -> None:
        self._grace = grace
        self._lock = threading.Lock()
        self._jobs: dict[str, CancelToken] = {}
        self._early: dict[str, float] = {}

    def register(self, job_id: str) -> CancelToken:
        token = CancelToken(self._grace)
        with self._lock:
            self._jobs[job_id] = token
            early = self._early.pop(job_id, None) is not None
        if early:
            token.request()
        return token

    def unregister(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Ask the job to stop. False when it isn't (or isn't yet) running here."""
        with self._lock:
            token = self._jobs.get(job_id)
            if token is None:
                now = time.monotonic()
                self._early = {j: t for j, t in self._early.items() if now - t < self._EARLY_TTL}
                self._early[job_id] = now
        if token is None:
            return False
        token.request()
        return True
