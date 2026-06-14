from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.enums import (
    AuditActorKind,
    RunEventActor,
    RunState,
    RunType,
    TriggeredBy,
)
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.user import User
from app.permissions import can_apply
from app.runs.transition import transition


async def trigger_run(
    session: AsyncSession,
    env: Environment,
    *,
    run_type: RunType,
    triggered_by: TriggeredBy,
    user: User | None = None,
    commit_sha: str | None = None,
    group_root: bool = False,
) -> Run:
    """Create a run in `queued` (SPECS §4). A plan changes nothing, so any writer+ may trigger;
    a `destroy` additionally requires can_destroy (§2.4). `group_root` starts a cascade group."""
    if run_type == RunType.destroy and (user is None or not user.can_destroy):
        raise ProblemException(403, "Forbidden", "destroy permission required.")

    run = Run(
        environment_id=env.id,
        type=run_type,
        state=RunState.queued,
        triggered_by=triggered_by,
        trigger_user_id=user.id if (user and triggered_by == TriggeredBy.manual) else None,
        commit_sha=commit_sha,
    )
    session.add(run)
    await session.flush()
    if group_root:
        run.run_group_id = run.id  # downstream cascade runs inherit this (§9.4)

    action = "run.destroy_triggered" if run_type == RunType.destroy else "run.triggered"
    await record_audit(
        session,
        action=action,
        actor_kind=AuditActorKind.user if user else AuditActorKind.system,
        actor_id=user.id if user else None,
        actor_email=user.email if user else None,
        target_kind="run",
        target_id=run.id,
        context={"environment_id": str(env.id), "type": run_type.value, "tier": env.tier.value},
    )
    await session.commit()
    await session.refresh(run)
    return run


async def promote_run(
    session: AsyncSession, source_env: Environment, target_env: Environment, user: User
) -> Run:
    """Promote the commit currently applied on `source_env` to `target_env` of the SAME stack: a
    tracked run on the target pinned to the source's last finished commit (§9.7). Triggering a plan
    needs writer; the apply is still gated by can_apply + 4-eyes at confirm time."""
    from sqlalchemy import select

    if source_env.stack_id != target_env.stack_id:
        raise ProblemException(
            400, "Cross-stack promotion", "Source and target must belong to the same stack."
        )
    if source_env.id == target_env.id:
        raise ProblemException(400, "Same environment", "Pick a different source environment.")

    src = (
        await session.execute(
            select(Run)
            .where(
                Run.environment_id == source_env.id,
                Run.state == RunState.finished,
                Run.commit_sha.is_not(None),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if src is None:
        raise ProblemException(
            409, "Nothing to promote", "The source environment has no applied commit yet."
        )

    run = Run(
        environment_id=target_env.id,
        type=RunType.tracked,
        state=RunState.queued,
        triggered_by=TriggeredBy.manual,
        trigger_user_id=user.id,
        commit_sha=src.commit_sha,
    )
    session.add(run)
    await session.flush()
    await record_audit(
        session,
        action="run.promoted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="run",
        target_id=run.id,
        context={
            "from_environment_id": str(source_env.id),
            "from_run_id": str(src.id),
            "target_environment_id": str(target_env.id),
            "commit": src.commit_sha,
        },
    )
    await session.commit()
    await session.refresh(run)
    return run


async def trigger_command_run(
    session: AsyncSession,
    env: Environment,
    user: User,
    *,
    command: str,
    args: list[str],
    commit_sha: str | None = None,
) -> Run:
    """Create a one-off `command` run (an allowlisted tofu/terraform subcommand, §4.3). Read-only
    commands need only writer (the route gate); mutating ones require `can_apply`."""
    from app.permissions import can_apply
    from app.runs.commands import ALLOWED_COMMANDS, is_mutating

    if command not in ALLOWED_COMMANDS:
        raise ProblemException(
            400, "Command not allowed", f"'{command}' is not in the allowlist of runnable commands."
        )
    if is_mutating(command):
        decision = can_apply(user, env)
        if not decision.allowed:
            raise ProblemException(403, "Forbidden", decision.reason)

    run = Run(
        environment_id=env.id,
        type=RunType.command,
        state=RunState.queued,
        triggered_by=TriggeredBy.manual,
        trigger_user_id=user.id,
        commit_sha=commit_sha,
        command={"name": command, "args": args},
    )
    session.add(run)
    await session.flush()
    await record_audit(
        session,
        action="run.command_triggered",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="run",
        target_id=run.id,
        context={"environment_id": str(env.id), "command": command, "args": args},
    )
    await session.commit()
    await session.refresh(run)
    return run


async def confirm_run(session: AsyncSession, run: Run, user: User) -> Run:
    """unconfirmed → confirmed, gated by can_apply + 4-eyes + mock block (§2.4, §9.3)."""
    if run.state != RunState.unconfirmed:
        raise ProblemException(409, "Run not awaiting confirmation", f"State is {run.state.value}.")
    env = await session.get(Environment, run.environment_id)
    assert env is not None

    decision = can_apply(user, env, is_destroy=run.type == RunType.destroy)
    if not decision.allowed:
        raise ProblemException(403, "Forbidden", decision.reason)

    # Mock block (§9.3): a plan that consumed mocks validates config, it is not applied by default.
    if run.used_mocks and not env.allow_mock_apply:
        raise ProblemException(
            409, "Apply disabled", "This run consumed mock outputs (allow_mock_apply is off)."
        )

    # 4-eyes (§2.4): triggerer ≠ confirmer for prod or when required, human triggers only.
    needs_four_eyes = env.tier.value == "prod" or env.require_second_pair_of_eyes
    if (
        needs_four_eyes
        and run.triggered_by == TriggeredBy.manual
        and run.trigger_user_id is not None
        and run.trigger_user_id == user.id
    ):
        raise ProblemException(403, "Forbidden", "You triggered this run (4-eyes required).")

    await transition(
        session,
        run,
        RunState.confirmed,
        actor=RunEventActor.user,
        actor_id=user.id,
        actor_email=user.email,
        fields={"confirmed_by_user_id": user.id, "confirmed_at": datetime.now(UTC)},
        audit_action="run.confirmed",
        audit_context={"environment_id": str(env.id), "commit": run.commit_sha},
    )
    await session.commit()
    await session.refresh(run)
    return run


async def discard_run(session: AsyncSession, run: Run, user: User) -> Run:
    await transition(
        session,
        run,
        RunState.discarded,
        actor=RunEventActor.user,
        actor_id=user.id,
        actor_email=user.email,
        audit_action="run.discarded",
    )
    await session.commit()
    await session.refresh(run)
    return run


async def cancel_run(session: AsyncSession, run: Run, user: User) -> Run:
    # MVP: cancel queued/unconfirmed directly. Cancelling an in-flight run via the heartbeat
    # command channel (SIGINT) is a follow-up (§7.1).
    if run.state not in (RunState.queued, RunState.unconfirmed):
        raise ProblemException(
            409, "Cannot cancel", f"Cancelling a {run.state.value} run needs worker signalling."
        )
    await transition(
        session,
        run,
        RunState.canceled,
        actor=RunEventActor.user,
        actor_id=user.id,
        actor_email=user.email,
        audit_action="run.canceled",
    )
    await session.commit()
    await session.refresh(run)
    return run
