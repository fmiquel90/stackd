from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.drift.service import detect_drift
from app.enums import ACTIVE_STATES, RunEventActor, RunState, WorkerStatus
from app.logging import get_logger
from app.models.run import Run
from app.models.worker import Worker
from app.notifications.dispatcher import dispatch_pending
from app.runs.transition import transition
from app.vcs.dispatcher import dispatch_vcs

# Distinct advisory-lock keys so each periodic task runs once across replicas (§7.5).
_LOCK_WORKER_LOST = 74001
_LOCK_NOTIFY = 74002
_LOCK_VCS = 74003
_LOCK_DRIFT = 74004
_log = get_logger("stackd.scheduler")


async def detect_worker_lost(session: AsyncSession, now: datetime) -> int:
    """Mark stale workers offline; fail active runs stuck on a dead worker (§3.7, §7.5)."""
    settings = get_settings()
    offline_cutoff = now - timedelta(seconds=settings.stackd_worker_offline_seconds)
    await session.execute(
        update(Worker)
        .where(Worker.last_heartbeat_at < offline_cutoff, Worker.status != WorkerStatus.offline)
        .values(status=WorkerStatus.offline)
    )
    await session.flush()

    offline_ids = (
        (await session.execute(select(Worker.id).where(Worker.status == WorkerStatus.offline)))
        .scalars()
        .all()
    )
    if not offline_ids:
        await session.commit()
        return 0

    lost_cutoff = now - timedelta(seconds=settings.stackd_worker_lost_seconds)
    # An applying run carries a hard budget (§4.2) the worker enforces by killing tofu. Don't
    # reclaim a still-applying run before that budget + grace — failing a healthy long apply
    # mid-flight would let a second worker claim the env and apply concurrently (split-brain). By
    # the time this cutoff passes, the worker has self-terminated and its state/OIDC tokens have
    # expired, so a reclaimed apply can no longer write state.
    apply_cutoff = now - timedelta(
        seconds=settings.stackd_apply_timeout_seconds + settings.stackd_apply_lost_grace_seconds
    )
    non_applying = [s for s in ACTIVE_STATES if s != RunState.applying]
    runs = (
        (
            await session.execute(
                select(Run).where(
                    Run.worker_id.in_(offline_ids),
                    or_(
                        and_(Run.state == RunState.applying, Run.claimed_at < apply_cutoff),
                        and_(Run.state.in_(non_applying), Run.claimed_at < lost_cutoff),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.system,
            fields={"error": "worker_lost"},
            audit_action="run.apply_failed",
            audit_context={"reason": "worker_lost"},
        )
    await session.commit()
    if runs:
        _log.warning(
            "worker_lost runs failed",
            extra={"event": "scheduler.worker_lost", "count": len(runs)},
        )
    return len(runs)


async def _with_lock(session: AsyncSession, key: int, coro) -> None:  # type: ignore[no-untyped-def]
    got = (
        await session.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=key))
    ).scalar()
    if not got:
        return
    try:
        await coro(session, datetime.now(UTC))
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))


async def scheduler_loop() -> None:
    """Single background loop; each task is advisory-locked + idempotent (§7.5). A 10s tick keeps
    outbound notifications responsive; worker_lost detection is cheap/idempotent at that rate."""
    while True:
        try:
            async with SessionLocal() as session:
                await _with_lock(session, _LOCK_WORKER_LOST, detect_worker_lost)
            async with SessionLocal() as session:
                await _with_lock(session, _LOCK_NOTIFY, dispatch_pending)
            async with SessionLocal() as session:
                await _with_lock(session, _LOCK_VCS, dispatch_vcs)
            async with SessionLocal() as session:
                await _with_lock(session, _LOCK_DRIFT, detect_drift)
        except Exception as exc:
            print(f"[scheduler] tick error: {exc}", flush=True)
        await asyncio.sleep(10)
