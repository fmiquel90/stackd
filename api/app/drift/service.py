from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.enums import (
    ACTIVE_STATES,
    AuditActorKind,
    DriftStatus,
    Role,
    RunState,
    RunType,
    TriggeredBy,
)
from app.inbox.service import notify
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.user import User

_log = get_logger("stackd.drift")
_APPROVERS = (Role.approver, Role.admin)


async def _last_applied_sha(session: AsyncSession, env_id: uuid.UUID) -> str | None:
    """The commit currently deployed: the most recent run that finished after applying. Drift =
    state vs reality at that commit, so a refresh-only plan must run against it (not git HEAD)."""
    return (
        await session.execute(
            select(Run.commit_sha)
            .where(
                Run.environment_id == env_id,
                Run.state == RunState.finished,
                Run.type.in_((RunType.tracked, RunType.destroy)),
                Run.commit_sha.is_not(None),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def detect_drift(session: AsyncSession, now: datetime) -> int:
    """Scheduler task (§19, advisory-locked): enqueue a read-only `-refresh-only` proposed run for
    each due environment with an applied commit and no active run. Idempotent — gated on
    `last_drift_checked_at + interval`, so re-running before the interval elapses is a no-op."""
    from app.runs.service import trigger_run

    interval = get_settings().stackd_drift_interval_seconds
    cutoff = now - timedelta(seconds=interval)
    due = (
        (
            await session.execute(
                select(Environment).where(
                    Environment.drift_check_enabled.is_(True),
                    Environment.locked.is_(False),
                    (Environment.last_drift_checked_at.is_(None))
                    | (Environment.last_drift_checked_at < cutoff),
                )
            )
        )
        .scalars()
        .all()
    )
    triggered = 0
    for env in due:
        active = (
            await session.execute(
                select(Run.id)
                .where(Run.environment_id == env.id, Run.state.in_(list(ACTIVE_STATES)))
                .limit(1)
            )
        ).first()
        if active is not None:
            continue
        env.last_drift_checked_at = now  # bump regardless, so a no-op env isn't retried every tick
        sha = await _last_applied_sha(session, env.id)
        if sha is None:
            await session.commit()  # nothing applied yet — record the check, leave status unknown
            continue
        await trigger_run(
            session,
            env,
            run_type=RunType.proposed,
            triggered_by=TriggeredBy.schedule,
            commit_sha=sha,
            is_drift=True,
        )
        triggered += 1
    if triggered:
        _log.info("drift checks enqueued", extra={"event": "drift.enqueued", "count": triggered})
    return triggered


async def _notify_drift(session: AsyncSession, env: Environment, run: Run) -> None:
    """In-app notification to every eligible approver for the env's tier (§17), like an approval
    request. Fired once per transition into drift (the caller debounces)."""
    ctx = {"environment_id": str(env.id), "environment": env.name, "tier": env.tier}
    approvers = (
        (
            await session.execute(
                select(User.id).where(
                    User.role.in_(_APPROVERS),
                    User.disabled.is_(False),
                    text(":tier = ANY(allowed_tiers)").bindparams(tier=env.tier),
                )
            )
        )
        .scalars()
        .all()
    )
    for uid in approvers:
        await notify(session, uid, "drift_detected", run_id=run.id, context=ctx)


async def record_drift_result(
    session: AsyncSession, run: Run, env: Environment, status: DriftStatus
) -> None:
    """Apply a completed drift run's outcome to its environment (same txn as the run transition).
    Notifies + audits once per transition INTO drift (debounced while it stays drifted)."""
    previous = env.drift_status
    env.drift_status = status.value
    if status == DriftStatus.drifted:
        env.drift_run_id = run.id
        if previous != DriftStatus.drifted.value:  # debounce: only on the edge into drift
            await record_audit(
                session,
                action="environment.drift_detected",
                actor_kind=AuditActorKind.system,
                target_kind="environment",
                target_id=env.id,
                context={"run_id": str(run.id), "tier": env.tier},
            )
            await _notify_drift(session, env, run)
    else:
        env.drift_run_id = None


async def clear_drift(session: AsyncSession, env: Environment, now: datetime) -> None:
    """A successful apply reconciles state with reality (§19): back to in_sync, clear the marker."""
    was_drifted = env.drift_status == DriftStatus.drifted.value
    env.drift_status = DriftStatus.in_sync.value
    env.drift_run_id = None
    env.last_drift_checked_at = now
    if was_drifted:
        await record_audit(
            session,
            action="environment.drift_cleared",
            actor_kind=AuditActorKind.system,
            target_kind="environment",
            target_id=env.id,
            context={"reason": "applied"},
        )
