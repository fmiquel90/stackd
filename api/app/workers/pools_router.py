from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import ACTIVE_STATES, AuditActorKind, Role, RunState
from app.errors import ProblemException
from app.models.run import Run
from app.models.worker import Worker, WorkerPool
from app.models.worker_command import WorkerCommand
from app.security import hash_token
from app.spaces import get_default_space, require_space_access
from app.workers.schemas import (
    PoolCreate,
    PoolCreated,
    PoolOut,
    QueueEntry,
    WorkerOut,
)

router = APIRouter(prefix="/api/v1", tags=["worker-admin"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Depends(require_role(Role.admin))


@router.post("/worker-pools", response_model=PoolCreated, status_code=201, dependencies=[Admin])
async def create_pool(body: PoolCreate, user: CurrentUser, session: DbSession) -> PoolCreated:
    space_id = body.space_id or (await get_default_space(session)).id
    await require_space_access(session, user, space_id, min_role=Role.admin)
    token = secrets.token_urlsafe(32)
    pool = WorkerPool(
        space_id=space_id, name=body.name, labels=body.labels, token_hash=hash_token(token)
    )
    session.add(pool)
    await session.flush()
    await record_audit(
        session,
        action="worker_pool.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="worker_pool",
        target_id=pool.id,
        context={"name": pool.name},
    )
    await session.commit()
    await session.refresh(pool)
    # Token returned once — only its hash is stored.
    return PoolCreated(
        id=pool.id, name=pool.name, labels=pool.labels, created_at=pool.created_at, token=token
    )


@router.get("/worker-pools", response_model=list[PoolOut], dependencies=[Admin])
async def list_pools(session: DbSession) -> list[WorkerPool]:
    return list(
        (await session.execute(select(WorkerPool).order_by(WorkerPool.name))).scalars().all()
    )


@router.delete("/worker-pools/{pool_id}", status_code=204, dependencies=[Admin])
async def delete_pool(pool_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    pool = await session.get(WorkerPool, pool_id)
    if pool is None:
        raise ProblemException(404, "Pool not found", None)
    await session.delete(pool)
    await record_audit(
        session,
        action="worker_pool.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="worker_pool",
        target_id=pool_id,
    )
    await session.commit()


@router.get("/workers", response_model=list[WorkerOut])
async def list_workers(_: CurrentUser, session: DbSession) -> list[Worker]:
    return list(
        (await session.execute(select(Worker).order_by(Worker.registered_at))).scalars().all()
    )


@router.post("/workers/{worker_id}/diagnostics", status_code=202, dependencies=[Admin])
async def request_diagnostics(worker_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Queue a read-only diagnostics bundle; the worker runs it on its next heartbeat (§7.1)."""
    if await session.get(Worker, worker_id) is None:
        raise ProblemException(404, "Worker not found", None)
    cmd = WorkerCommand(worker_id=worker_id, type="diagnostics")
    session.add(cmd)
    await session.flush()
    await record_audit(
        session,
        action="worker.diagnostics_requested",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="worker",
        target_id=worker_id,
    )
    await session.commit()
    return {"command_id": str(cmd.id)}


@router.get("/workers/{worker_id}/diagnostics", dependencies=[Admin])
async def latest_diagnostics(worker_id: uuid.UUID, session: DbSession) -> dict:
    cmd = (
        await session.execute(
            select(WorkerCommand)
            .where(WorkerCommand.worker_id == worker_id, WorkerCommand.type == "diagnostics")
            .order_by(WorkerCommand.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if cmd is None:
        return {"status": "none", "result": None}
    return {
        "status": cmd.status,
        "result": cmd.result,
        "requested_at": cmd.created_at.isoformat(),
        "completed_at": cmd.completed_at.isoformat() if cmd.completed_at else None,
    }


def _blocking_reason(run: Run, active_by_env: dict[uuid.UUID, int], env_locked: bool) -> str | None:
    if run.state not in (RunState.queued, RunState.confirmed):
        return None
    if env_locked:
        return "env_locked"
    if active_by_env.get(run.environment_id, 0) > 0:
        return "active_run"
    if run.state == RunState.confirmed:
        return "apply_affinity_hold"
    return None  # claimable; no compatible worker check is best-effort and omitted at MVP


@router.get("/queue", response_model=list[QueueEntry])
async def queue(_: CurrentUser, session: DbSession) -> list[QueueEntry]:
    # Runs in progress (claimed/active) + waiting (queued/confirmed, unclaimed) with a reason.
    runs = (
        (
            await session.execute(
                select(Run)
                .where(Run.state.in_([*ACTIVE_STATES, RunState.queued]))
                .order_by(Run.created_at)
            )
        )
        .scalars()
        .all()
    )

    # Count OTHER active runs per env (to flag a waiting run blocked by an active one).
    active_by_env: dict[uuid.UUID, int] = {}
    for r in runs:
        if r.state in ACTIVE_STATES and r.worker_id is not None:
            active_by_env[r.environment_id] = active_by_env.get(r.environment_id, 0) + 1

    out: list[QueueEntry] = []
    for r in runs:
        out.append(
            QueueEntry(
                run_id=r.id,
                environment_id=r.environment_id,
                state=r.state.value,
                worker_id=r.worker_id,
                blocking_reason=_blocking_reason(r, active_by_env, env_locked=False),
            )
        )
    return out
