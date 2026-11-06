"""Graceful shutdown of the agent (SPECS §7.1, "Drain").

The first SIGTERM/SIGINT starts a **drain**: the claim loop starts no new claim (a long-poll
already in flight is let through, and a job it returns is run — dropping a claimed job would leave
its run to the API's `worker_lost` reclaim), the heartbeat keeps beating, the jobs in flight run to
their end and report, then the agent exits 0.

Past `STACKD_DRAIN_TIMEOUT_SECONDS`, or on a second signal, the agent stops what is still running
through the cancel path (`cancel.JobRegistry.shutdown`: SIGINT to the tool, SIGKILL after
`STACKD_CANCEL_GRACE_SECONDS`), waits for those jobs to report, and exits 1.

The signal handler only flips plain attributes: it runs in the main thread between two bytecodes,
possibly while that thread holds a lock (logging, an Event's condition, the job registry), so it
takes none. A supervisor thread polls them and does the logging and the stopping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from agent.logging import get_logger

log = get_logger()

# Default drain timeout: the API's apply window — the apply budget (`stackd_apply_timeout_seconds`,
# 15 min) plus its lost-worker grace (5 min) = 20 min. An apply started just before the signal can
# use its whole budget plus init, hooks and outputs and still finish; and past that window the API
# reclaims the run anyway (`worker_lost`, §3.7), so waiting longer buys nothing.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 1200
# After the fallback stop, on top of the cancel grace: time for the job to report its end.
REPORT_MARGIN_SECONDS = 30

Outcome = Literal["drained", "stopped", "abandoned"]


class Drain:
    """RUNNING → DRAINING (first signal) → FORCED (second signal). Plain attributes only: written
    by the signal handler, read by the claim loop and the supervisor."""

    def __init__(self, timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.draining = False
        self.forced = False
        self.signal: int | None = None
        self.claims_stopped = False  # the claim loop is over: no job can start any more

    def on_signal(self, signum: int, _frame: object = None) -> None:
        """The SIGTERM/SIGINT handler: first → drain, second → stop now."""
        self.signal = signum
        if self.draining:
            self.forced = True
        else:
            self.draining = True

    def sleep(self, seconds: float, step: float = 0.2) -> None:
        """`time.sleep` that returns as soon as a drain starts."""
        end = time.monotonic() + seconds
        while not self.draining:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(step, left))


def supervise(
    drain: Drain,
    *,
    in_flight: Callable[[], int],
    stop_all: Callable[[], int],
    cancel_grace: float,
    poll: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    """Wait for a drain to start, then for it to end. `drained`: every job finished and reported;
    `stopped`: the rest were stopped through the cancel path and have reported; `abandoned`: some
    job still had not ended `cancel_grace` + a margin after being stopped."""
    while not drain.draining:
        sleep(poll)

    def idle() -> bool:
        return drain.claims_stopped and in_flight() == 0

    started = clock()
    deadline = started + drain.timeout
    log.info(
        "drain started: no new job, waiting for the running ones",
        extra={
            "event": "agent.drain_started",
            "signal": drain.signal,
            "in_flight": in_flight(),
            "timeout_seconds": drain.timeout,
        },
    )
    while True:
        if idle():
            log.info(
                "drain finished",
                extra={"event": "agent.drain_finished", "seconds": round(clock() - started, 1)},
            )
            return "drained"
        if drain.forced:
            log.warning(
                "second stop signal: stopping the running jobs now",
                extra={"event": "agent.drain_forced", "in_flight": in_flight()},
            )
            break
        if clock() >= deadline:
            log.warning(
                "drain timed out: stopping the running jobs",
                extra={
                    "event": "agent.drain_timeout",
                    "in_flight": in_flight(),
                    "timeout_seconds": drain.timeout,
                },
            )
            break
        sleep(poll)

    stopped = stop_all()
    end = clock() + cancel_grace + REPORT_MARGIN_SECONDS
    while clock() < end:
        if idle():
            log.info(
                "running jobs stopped",
                extra={"event": "agent.drain_stopped", "stopped": stopped},
            )
            return "stopped"
        sleep(poll)
    log.error(
        "jobs still running at exit",
        extra={"event": "agent.drain_abandoned", "in_flight": in_flight()},
    )
    return "abandoned"


EXIT_CODES: dict[Outcome, int] = {"drained": 0, "stopped": 1, "abandoned": 1}
