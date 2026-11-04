"""Environment rollouts API (SPECS §21.7)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.config import get_settings
from app.db import get_session
from app.enums import Role, RolloutMemberStatus, RolloutState, RunEventActor, RunState
from app.errors import ProblemException
from app.models.dependency import EnvDependency, EnvOutput, OutputReference
from app.models.environment import Environment
from app.models.rollout import EnvRollout, EnvRolloutMember
from app.models.run import Run, RunEvent
from app.models.stack import Stack
from app.models.user import User
from app.rollouts.service import (
    approval_blocker,
    approval_refusal,
    approve_rollout,
    cancel_rollout,
    create_rollout,
    lock_rollout,
    member_edges,
    member_refusals,
    members_of,
    reapproval_due,
    resolve_members,
    retry_rollout,
)
from app.spaces import accessible_space_ids, require_space_access, resolve_target_space
from app.workers.pools_router import compatible, online_workers

router = APIRouter(prefix="/api/v1/rollouts", tags=["rollouts"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


class RolloutCreate(BaseModel):
    space_id: uuid.UUID | None = None
    env_name: str | None = None
    environment_ids: list[uuid.UUID] | None = None


_GATE_CLOSED_TEXT = {
    "approval_expired": "the approval expired",
    "approver_lost_rights": "the approver may no longer apply here",
    "member_changed_since_approval": "its tier, protection or four-eyes changed since the approval",
}


async def _waiting_reason(
    session: AsyncSession,
    rollout: EnvRollout,
    m: EnvRolloutMember,
    run: Run | None,
    env: Environment,
) -> str | None:
    """Why a member's plan stopped for a human (DESIGN §5.11): what keeps the gate from taking
    it (§21.5), else why the rollout's approval does not cover it."""
    if run is None or run.state != RunState.unconfirmed:
        return None
    checks = (run.check_results or {}).get("checks") or []
    reasons = await member_refusals(session, rollout, run, env, run.plan_summary, checks)
    if not reasons:
        blocker = await approval_blocker(session, rollout, m, env)
        reasons.append(
            _GATE_CLOSED_TEXT.get(
                blocker or rollout.pause_reason or "",
                "it was planned while the rollout could not confirm",
            )
        )
    return "; ".join(reasons)


async def _expected_mocks(
    session: AsyncSession, env_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Members that will plan on a mock (§21.4 preview): an upstream outside the rollout that has
    not published an output a reference needs, the reference having a mock."""
    out: dict[uuid.UUID, list[str]] = {}
    rows = await session.execute(
        select(EnvDependency, OutputReference)
        .join(OutputReference, OutputReference.dependency_id == EnvDependency.id)
        .where(EnvDependency.downstream_env_id.in_(env_ids))
    )
    for dep, ref in rows:
        if dep.upstream_env_id in env_ids or ref.mock_value is None:
            continue  # a member upstream will have applied by then; no mock: the run would fail
        published = (
            await session.execute(
                select(EnvOutput.id).where(
                    EnvOutput.environment_id == dep.upstream_env_id,
                    EnvOutput.name == ref.output_name,
                )
            )
        ).scalar_one_or_none()
        if published is None:
            out.setdefault(dep.downstream_env_id, []).append(ref.output_name)
    return out


async def _email(session: AsyncSession, user_id: uuid.UUID | None) -> dict[str, str] | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return {"id": str(user_id), "email": user.email if user else "?"}


async def _detail(session: AsyncSession, rollout: EnvRollout, viewer: User) -> dict[str, Any]:
    members = await members_of(session, rollout.id)
    env_ids = {m.environment_id for m in members}
    rows = {
        env.id: (env, stack)
        for env, stack in (
            await session.execute(
                select(Environment, Stack)
                .join(Stack, Stack.id == Environment.stack_id)
                .where(Environment.id.in_(env_ids))
            )
        ).all()
    }
    run_ids = [m.run_id for m in members if m.run_id]
    runs = {
        r.id: r for r in (await session.execute(select(Run).where(Run.id.in_(run_ids)))).scalars()
    }
    auto = set(
        (
            await session.execute(
                select(RunEvent.run_id).where(
                    RunEvent.run_id.in_(run_ids),
                    RunEvent.to_state == RunState.confirmed,
                    RunEvent.actor == RunEventActor.system,
                )
            )
        ).scalars()
    )
    mocks = await _expected_mocks(session, env_ids)
    # A queued member run no online worker can take (labels, tool version: §7.2/§7.4) would wait
    # forever while the rollout reads `running` — say so on the member.
    online = (
        await online_workers(session)
        if any(r.state == RunState.queued for r in runs.values())
        else []
    )
    refusal = await approval_refusal(session, viewer, rollout)
    out_members = []
    for m in members:
        env, stack = rows[m.environment_id]
        run = runs.get(m.run_id) if m.run_id else None
        reason = (
            await _waiting_reason(session, rollout, m, run, env)
            if m.status == RolloutMemberStatus.needs_confirmation
            else None
        )
        if run is not None and run.state == RunState.queued and not compatible(env, stack, online):
            reason = (
                "no online worker can take it (check the worker labels — the environment's "
                "labels route its runs — and the tool version)"
            )
        out_members.append(
            {
                "environment_id": str(env.id),
                "stack_name": stack.name,
                "env_name": env.name,
                "tier": env.tier,
                "protected": env.protected,
                "level": m.level,
                "status": m.status.value,
                "run_id": str(run.id) if run else None,
                "run_state": run.state.value if run else None,
                "plan_summary": run.plan_summary if run else None,
                "auto_confirmed": run is not None and run.id in auto,
                "waiting_reason": reason,
                "expected_mocks": mocks.get(env.id, []),
            }
        )
    return {
        "id": str(rollout.id),
        "space_id": str(rollout.space_id),
        "name": rollout.name,
        "state": rollout.state.value,
        "pause_reason": rollout.pause_reason,
        "created_by": await _email(session, rollout.created_by_user_id),
        "approved_by": await _email(session, rollout.approved_by_user_id),
        "approved_at": rollout.approved_at.isoformat() if rollout.approved_at else None,
        "approval_expires_at": rollout.approval_expires_at.isoformat()
        if rollout.approval_expires_at
        else None,
        "created_at": rollout.created_at.isoformat() if rollout.created_at else None,
        "finished_at": rollout.finished_at.isoformat() if rollout.finished_at else None,
        "members": out_members,
        "edges": [
            {"upstream_env_id": str(u), "downstream_env_id": str(d)}
            for u, d in await member_edges(session, env_ids)
        ],
        # For the viewer: may they approve (the UI hides or explains the action, DESIGN §5.11)?
        "can_approve": {"allowed": refusal is None, "reason": refusal},
        # Whether an approval is what the rollout waits for: its first, or a re-approval that
        # reopens a closed gate (§21.6).
        "approvable": rollout.state == RolloutState.awaiting_approval or reapproval_due(rollout),
        # How long an approval lasts (§21.6): the approval dialog states it.
        "approval_ttl_seconds": get_settings().stackd_rollout_approval_ttl_seconds,
    }


async def _load(
    session: AsyncSession,
    user: User,
    rollout_id: uuid.UUID,
    min_role: Role = Role.reader,
    *,
    lock: bool = False,
) -> EnvRollout:
    rollout = (
        await lock_rollout(session, rollout_id)
        if lock
        else await session.get(EnvRollout, rollout_id)
    )
    if rollout is None:
        raise ProblemException(404, "Rollout not found", None)
    await require_space_access(session, user, rollout.space_id, min_role=min_role)
    return rollout


@router.post("", status_code=201)
async def create(body: RolloutCreate, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Create a rollout (§21.4 step 1); the response is its preview, awaiting approval."""
    space_id = await resolve_target_space(session, user, body.space_id, min_role=Role.writer)
    envs = await resolve_members(session, space_id, body.env_name, body.environment_ids)
    names = {e.name for e in envs}
    name = body.env_name or (names.pop() if len(names) == 1 else f"{len(envs)} environments")
    rollout = await create_rollout(session, user, space_id, envs, name)
    await session.commit()
    return await _detail(session, rollout, user)


@router.get("")
async def list_rollouts(
    user: CurrentUser,
    session: DbSession,
    space_id: uuid.UUID | None = None,
    state: RolloutState | None = None,
) -> list[dict[str, Any]]:
    q = select(EnvRollout).order_by(EnvRollout.created_at.desc()).limit(100)
    ids = await accessible_space_ids(session, user)
    if ids is not None:
        q = q.where(EnvRollout.space_id.in_(ids))
    if space_id is not None:
        q = q.where(EnvRollout.space_id == space_id)
    if state is not None:
        q = q.where(EnvRollout.state == state)
    rollouts = list((await session.execute(q)).scalars().all())
    counts: dict[uuid.UUID, dict[str, int]] = {}
    if rollouts:
        rows = await session.execute(
            select(EnvRolloutMember.rollout_id, EnvRolloutMember.status, func.count())
            .where(EnvRolloutMember.rollout_id.in_([r.id for r in rollouts]))
            .group_by(EnvRolloutMember.rollout_id, EnvRolloutMember.status)
        )
        for rid, status, n in rows:
            counts.setdefault(rid, {})[status.value] = n
    return [
        {
            "id": str(r.id),
            "space_id": str(r.space_id),
            "name": r.name,
            "state": r.state.value,
            "pause_reason": r.pause_reason,
            "members": sum(counts.get(r.id, {}).values()),
            "done": counts.get(r.id, {}).get("done", 0),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rollouts
    ]


@router.get("/{rollout_id}")
async def get_rollout(rollout_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    return await _detail(session, await _load(session, user, rollout_id), user)


@router.post("/{rollout_id}/approve")
async def approve(rollout_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    rollout = await _load(session, user, rollout_id, lock=True)
    await approve_rollout(session, user, rollout)
    await session.commit()
    return await _detail(session, rollout, user)


@router.post("/{rollout_id}/retry")
async def retry(rollout_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    rollout = await _load(session, user, rollout_id, Role.writer, lock=True)
    await retry_rollout(session, user, rollout)
    await session.commit()
    return await _detail(session, rollout, user)


@router.post("/{rollout_id}/cancel")
async def cancel(rollout_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    rollout = await _load(session, user, rollout_id, Role.writer, lock=True)
    await cancel_rollout(session, user, rollout)
    await session.commit()
    return await _detail(session, rollout, user)
