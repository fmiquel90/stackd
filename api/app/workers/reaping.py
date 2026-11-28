"""Ending a run whose job no worker is executing any more (SPECS §3.7, §7.1): its worker went
offline (`worker_lost`), or a live worker says it does not run it (`job_not_running` — a lost
claim response, a terminal report that never arrived)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import RunEventActor, RunState
from app.models.run import Run
from app.runs.transition import transition
from app.workers.claim import holds_apply_credentials

# Consecutive heartbeats of a live worker not listing a job before its run is failed: one could be
# a beat whose snapshot raced the job's start.
UNLISTED_HEARTBEATS_TO_REAP = 2


def reclaimable(run: Run, now: datetime) -> bool:
    """The claim is old enough for the run to be failed once its job is known not to run: past
    `stackd_worker_lost_seconds` (a claim response still in flight, a heartbeat sent before the
    claim) — and for a job holding apply credentials past the apply window (§4.2): its state and
    OIDC tokens have expired by then, so a job still running somewhere can no longer write state
    while the env goes to the next run (split-brain)."""
    if run.claimed_at is None:
        return False
    settings = get_settings()
    window = (
        settings.apply_reclaim_window_seconds
        if holds_apply_credentials(run)
        else settings.stackd_worker_lost_seconds
    )
    return run.claimed_at < now - timedelta(seconds=window)


async def fail_lost_job(session: AsyncSession, run: Run, reason: str) -> None:
    await transition(
        session,
        run,
        RunState.failed,
        actor=RunEventActor.system,
        fields={"error": reason},
        audit_action="run.apply_failed",
        audit_context={"reason": reason},
    )
