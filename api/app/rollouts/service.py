"""Environment rollouts (SPECS §21): an approved, ordered plan → apply of a set of environments
along their dependency edges.

A rollout's state changes only through `advance_rollout()`, always in the transaction of the event
that moves it: its creation, its approval, a member run's `transition()` (`on_run_transition`), a
retry or a cancel. A member run is an ordinary tracked run: it goes through `transition()`, and the
only thing a rollout adds is the gate (§21.5) under which its plan may be confirmed by the system.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.enums import (
    AuditActorKind,
    RolloutMemberStatus,
    RolloutState,
    RunEventActor,
    RunState,
    RunType,
    TriggeredBy,
)
from app.errors import ProblemException
from app.inbox.service import notify
from app.models.dependency import EnvDependency
from app.models.environment import Environment
from app.models.rollout import EnvRollout, EnvRolloutMember
from app.models.run import Run
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.permissions import can_apply
from app.spaces.service import get_membership

M = RolloutMemberStatus
TERMINAL = frozenset({RolloutState.succeeded, RolloutState.failed, RolloutState.canceled})
# Members still to do something: the rollout is not over while one of them is in these.
_OPEN = frozenset({M.pending, M.planning, M.needs_confirmation, M.applying})
# A paused rollout in which members may still be auto-confirmed (§21.5): it waits on one member's
# human confirmation, the other branches go on. An expired approval or a lost right stops them all.
_GATE_OPEN_PAUSES = frozenset({None, "needs_confirmation"})


def _now() -> datetime:
    return datetime.now(UTC)


async def members_of(session: AsyncSession, rollout_id: uuid.UUID) -> list[EnvRolloutMember]:
    return list(
        (
            await session.execute(
                select(EnvRolloutMember)
                .where(EnvRolloutMember.rollout_id == rollout_id)
                .order_by(EnvRolloutMember.level)
            )
        )
        .scalars()
        .all()
    )


async def member_edges(
    session: AsyncSession, env_ids: set[uuid.UUID]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """The dependency edges (upstream, downstream) between the members only."""
    rows = await session.execute(
        select(EnvDependency.upstream_env_id, EnvDependency.downstream_env_id).where(
            EnvDependency.upstream_env_id.in_(env_ids),
            EnvDependency.downstream_env_id.in_(env_ids),
        )
    )
    return [(up, down) for up, down in rows]


def levels(env_ids: set[uuid.UUID], edges: list[tuple[uuid.UUID, uuid.UUID]]) -> dict:
    """Topological level of each member (Kahn): 0 for the roots, else 1 + its deepest upstream."""
    parents: dict[uuid.UUID, set[uuid.UUID]] = {e: set() for e in env_ids}
    children: dict[uuid.UUID, set[uuid.UUID]] = {e: set() for e in env_ids}
    for up, down in edges:
        parents[down].add(up)
        children[up].add(down)
    level = {e: 0 for e in env_ids}
    indegree = {e: len(parents[e]) for e in env_ids}
    queue = deque(e for e in env_ids if indegree[e] == 0)
    while queue:
        node = queue.popleft()
        for child in children[node]:
            level[child] = max(level[child], level[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return level


async def _signal(session: AsyncSession, rollout: EnvRollout) -> None:
    """WS fan-out (§21.7), same transaction: subscribers re-read the rollout."""
    payload = json.dumps({"kind": "rollout_event", "rollout_id": str(rollout.id)})
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(
            chan=f"rollout_{rollout.id}", payload=payload
        )
    )


async def _audit(
    session: AsyncSession,
    rollout: EnvRollout,
    action: str,
    *,
    user: User | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    await record_audit(
        session,
        action=action,
        actor_kind=AuditActorKind.user if user else AuditActorKind.system,
        actor_id=user.id if user else None,
        actor_email=user.email if user else None,
        target_kind="rollout",
        target_id=rollout.id,
        context={"name": rollout.name, **(context or {})},
    )


async def _notify_people(session: AsyncSession, rollout: EnvRollout, kind: str) -> None:
    """In-app notifications (§17) to the rollout's creator and approver."""
    for user_id in {rollout.created_by_user_id, rollout.approved_by_user_id} - {None}:
        assert user_id is not None
        await notify(
            session,
            user_id,
            kind,
            context={"rollout_id": str(rollout.id), "name": rollout.name},
        )


# --- the gate (§21.5) ---


def plan_passes_gate(
    run_type: RunType,
    summary: dict[str, Any] | None,
    has_warnings: bool,
    used_mocks: bool,
    used_secret_fallback: bool,
) -> bool:
    """A plan the system may confirm under a rollout's approval: it only creates (no change,
    destroy — a replacement included — nor import), on a tracked run, with no mock, no secret
    fallback and no warn or fail check."""
    s = summary or {}
    return (
        run_type == RunType.tracked
        and int(s.get("add") or 0) > 0
        and int(s.get("change") or 0) == 0
        and int(s.get("destroy") or 0) == 0
        and int(s.get("import") or 0) == 0
        and not used_mocks
        and not used_secret_fallback
        and not has_warnings
    )


def approval_valid(rollout: EnvRollout, now: datetime | None = None) -> bool:
    return rollout.approval_expires_at is not None and (now or _now()) < rollout.approval_expires_at


async def approver_may_apply(session: AsyncSession, rollout: EnvRollout, env: Environment) -> bool:
    """The approver still passes `can_apply` on `env` (§2.4: role and tier, effective in the
    space): a revoked right is not inherited by the rollout."""
    if rollout.approved_by_user_id is None:
        return False
    approver = await session.get(User, rollout.approved_by_user_id)
    if approver is None or approver.disabled:
        return False
    membership = await get_membership(session, approver.id, rollout.space_id)
    return can_apply(approver, env, membership).allowed


async def confirm_under_approval(
    session: AsyncSession, run: Run, *, fields: dict[str, Any] | None = None
) -> bool:
    """Confirm `run` (from planning, checking or unconfirmed) by the system under its rollout's
    approval if the gate passes (§21.5, §21.6); the confirmer recorded is the approver. Returns
    whether it did. The plan's own outcome (`fields`) is applied in the same transition."""
    from app.runs.transition import transition

    if run.rollout_id is None:
        return False
    rollout = await session.get(EnvRollout, run.rollout_id)
    env = await session.get(Environment, run.environment_id)
    if rollout is None or env is None:
        return False
    if rollout.state not in (RolloutState.running, RolloutState.paused):
        return False
    if rollout.pause_reason not in _GATE_OPEN_PAUSES or not approval_valid(rollout):
        return False
    merged = {**(fields or {})}
    summary = merged.get("plan_summary", run.plan_summary)
    checks = (merged.get("check_results") or run.check_results or {}).get("checks") or []
    if not plan_passes_gate(
        run.type, summary, bool(checks), run.used_mocks, run.used_secret_fallback
    ):
        return False
    if not await approver_may_apply(session, rollout, env):
        return False
    await transition(
        session,
        run,
        RunState.confirmed,
        actor=RunEventActor.system,
        fields={
            **merged,
            "confirmed_by_user_id": rollout.approved_by_user_id,
            "confirmed_at": _now(),
        },
        audit_action="run.confirmed",
        audit_context={
            "via": "rollout",
            "rollout_id": str(rollout.id),
            "approved_by": str(rollout.approved_by_user_id),
        },
    )
    return True


# --- lifecycle ---


async def _start_member(session: AsyncSession, rollout: EnvRollout, m: EnvRolloutMember) -> None:
    """The member's plan: an ordinary tracked run, queued like any other (§21.4 step 3)."""
    run = Run(
        environment_id=m.environment_id,
        type=RunType.tracked,
        state=RunState.queued,
        triggered_by=TriggeredBy.rollout,
        # Four-eyes keeps its meaning on every member run (§21.3).
        trigger_user_id=rollout.created_by_user_id,
        rollout_id=rollout.id,
    )
    session.add(run)
    await session.flush()
    m.run_id = run.id
    m.status = M.planning
    await record_audit(
        session,
        action="run.triggered",
        actor_kind=AuditActorKind.system,
        target_kind="run",
        target_id=run.id,
        context={
            "via": "rollout",
            "rollout_id": str(rollout.id),
            "environment_id": str(run.environment_id),
        },
    )


async def _set_state(
    session: AsyncSession,
    rollout: EnvRollout,
    state: RolloutState,
    reason: str | None = None,
) -> None:
    if rollout.state == state and rollout.pause_reason == reason:
        return
    before = rollout.state
    rollout.state = state
    rollout.pause_reason = reason
    rollout.updated_at = _now()
    if state in TERMINAL:
        rollout.finished_at = _now()
        for m in await members_of(session, rollout.id):
            m.active = False
    action = {
        RolloutState.paused: "rollout.paused",
        RolloutState.succeeded: "rollout.succeeded",
        RolloutState.failed: "rollout.failed",
        RolloutState.canceled: "rollout.canceled",
    }.get(state)
    if state == RolloutState.running and before == RolloutState.paused:
        action = "rollout.resumed"
    if (
        action and action != "rollout.canceled"
    ):  # a cancel is audited by its endpoint, with its user
        await _audit(session, rollout, action, context={"reason": reason, "from": before.value})
    if state == RolloutState.paused:
        await _notify_people(session, rollout, "rollout_paused")
    elif state in (RolloutState.succeeded, RolloutState.failed):
        await _notify_people(session, rollout, f"rollout_{state.value}")


async def advance_rollout(session: AsyncSession, rollout: EnvRollout) -> None:
    """Start every member whose upstream members are all done, then derive the rollout's state
    (§21.4). The ONE place a rollout's state changes after its creation."""
    if rollout.state in TERMINAL or rollout.state == RolloutState.awaiting_approval:
        await _signal(session, rollout)
        return
    members = await members_of(session, rollout.id)
    by_env = {m.environment_id: m for m in members}
    edges = await member_edges(session, set(by_env))
    parents: dict[uuid.UUID, set[uuid.UUID]] = {e: set() for e in by_env}
    for up, down in edges:
        parents[down].add(up)

    now = _now()
    valid = approval_valid(rollout, now)
    if valid:
        for m in members:
            if m.status == M.pending and all(
                by_env[p].status == M.done for p in parents[m.environment_id]
            ):
                await _start_member(session, rollout, m)

    statuses = {m.status for m in members}
    if not statuses & _OPEN:
        final = RolloutState.succeeded if statuses == {M.done} else RolloutState.failed
        await _set_state(session, rollout, final)
    elif not valid and statuses & {M.pending, M.needs_confirmation}:
        await _set_state(session, rollout, RolloutState.paused, "approval_expired")
    elif M.needs_confirmation in statuses:
        reason = "needs_confirmation"
        for m in members:
            if m.status != M.needs_confirmation:
                continue
            env = await session.get(Environment, m.environment_id)
            if env is not None and not await approver_may_apply(session, rollout, env):
                reason = "approver_lost_rights"
                break
        await _set_state(session, rollout, RolloutState.paused, reason)
    else:
        await _set_state(session, rollout, RolloutState.running)
    await _signal(session, rollout)


def _reachable(start: uuid.UUID, edges: list[tuple[uuid.UUID, uuid.UUID]]) -> set[uuid.UUID]:
    children: dict[uuid.UUID, set[uuid.UUID]] = {}
    for up, down in edges:
        children.setdefault(up, set()).add(down)
    seen: set[uuid.UUID] = set()
    stack = list(children.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(children.get(node, ()))
    return seen


async def on_run_transition(session: AsyncSession, run: Run, to_state: RunState) -> None:
    """Called by `transition()` for a member run (same transaction): the member follows its run,
    and the rollout advances. A run of a canceled rollout only updates its member."""
    rollout = await session.get(EnvRollout, run.rollout_id)
    if rollout is None:
        return
    member = await session.get(EnvRolloutMember, (rollout.id, run.environment_id))
    if member is None or member.run_id != run.id:
        return
    if to_state in (RunState.preparing, RunState.planning, RunState.checking):
        member.status = M.planning
    elif to_state == RunState.unconfirmed:
        member.status = M.needs_confirmation
    elif to_state in (RunState.confirmed, RunState.applying):
        member.status = M.applying
    elif to_state == RunState.finished:
        member.status = M.done
    elif to_state in (RunState.failed, RunState.discarded, RunState.canceled):
        if rollout.state == RolloutState.canceled:
            member.status = M.canceled
        else:
            member.status = M.failed
            # §21.4 step 6: everything reachable from it inside the rollout is skipped.
            members = await members_of(session, rollout.id)
            by_env = {m.environment_id: m for m in members}
            for env_id in _reachable(run.environment_id, await member_edges(session, set(by_env))):
                if by_env[env_id].status == M.pending:
                    by_env[env_id].status = M.skipped
    if rollout.state != RolloutState.canceled:
        await advance_rollout(session, rollout)
    else:
        await _signal(session, rollout)


async def resolve_members(
    session: AsyncSession,
    space_id: uuid.UUID,
    env_name: str | None,
    environment_ids: list[uuid.UUID] | None,
) -> list[Environment]:
    if (env_name is None) == (environment_ids is None):
        raise ProblemException(422, "Members", "Give either env_name or environment_ids.")
    q = select(Environment).join(Stack, Stack.id == Environment.stack_id)
    q = q.where(Stack.space_id == space_id)
    if env_name is not None:
        envs = list((await session.execute(q.where(Environment.name == env_name))).scalars().all())
    else:
        assert environment_ids is not None
        wanted = set(environment_ids)
        q = q.where(Environment.id.in_(wanted))
        envs = list((await session.execute(q)).scalars().all())
        if {e.id for e in envs} != wanted:
            raise ProblemException(422, "Members", "Every environment must be in the space.")
    if not envs:
        raise ProblemException(422, "Members", f"No environment named '{env_name}' in this space.")
    return envs


async def create_rollout(
    session: AsyncSession, user: User, space_id: uuid.UUID, envs: list[Environment], name: str
) -> EnvRollout:
    rollout = EnvRollout(space_id=space_id, name=name, created_by_user_id=user.id)
    session.add(rollout)
    await session.flush()
    ids = {e.id for e in envs}
    level = levels(ids, await member_edges(session, ids))
    for env in envs:
        session.add(
            EnvRolloutMember(rollout_id=rollout.id, environment_id=env.id, level=level[env.id])
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Already rolling out", "An environment already belongs to an active rollout."
        ) from exc
    await _audit(
        session,
        rollout,
        "rollout.created",
        user=user,
        context={"environments": [str(e.id) for e in envs]},
    )
    await _signal(session, rollout)
    return rollout


async def approval_refusal(session: AsyncSession, user: User, rollout: EnvRollout) -> str | None:
    """Why `user` may not approve `rollout` (§21.6), or None: `can_apply` on every member (all or
    nothing), and four-eyes against the creator when any member's tier, `protected` or
    `require_second_pair_of_eyes` asks for it."""
    members = await members_of(session, rollout.id)
    envs = [e for m in members if (e := await session.get(Environment, m.environment_id))]
    membership = await get_membership(session, user.id, rollout.space_id)
    refused = sorted({e.name for e in envs if not can_apply(user, e, membership).allowed})
    if refused:
        return f"You may not apply on: {', '.join(refused)}"
    tiers = {
        t.name: t
        for t in (
            await session.execute(select(Tier).where(Tier.name.in_({e.tier for e in envs})))
        ).scalars()
    }
    four_eyes = any(
        # Fail closed: a missing tier row requires four-eyes (§2.4).
        (tiers[e.tier].requires_four_eyes if e.tier in tiers else True)
        or e.protected
        or e.require_second_pair_of_eyes
        for e in envs
    )
    if four_eyes and user.id == rollout.created_by_user_id:
        return "You created this rollout (four-eyes required)."
    return None


async def approve_rollout(session: AsyncSession, user: User, rollout: EnvRollout) -> None:
    """§21.6: all-or-nothing `can_apply` over the members, four-eyes against the creator, a
    time-boxed approval; a re-approval also confirms the members whose plan waited only on it."""
    re_approval = rollout.state == RolloutState.paused and rollout.pause_reason in (
        "approval_expired",
        "approver_lost_rights",
    )
    if rollout.state != RolloutState.awaiting_approval and not re_approval:
        raise ProblemException(409, "Not awaiting approval", f"State is {rollout.state.value}.")
    refusal = await approval_refusal(session, user, rollout)
    if refusal is not None:
        raise ProblemException(403, "Forbidden", refusal)
    members = await members_of(session, rollout.id)
    envs = [e for m in members if (e := await session.get(Environment, m.environment_id))]

    now = _now()
    rollout.approved_by_user_id = user.id
    rollout.approved_at = now
    rollout.approval_expires_at = now + timedelta(
        seconds=get_settings().stackd_rollout_approval_ttl_seconds
    )
    rollout.state = RolloutState.running
    rollout.pause_reason = None
    await _audit(
        session,
        rollout,
        "rollout.approved",
        user=user,
        context={
            "environments": {str(e.id): e.tier for e in envs if e},
            "expires_at": rollout.approval_expires_at.isoformat(),
            "re_approval": re_approval,
        },
    )
    for m in members:
        if m.status == M.needs_confirmation and m.run_id is not None:
            run = await session.get(Run, m.run_id)
            if run is not None and run.state == RunState.unconfirmed:
                await confirm_under_approval(session, run)
    await advance_rollout(session, rollout)


async def cancel_rollout(session: AsyncSession, user: User, rollout: EnvRollout) -> None:
    from app.runs.transition import transition

    if rollout.state in TERMINAL:
        raise ProblemException(409, "Rollout over", f"State is {rollout.state.value}.")
    await _set_state(session, rollout, RolloutState.canceled)
    await _audit(session, rollout, "rollout.canceled", user=user)
    for m in await members_of(session, rollout.id):
        if m.status == M.pending:
            m.status = M.canceled
            continue
        run = await session.get(Run, m.run_id) if m.run_id else None
        if run is not None and run.state in (RunState.queued, RunState.unconfirmed):
            await transition(
                session,
                run,
                RunState.canceled,
                actor=RunEventActor.user,
                actor_id=user.id,
                actor_email=user.email,
                audit_action="run.canceled",
                audit_context={"via": "rollout", "rollout_id": str(rollout.id)},
            )
    await _signal(session, rollout)


async def retry_rollout(session: AsyncSession, user: User, rollout: EnvRollout) -> None:
    if rollout.state != RolloutState.failed:
        raise ProblemException(409, "Not failed", f"State is {rollout.state.value}.")
    members = await members_of(session, rollout.id)
    for m in members:
        if m.status in (M.failed, M.skipped):
            m.status = M.pending
            m.run_id = None
        m.active = True
    rollout.finished_at = None
    rollout.state = RolloutState.running
    rollout.pause_reason = None
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Already rolling out", "An environment now belongs to another active rollout."
        ) from exc
    await _audit(session, rollout, "rollout.retried", user=user)
    await advance_rollout(session, rollout)
