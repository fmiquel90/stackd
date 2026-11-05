from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.config import get_settings
from app.db import get_session
from app.enums import ACTIVE_STATES, AuditActorKind, Role, RunState, WorkerStatus
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.worker import Worker, WorkerPool
from app.models.worker_command import WorkerCommand
from app.security import hash_token
from app.spaces import accessible_space_ids, resolve_target_space
from app.workers.schemas import (
    PoolCreate,
    PoolCreated,
    PoolOut,
    QueueEntry,
    WorkerOut,
)
from app.workers.tools import can_run

router = APIRouter(prefix="/api/v1", tags=["worker-admin"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Depends(require_role(Role.admin))


@router.post("/worker-pools", response_model=PoolCreated, status_code=201, dependencies=[Admin])
async def create_pool(body: PoolCreate, user: CurrentUser, session: DbSession) -> PoolCreated:
    space_id = await resolve_target_space(session, user, body.space_id, min_role=Role.admin)
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
async def list_workers(user: CurrentUser, session: DbSession) -> list[Worker]:
    q = select(Worker).order_by(Worker.registered_at)
    ids = await accessible_space_ids(session, user)  # None = instance admin → all pools
    if ids is not None:
        q = q.join(WorkerPool, WorkerPool.id == Worker.pool_id).where(WorkerPool.space_id.in_(ids))
    return list((await session.execute(q)).scalars().all())


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


def _blocking_reason(
    run: Run,
    active_by_env: dict[uuid.UUID, set[uuid.UUID]],
    env_locked: bool,
    compatible: bool = True,
    affinity_seconds: int = 0,
    now: datetime | None = None,
) -> str | None:
    """Why a waiting run is not claimed yet (§12) — the claim's own filters (§7.2), in order."""
    if run.state not in (RunState.queued, RunState.confirmed):
        return None
    if env_locked:
        return "env_locked"
    if active_by_env.get(run.environment_id, set()) - {run.id}:
        return "active_run"
    if run.state == RunState.confirmed and run.confirmed_at is not None:
        # Reserved for the plan's worker until the affinity window ends — whether that worker is
        # still online or not, as the claim does (§7.2): past it, any compatible worker takes it.
        held_until = run.confirmed_at + timedelta(seconds=affinity_seconds)
        if (now or datetime.now(UTC)) < held_until:
            return "apply_affinity_hold"
    if not compatible:
        return "no_compatible_worker"
    return None


async def online_workers(session: AsyncSession) -> list[Worker]:
    since = datetime.now(UTC) - timedelta(seconds=get_settings().stackd_worker_offline_seconds)
    return list(
        (
            await session.execute(
                select(Worker).where(
                    Worker.status != WorkerStatus.offline, Worker.last_heartbeat_at >= since
                )
            )
        )
        .scalars()
        .all()
    )


def compatible(env: Environment, stack: Stack, workers: list[Worker]) -> bool:
    """True when an online worker could claim a run of `env` (§7.2/§7.4): its labels cover the env's
    and it can run the stack's tool version — the claim's own filters, evaluated in Python."""
    wanted = env.labels or {}
    return any(
        all((w.labels or {}).get(k) == v for k, v in wanted.items())
        and can_run(w.tools, stack.tool.value, stack.tool_version)
        for w in workers
    )


@router.get("/queue", response_model=list[QueueEntry])
async def queue(user: CurrentUser, session: DbSession) -> list[QueueEntry]:
    # Runs in progress (claimed/active) + waiting (queued/confirmed, unclaimed) with a reason.
    q = select(Run).where(Run.state.in_([*ACTIVE_STATES, RunState.queued])).order_by(Run.created_at)
    ids = await accessible_space_ids(session, user)  # None = instance admin → all spaces
    if ids is not None:
        q = (
            q.join(Environment, Environment.id == Run.environment_id)
            .join(Stack, Stack.id == Environment.stack_id)
            .where(Stack.space_id.in_(ids))
        )
    runs = (await session.execute(q)).scalars().all()

    # Active runs per env (to flag a waiting run blocked by ANOTHER active one; a `confirmed` run
    # is itself in ACTIVE_STATES and keeps its plan worker's id, so it must not block itself).
    active_by_env: dict[uuid.UUID, set[uuid.UUID]] = {}
    for r in runs:
        if r.state in ACTIVE_STATES and r.worker_id is not None:
            active_by_env.setdefault(r.environment_id, set()).add(r.id)

    # Online workers, and each waiting run's env + stack, for `env_locked`/`no_compatible_worker`.
    online = await online_workers(session)
    waiting_envs = {
        r.environment_id for r in runs if r.state in (RunState.queued, RunState.confirmed)
    }
    env_stack = {
        env.id: (env, stack)
        for env, stack in (
            await session.execute(
                select(Environment, Stack)
                .join(Stack, Stack.id == Environment.stack_id)
                .where(Environment.id.in_(waiting_envs))
            )
        ).all()
    }

    affinity = get_settings().stackd_apply_affinity_seconds
    now = datetime.now(UTC)
    out: list[QueueEntry] = []
    for r in runs:
        pair = env_stack.get(r.environment_id)
        can_take = pair is None or compatible(pair[0], pair[1], online)
        locked = pair is not None and pair[0].locked
        out.append(
            QueueEntry(
                run_id=r.id,
                environment_id=r.environment_id,
                state=r.state.value,
                worker_id=r.worker_id,
                blocking_reason=_blocking_reason(
                    r,
                    active_by_env,
                    env_locked=locked,
                    compatible=can_take,
                    affinity_seconds=affinity,
                    now=now,
                ),
            )
        )
    return out
