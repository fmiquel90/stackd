from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, get_sessionmaker
from app.drift.service import detect_drift
from app.enums import ACTIVE_STATES, RunEventActor, RunState, WorkerStatus
from app.logging import get_logger
from app.models.run import Run
from app.models.worker import Worker
from app.notifications.dispatcher import dispatch_pending
from app.runs.transition import transition
from app.vcs.dispatcher import dispatch_vcs
from app.workers.claim import holds_apply_credentials

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
    # A job holding apply credentials — an applying run, or a mutating command run (§4.3) — is
    # not reclaimed before the apply budget + grace (§4.2): failing a healthy long apply mid-flight
    # would let a second run claim the env and write state concurrently (split-brain). Its state and
    # OIDC tokens are minted for exactly this window (same `holds_apply_credentials` definition as
    # the claim), so by the time this cutoff passes they have expired and a reclaimed job can no
    # longer write state.
    apply_cutoff = now - timedelta(seconds=settings.apply_reclaim_window_seconds)
    candidates = (
        (
            await session.execute(
                select(Run).where(
                    Run.worker_id.in_(offline_ids),
                    Run.state.in_(ACTIVE_STATES),
                    Run.claimed_at < max(lost_cutoff, apply_cutoff),
                )
            )
        )
        .scalars()
        .all()
    )
    runs = [
        run
        for run in candidates
        if run.claimed_at is not None
        and run.claimed_at < (apply_cutoff if holds_apply_credentials(run) else lost_cutoff)
    ]
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
    """Run `coro(session, now)` only if no other replica holds `key` (§7.5). The session-level
    advisory lock lives on a dedicated connection held for the task's whole run, never on the
    task's session: the tasks commit mid-way, which hands the session's connection back to the
    pool, so an unlock through the session could land on another pooled connection, miss, and leak
    the lock (later ticks would then silently skip the task). AUTOCOMMIT keeps the lock connection
    idle rather than idle-in-transaction, and the unlock runs even if the task's session failed."""
    engine = get_sessionmaker().kw["bind"]  # the shared AsyncEngine (same pool as the session)
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as lock_conn:
        got = (
            await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=key))
        ).scalar()
        if not got:
            return
        try:
            await coro(session, datetime.now(UTC))
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))


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
