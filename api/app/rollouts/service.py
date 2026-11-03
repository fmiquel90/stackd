"""Environment rollouts (SPECS §21): an approved, ordered plan → apply of a set of environments
along their dependency edges.

A rollout's state changes only through `advance_rollout()`, always in the transaction of the event
that moves it: its creation, its approval, a member run's `transition()` (`on_run_transition`), a
retry or a cancel. A member run is an ordinary tracked run: it goes through `transition()`, and the
only thing a rollout adds is the gate (§21.5) under which its plan may be confirmed by the system.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.enums import (
    AuditActorKind,
    Role,
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
from app.models.run import Run, RunEvent
from app.models.stack import Stack
from app.models.state import StateVersion
from app.models.user import User
from app.permissions import can_apply
from app.spaces.service import get_membership

M = RolloutMemberStatus
TERMINAL = frozenset({RolloutState.succeeded, RolloutState.failed, RolloutState.canceled})
# Members still to do something: the rollout is not over while one of them is in these.
_OPEN = frozenset({M.pending, M.planning, M.needs_confirmation, M.applying})
# Pause reasons (§21.4), most severe first. `needs_confirmation`: a member waits on a human, the
# gate stays open and the other branches go on. The others CLOSE the gate: nothing is started nor
# confirmed under the approval until a re-approval (§21.6) clears them; runs already planning or
# applying finish.
GATE_CLOSED = ("approval_expired", "approver_lost_rights", "member_changed_since_approval")
# Kept until a re-approval even once its cause is gone (an expired approval is derived from time).
_STICKY = frozenset({"approver_lost_rights", "member_changed_since_approval"})
# The `plan_summary` counters (§3.5) an agent must report for the gate to judge a plan: one that
# can't prove a plan imports or forgets nothing fails closed.
REQUIRED_COUNTERS = ("add", "change", "destroy", "import", "forget")
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_COUNTER_TEXT = {
    "change": "changes",
    "destroy": "destroys",
    "import": "imports",
    "forget": "forgets",
}


def _now() -> datetime:
    return datetime.now(UTC)


async def lock_rollout(session: AsyncSession, rollout_id: uuid.UUID) -> EnvRollout | None:
    """The rollout, its row locked until the transaction ends (§21.3): every change to a rollout
    and its members runs under this lock, so two member events finishing at once (two parents of
    the same member, a cancel and an apply) are serialized and the second sees the first's work."""
    return (
        await session.execute(
            select(EnvRollout)
            .where(EnvRollout.id == rollout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def members_of(session: AsyncSession, rollout_id: uuid.UUID) -> list[EnvRolloutMember]:
    # Fresh rows (populate_existing): read after the rollout lock, they include what a concurrent
    # transaction committed while this one waited for it.
    return list(
        (
            await session.execute(
                select(EnvRolloutMember)
                .where(EnvRolloutMember.rollout_id == rollout_id)
                .order_by(EnvRolloutMember.level)
                .execution_options(populate_existing=True)
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


async def _notify_people(
    session: AsyncSession, rollout: EnvRollout, kind: str, extra: dict[str, Any] | None = None
) -> None:
    """In-app notifications (§17) to the rollout's creator and approver."""
    for user_id in {rollout.created_by_user_id, rollout.approved_by_user_id} - {None}:
        assert user_id is not None
        await notify(
            session,
            user_id,
            kind,
            context={"rollout_id": str(rollout.id), "name": rollout.name, **(extra or {})},
        )


# --- the gate (§21.5) ---


def _count(value: object) -> int | None:
    """A counter of `plan_summary`, or None when it is no integer (fail closed)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def plan_refusals(
    run_type: RunType,
    summary: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    *,
    used_mocks: bool,
    used_secret_fallback: bool,
) -> list[str]:
    """What, in the plan itself, keeps the system from confirming it under a rollout's approval
    (§21.5) — empty when it only creates (no change, destroy — a replacement included —, import,
    forget nor any other kind of action), on a tracked run, with no mock, no secret fallback and
    no warn or fail check."""
    s = summary or {}
    out: list[str] = []
    if run_type != RunType.tracked:
        out.append(f"a {run_type.value} run")
    if used_mocks:
        out.append("consumed a mock")
    if used_secret_fallback:
        out.append("used a secret fallback")
    if checks:
        out.append("check " + ", ".join(f"{c.get('status')}: {c.get('name')}" for c in checks))
    missing = [k for k in REQUIRED_COUNTERS if k not in s]
    if missing:
        out.append(f"its worker does not report {', '.join(missing)} (update the agent)")
    # Every counter but `add` — the known ones and any a newer tool reports — must be zero.
    for key, value in s.items():
        if key == "add":
            continue
        n = _count(value)
        if n is None:
            out.append(f"unreadable plan summary ({key})")
        elif n:
            verb = _COUNTER_TEXT.get(key, key.replace("_", " "))
            out.append(f"{verb} {n} resource{'s' if n > 1 else ''}")
    if not (_count(s.get("add")) or 0) > 0 and not out:
        out.append("creates nothing (only changes outside resources, e.g. outputs)")
    return out


async def has_infrastructure(session: AsyncSession, env: Environment) -> bool:
    """Whether `env` may already hold infrastructure (§21.5): a stored state version, or a run of
    it that reached an apply or a command. Fail closed: with an unmanaged state, nobody can tell."""
    if not env.managed_state:
        return True
    state = (
        await session.execute(
            select(StateVersion.id).where(StateVersion.environment_id == env.id).limit(1)
        )
    ).scalar_one_or_none()
    if state is not None:
        return True
    applied = (
        await session.execute(
            select(RunEvent.id)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                Run.environment_id == env.id,
                RunEvent.to_state.in_((RunState.applying, RunState.running)),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return applied is not None


async def rollout_pin(session: AsyncSession, rollout_id: uuid.UUID, env: Environment) -> str | None:
    """The commit the rollout pinned for `env`'s stack and branch (§21.6): that of its first member
    run there that knew its commit. Later member runs of that stack — retries included — plan it."""
    return (
        await session.execute(
            select(Run.commit_sha)
            .join(Environment, Environment.id == Run.environment_id)
            .where(
                Run.rollout_id == rollout_id,
                Environment.stack_id == env.stack_id,
                Environment.branch.is_not_distinct_from(env.branch),
                Run.commit_sha.is_not(None),
            )
            .order_by(Run.created_at, Run.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def member_refusals(
    session: AsyncSession,
    rollout: EnvRollout,
    run: Run,
    env: Environment,
    summary: dict[str, Any] | None,
    checks: list[dict[str, Any]],
) -> list[str]:
    """Why this member's plan needs a human whatever the approval (§21.5): the plan itself, an
    environment that already holds infrastructure, or a commit other than the rollout's pin."""
    out = plan_refusals(
        run.type,
        summary,
        checks,
        used_mocks=run.used_mocks,
        used_secret_fallback=run.used_secret_fallback,
    )
    if await has_infrastructure(session, env):
        out.append("the environment already has infrastructure (applied before)")
    sha = (run.commit_sha or "").lower()
    if not _FULL_SHA.match(sha):
        out.append("its commit is not known")
    else:
        pin = await rollout_pin(session, rollout.id, env)
        if pin is not None and pin.lower() != sha:
            out.append(f"planned commit {sha[:12]}, not {pin[:12]} the rollout pinned")
    return out


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
    if membership is None and approver.role != Role.admin:
        # Removed from the space: no fallback to the instance defaults (§2.4, §21.5).
        return False
    return can_apply(approver, env, membership).allowed


async def approval_blocker(
    session: AsyncSession, rollout: EnvRollout, member: EnvRolloutMember, env: Environment
) -> str | None:
    """Why the approval no longer covers `member` (a pause reason closing the gate, §21.5), or
    None: expired, the approver lost the right to apply there, or the environment's tier,
    `protected` or four-eyes requirement changed since the approval — four-eyes is re-checked
    against the creator at that moment too."""
    from app.runs.service import needs_four_eyes

    if not approval_valid(rollout):
        return "approval_expired"
    if not await approver_may_apply(session, rollout, env):
        return "approver_lost_rights"
    four_eyes = await needs_four_eyes(session, env)
    seen = (member.approved_tier, member.approved_protected, member.approved_four_eyes)
    if seen != (env.tier, env.protected, four_eyes):
        return "member_changed_since_approval"
    if four_eyes and rollout.approved_by_user_id == rollout.created_by_user_id:
        return "member_changed_since_approval"
    return None


async def confirm_under_approval(
    session: AsyncSession, run: Run, *, fields: dict[str, Any] | None = None
) -> bool:
    """Confirm `run` (from planning, checking or unconfirmed) by the system under its rollout's
    approval if the gate passes (§21.5, §21.6); the confirmer recorded is the approver. Returns
    whether it did. The plan's own outcome (`fields`) is applied in the same transition."""
    from app.runs.transition import transition

    if run.rollout_id is None:
        return False
    # The rollout's lock first (§21.3), before anything is judged: a cancel or a re-approval
    # committed meanwhile is seen, and the lock order is the one every member path takes.
    rollout = await lock_rollout(session, run.rollout_id)
    if rollout is None or rollout.state not in (RolloutState.running, RolloutState.paused):
        return False
    if rollout.pause_reason in GATE_CLOSED:
        return False
    member = await session.get(
        EnvRolloutMember, (rollout.id, run.environment_id), populate_existing=True
    )
    env = await session.get(Environment, run.environment_id, populate_existing=True)
    if member is None or member.run_id != run.id or env is None:
        return False
    merged = {**(fields or {})}
    summary = merged.get("plan_summary", run.plan_summary)
    checks = (merged.get("check_results") or run.check_results or {}).get("checks") or []
    if await member_refusals(session, rollout, run, env, summary, checks):
        return False
    if await approval_blocker(session, rollout, member, env) is not None:
        return False  # the transition to unconfirmed then pauses the rollout with that reason
    approver = await session.get(User, rollout.approved_by_user_id)
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
            "approved_by_email": approver.email if approver else None,
        },
    )
    return True


# --- lifecycle ---


async def _start_member(session: AsyncSession, rollout: EnvRollout, m: EnvRolloutMember) -> None:
    """The member's plan: an ordinary tracked run, queued like any other (§21.4 step 3) — on the
    commit the rollout pinned for its stack and branch, once one is known (§21.6)."""
    env = await session.get(Environment, m.environment_id)
    pin = await rollout_pin(session, rollout.id, env) if env is not None else None
    run = Run(
        environment_id=m.environment_id,
        type=RunType.tracked,
        state=RunState.queued,
        triggered_by=TriggeredBy.rollout,
        # Four-eyes keeps its meaning on every member run (§21.3).
        trigger_user_id=rollout.created_by_user_id,
        rollout_id=rollout.id,
        commit_sha=pin,
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
            "commit": pin,
        },
    )


async def _set_state(
    session: AsyncSession,
    rollout: EnvRollout,
    state: RolloutState,
    reason: str | None = None,
) -> None:
    """The one writer of a rollout's state (§21.3). Audits every pause — a change of reason while
    paused too —, every resume (paused → running) and the terminal outcomes (§21.8)."""
    if rollout.state == state and rollout.pause_reason == reason:
        return
    before, before_reason = rollout.state, rollout.pause_reason
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
    }.get(state)
    if state == RolloutState.running and before == RolloutState.paused:
        action = "rollout.resumed"
    # A cancel is audited by its endpoint, with its user; a start (awaiting_approval → running)
    # by the approval.
    if action:
        await _audit(
            session,
            rollout,
            action,
            context={"reason": reason, "from": before.value, "previous_reason": before_reason},
        )
    if state == RolloutState.paused:
        await _notify_people(session, rollout, "rollout_paused", {"reason": reason})
    elif state in (RolloutState.succeeded, RolloutState.failed):
        await _notify_people(session, rollout, f"rollout_{state.value}")


async def _gate_closed_reason(
    session: AsyncSession, rollout: EnvRollout, members: list[EnvRolloutMember]
) -> str | None:
    """Whether the gate is closed for the whole rollout (§21.4, §21.5), and why: an expired
    approval, a reason kept until a re-approval, or a member waiting at `unconfirmed` with a plan
    the gate would take but that the approval no longer covers."""
    if not approval_valid(rollout):
        return "approval_expired"
    if rollout.pause_reason in _STICKY:
        return rollout.pause_reason
    for m in members:
        if m.status != M.needs_confirmation or m.run_id is None:
            continue
        run = await session.get(Run, m.run_id)
        env = await session.get(Environment, m.environment_id)
        if run is None or env is None or run.state != RunState.unconfirmed:
            continue
        checks = (run.check_results or {}).get("checks") or []
        if await member_refusals(session, rollout, run, env, run.plan_summary, checks):
            continue  # a human confirms it whatever the approval
        if (reason := await approval_blocker(session, rollout, m, env)) is not None:
            return reason
    return None


async def advance_rollout(session: AsyncSession, rollout: EnvRollout) -> None:
    """Start every member whose upstream members are all done — unless the gate is closed — then
    derive the rollout's state (§21.4). The ONE place a rollout's state changes after its
    creation (with the approval, the cancel and the retry that call `_set_state`)."""
    if rollout.state in TERMINAL or rollout.state == RolloutState.awaiting_approval:
        await _signal(session, rollout)
        return
    members = await members_of(session, rollout.id)
    by_env = {m.environment_id: m for m in members}
    edges = await member_edges(session, set(by_env))
    parents: dict[uuid.UUID, set[uuid.UUID]] = {e: set() for e in by_env}
    for up, down in edges:
        parents[down].add(up)

    blocker = await _gate_closed_reason(session, rollout, members)
    if blocker is None:
        startable = [
            m
            for m in members
            if m.status == M.pending
            and all(by_env[p].status == M.done for p in parents[m.environment_id])
        ]
        # The approval must still cover a member before its plan starts: else nothing starts
        # (a plan would only stop at `unconfirmed` and wait there).
        for m in startable:
            env = await session.get(Environment, m.environment_id, populate_existing=True)
            if env is not None and (reason := await approval_blocker(session, rollout, m, env)):
                blocker = reason
                break
        if blocker is None:
            for m in startable:
                await _start_member(session, rollout, m)

    statuses = {m.status for m in members}
    if not statuses & _OPEN:
        final = RolloutState.succeeded if statuses == {M.done} else RolloutState.failed
        await _set_state(session, rollout, final)
    elif blocker is not None:
        await _set_state(session, rollout, RolloutState.paused, blocker)
    elif M.needs_confirmation in statuses:
        await _set_state(session, rollout, RolloutState.paused, "needs_confirmation")
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
    assert run.rollout_id is not None
    rollout = await lock_rollout(session, run.rollout_id)
    if rollout is None:
        return
    member = await session.get(
        EnvRolloutMember, (rollout.id, run.environment_id), populate_existing=True
    )
    if member is None or member.run_id != run.id:
        return
    before = member.status
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
    if rollout.state != RolloutState.canceled and member.status != before:
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
    from app.runs.service import needs_four_eyes

    four_eyes = False
    for e in envs:
        if await needs_four_eyes(session, e):
            four_eyes = True
            break
    if four_eyes and user.id == rollout.created_by_user_id:
        return "You created this rollout (four-eyes required)."
    return None


def reapproval_due(rollout: EnvRollout) -> bool:
    """A started rollout whose gate a re-approval would reopen (§21.6)."""
    return rollout.state in (RolloutState.running, RolloutState.paused) and (
        rollout.pause_reason in GATE_CLOSED or not approval_valid(rollout)
    )


async def approve_rollout(session: AsyncSession, user: User, rollout: EnvRollout) -> None:
    """§21.6: all-or-nothing `can_apply` over the members, four-eyes against the creator, a
    time-boxed approval recording what it saw of each member; a re-approval also confirms the
    members whose plan waited only on it."""
    from app.runs.service import needs_four_eyes

    re_approval = reapproval_due(rollout)
    if rollout.state != RolloutState.awaiting_approval and not re_approval:
        raise ProblemException(409, "Not awaiting approval", f"State is {rollout.state.value}.")
    refusal = await approval_refusal(session, user, rollout)
    if refusal is not None:
        raise ProblemException(403, "Forbidden", refusal)
    members = await members_of(session, rollout.id)

    seen: dict[str, dict[str, Any]] = {}
    for m in members:
        env = await session.get(Environment, m.environment_id, populate_existing=True)
        if env is None:
            continue
        m.approved_tier = env.tier
        m.approved_protected = env.protected
        m.approved_four_eyes = await needs_four_eyes(session, env)
        seen[str(env.id)] = {
            "tier": env.tier,
            "protected": env.protected,
            "four_eyes": m.approved_four_eyes,
        }
    now = _now()
    rollout.approved_by_user_id = user.id
    rollout.approved_at = now
    rollout.approval_expires_at = now + timedelta(
        seconds=get_settings().stackd_rollout_approval_ttl_seconds
    )
    await _set_state(session, rollout, RolloutState.running)
    await _audit(
        session,
        rollout,
        "rollout.approved",
        user=user,
        context={
            "environments": seen,
            "expires_at": rollout.approval_expires_at.isoformat(),
            "re_approval": re_approval,
        },
    )
    # The gate runs again on every plan that waited at `unconfirmed` (§21.6).
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
        # Not yet executing: queued, awaiting a confirmation, or confirmed and not claimed yet.
        if run is not None and run.state in (
            RunState.queued,
            RunState.unconfirmed,
            RunState.confirmed,
        ):
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
    await _set_state(session, rollout, RolloutState.running)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Already rolling out", "An environment now belongs to another active rollout."
        ) from exc
    await _audit(session, rollout, "rollout.retried", user=user)
    await advance_rollout(session, rollout)


async def upstream_changed(session: AsyncSession, env_id: uuid.UUID, upstream_run: Run) -> None:
    """A run outside the rollout applied an upstream of `env_id`, a member of an active rollout:
    the cascade creates no run there (§9.2, §21.4). A member that has not planned yet will read
    the new outputs when its turn comes; one that already did is reported to the rollout's people
    rather than re-planned behind the rollout's back."""
    member = (
        await session.execute(
            select(EnvRolloutMember).where(
                EnvRolloutMember.environment_id == env_id, EnvRolloutMember.active
            )
        )
    ).scalar_one_or_none()
    if member is None or member.status == M.pending:
        return
    rollout = await session.get(EnvRollout, member.rollout_id)
    if rollout is None:
        return
    await _notify_people(
        session,
        rollout,
        "rollout_upstream_changed",
        {
            "environment_id": str(env_id),
            "upstream_env_id": str(upstream_run.environment_id),
            "upstream_run_id": str(upstream_run.id),
        },
    )


async def expire_approvals(session: AsyncSession, now: datetime) -> int:
    """Periodic task (§7.5): pause the started rollouts whose approval just expired, so the
    rollout shows `paused (approval_expired)` without waiting for its next event."""
    ids = list(
        (
            await session.execute(
                select(EnvRollout.id).where(
                    EnvRollout.state.in_((RolloutState.running, RolloutState.paused)),
                    EnvRollout.approval_expires_at <= now,
                    or_(
                        EnvRollout.pause_reason.is_(None),
                        EnvRollout.pause_reason != "approval_expired",
                    ),
                )
            )
        ).scalars()
    )
    for rollout_id in ids:
        rollout = await lock_rollout(session, rollout_id)
        if rollout is not None and rollout.state in (RolloutState.running, RolloutState.paused):
            await advance_rollout(session, rollout)
        await session.commit()
    if not ids:
        await session.commit()
    return len(ids)
