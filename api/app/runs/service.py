from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.crypto import encrypt
from app.enums import (
    WORKER_EXECUTING_STATES,
    AuditActorKind,
    RunEventActor,
    RunState,
    RunType,
    SecretFallback,
    TriggeredBy,
)
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.user import User
from app.permissions import can_apply, effective_can_destroy
from app.runs.transition import transition
from app.spaces.service import get_membership


async def _env_membership(session: AsyncSession, user: User | None, env: Environment):  # type: ignore[no-untyped-def]
    """The user's membership in the env's space (§6, Phase F) — drives the effective apply gate.
    None for a system actor or an instance admin without an explicit membership."""
    if user is None:
        return None
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    return await get_membership(session, user.id, stack.space_id)


async def _check_overridable(
    session: AsyncSession, env: Environment, secret_overrides: dict[str, str]
) -> None:
    """A break-glass override is only accepted for a variable of `env` that is an external secret
    reference opted in with `secret_fallback_mode = break_glass` (§15.4) — any other name is
    refused (422), so an override can never stand in for a plain or sensitive variable. Names
    only in the error, never a value."""
    from app.variables.resolution import resolve_variables

    overridable = {
        rv.name
        for rv in await resolve_variables(session, env)
        if rv.is_reference and rv.secret_fallback_mode == SecretFallback.break_glass
    }
    refused = sorted(set(secret_overrides) - overridable)
    if refused:
        raise ProblemException(
            422,
            "Override not allowed",
            "A secret override needs a secret reference with secret_fallback_mode=break_glass; "
            f"not overridable: {', '.join(refused)}.",
        )


async def trigger_run(
    session: AsyncSession,
    env: Environment,
    *,
    run_type: RunType,
    triggered_by: TriggeredBy,
    user: User | None = None,
    commit_sha: str | None = None,
    group_root: bool = False,
    secret_overrides: dict[str, str] | None = None,
    is_drift: bool = False,
    plan_options: dict[str, list[str]] | None = None,
) -> Run:
    """Create a run in `queued` (SPECS §4). A plan changes nothing, so any writer+ may trigger;
    a `destroy` additionally requires can_destroy (§2.4). `group_root` starts a cascade group.
    `secret_overrides` is a break-glass map of variable→value used if the secret source is down
    (§15.4) — an apply-affecting bypass, so it requires can_apply and is sealed AES-GCM.
    `plan_options` (`targets`/`replace`, validated by TriggerRunIn) make a targeted / replace plan
    (§3.5): only a human's tracked or destroy trigger carries them — never a proposed, drift,
    cascade or rollout run — and such a run never auto-confirms (§4.2)."""
    if plan_options and run_type not in (RunType.tracked, RunType.destroy):
        raise ProblemException(422, "Plan options not allowed", "Only on a tracked or destroy run.")
    if plan_options and plan_options.get("replace") and run_type != RunType.tracked:
        raise ProblemException(422, "Plan options not allowed", "replace is tracked runs only.")
    # The membership only feeds the destroy / secret-override gates — don't query it for the
    # common plain trigger.
    membership = None
    if run_type == RunType.destroy or secret_overrides:
        membership = await _env_membership(session, user, env)
    if run_type == RunType.destroy and (
        user is None or not effective_can_destroy(user, membership)
    ):
        raise ProblemException(403, "Forbidden", "destroy permission required.")

    overrides_encrypted = None
    if secret_overrides:
        is_destroy = run_type == RunType.destroy
        if user is None or not can_apply(user, env, membership, is_destroy=is_destroy).allowed:
            raise ProblemException(
                403, "Forbidden", "Supplying secret overrides requires apply permission."
            )
        await _check_overridable(session, env, secret_overrides)
        overrides_encrypted = encrypt(json.dumps(secret_overrides))

    run = Run(
        environment_id=env.id,
        type=run_type,
        state=RunState.queued,
        triggered_by=triggered_by,
        trigger_user_id=user.id if (user and triggered_by == TriggeredBy.manual) else None,
        commit_sha=commit_sha,
        secret_overrides_encrypted=overrides_encrypted,
        is_drift=is_drift,
        plan_options=plan_options or None,
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
        context={
            "environment_id": str(env.id),
            "type": run_type.value,
            "tier": env.tier,
            **({"plan_options": plan_options} if plan_options else {}),
        },
    )
    await session.commit()
    await session.refresh(run)
    return run


async def promote_run(
    session: AsyncSession, source_env: Environment, target_env: Environment, user: User
) -> Run:
    """Promote the commit currently applied on `source_env` to `target_env` of the SAME stack: a
    tracked run on the target pinned to the commit of the source's last applied run (§9.7).
    Triggering a plan needs writer; the apply is still gated by can_apply + 4-eyes at confirm."""
    from sqlalchemy import or_, select

    if source_env.stack_id != target_env.stack_id:
        raise ProblemException(
            400, "Cross-stack promotion", "Source and target must belong to the same stack."
        )
    if source_env.id == target_env.id:
        raise ProblemException(400, "Same environment", "Pick a different source environment.")

    # The last apply that went through: a confirmed tracked/destroy run that finished. Not a PR or
    # drift plan (proposed: never applied — a PR's commit may not even be merged), a command run,
    # an empty plan (finished without a confirmation), or a targeted apply (§9.7: it applied only
    # part of its commit, so that commit is not what the environment runs).
    src = (
        await session.execute(
            select(Run)
            .where(
                Run.environment_id == source_env.id,
                Run.state == RunState.finished,
                Run.type.in_((RunType.tracked, RunType.destroy)),
                Run.confirmed_at.is_not(None),
                Run.commit_sha.is_not(None),
                or_(Run.plan_options.is_(None), ~Run.plan_options.has_key("targets")),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if src is None or src.type == RunType.destroy:
        # After a destroy nothing is applied on the source: there is no commit to promote.
        raise ProblemException(
            409, "Nothing to promote", "The source environment has no applied commit."
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
    from app.enums import Role
    from app.runs.commands import (
        ADMIN_ONLY_COMMANDS,
        ALLOWED_COMMANDS,
        CommandArgsError,
        is_mutating,
        normalize_args,
    )

    if command not in ALLOWED_COMMANDS:
        raise ProblemException(
            400, "Command not allowed", f"'{command}' is not in the allowlist of runnable commands."
        )
    # Args are passed verbatim to `<tool> <command> <args...>`. A leading-`-` arg (e.g. -chdir,
    # -state, -backend) would widen what a command run can touch beyond its gate: refused unless
    # the command's option allowlist names it (§4.3) — options first in the stored order.
    try:
        args = normalize_args(command, args)
    except CommandArgsError as exc:
        raise ProblemException(400, "Argument not allowed", str(exc)) from exc
    if command in ADMIN_ONLY_COMMANDS and user.role != Role.admin:
        # Parity with DELETE /environments/{id}/state/lock (instance admin, §11.2).
        raise ProblemException(403, "Forbidden", f"'{command}' requires the admin role.")
    if command == "force-unlock" and env.managed_state:
        raise ProblemException(
            409,
            "Managed state lock",
            "This environment's state is managed by Stackd, which owns its lock: release it with "
            f"DELETE /api/v1/environments/{env.id}/state/lock.",
        )
    if is_mutating(command):
        membership = await _env_membership(session, user, env)
        decision = can_apply(user, env, membership)
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


async def needs_four_eyes(session: AsyncSession, env: Environment) -> bool:
    """THE four-eyes rule (§2.4): the env's tier requires it, or the env is `protected`, or it sets
    `require_second_pair_of_eyes`. Fail closed: an env pointing at a missing tier row (should be
    impossible via the API, but never silently weaken a security control) requires it. Shared by
    a run's confirmation and a rollout's approval (§21.6)."""
    from sqlalchemy import select

    from app.models.tier import Tier

    tier_def = (
        await session.execute(select(Tier).where(Tier.name == env.tier))
    ).scalar_one_or_none()
    tier_four_eyes = tier_def.requires_four_eyes if tier_def else True
    return tier_four_eyes or env.protected or env.require_second_pair_of_eyes


async def confirm_run(session: AsyncSession, run: Run, user: User) -> Run:
    """unconfirmed → confirmed, gated by can_apply + 4-eyes + mock block (§2.4, §9.3)."""
    if run.state != RunState.unconfirmed:
        raise ProblemException(409, "Run not awaiting confirmation", f"State is {run.state.value}.")
    env = await session.get(Environment, run.environment_id)
    assert env is not None

    membership = await _env_membership(session, user, env)
    decision = can_apply(user, env, membership, is_destroy=run.type == RunType.destroy)
    if not decision.allowed:
        raise ProblemException(403, "Forbidden", decision.reason)

    # Mock block (§9.3): a plan that consumed mocks validates config, it is not applied by default.
    if run.used_mocks and not env.allow_mock_apply:
        raise ProblemException(
            409, "Apply disabled", "This run consumed mock outputs (allow_mock_apply is off)."
        )

    # Secret fallback block (§15.5): a value resolved via fallback is not the real secret.
    if run.used_secret_fallback and not env.allow_fallback_apply:
        raise ProblemException(
            409,
            "Apply disabled",
            "This run resolved a secret via fallback (allow_fallback_apply is off).",
        )

    # 4-eyes (§2.4): triggerer ≠ confirmer when the env's tier requires it (was hardcoded to prod;
    # now a per-tier flag), when the env is `protected` (CLAUDE.md §4: it forces confirmation +
    # 4-eyes, never who may apply), or when the env opts in. Human triggers only.

    if (
        await needs_four_eyes(session, env)
        # A rollout's member runs carry its creator as triggerer (§21.3).
        and run.triggered_by in (TriggeredBy.manual, TriggeredBy.rollout)
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
    """unconfirmed → discarded (§4.2): a plan awaiting confirmation is dropped, never applied. Any
    other run is canceled or stopped instead (`cancel_run`)."""
    if run.state != RunState.unconfirmed:
        raise ProblemException(
            409,
            "Cannot discard",
            f"Only a plan awaiting confirmation is discarded, not a {run.state.value} run.",
        )
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
    """Cancel a run (§4.1). Not claimed yet (`queued`, `confirmed`) or waiting on a human
    (`unconfirmed`): `canceled` at once. Executing: ask the worker to stop (see
    `request_cancel`) — the run ends when the worker reports it."""
    # A worker executing the job: a cancel there is a request the worker honours, not a transition.
    if run.state in WORKER_EXECUTING_STATES:
        return await request_cancel(session, run, user)
    if run.state not in (RunState.queued, RunState.confirmed, RunState.unconfirmed):
        raise ProblemException(409, "Cannot cancel", f"A {run.state.value} run cannot be canceled.")
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


async def request_cancel(
    session: AsyncSession, run: Run, user: User | None, *, reason: str | None = None
) -> Run:
    """Ask the worker executing `run` to stop it (SPECS §4.1, §7.1): record the request, audit it
    and queue a `cancel_job` command for the worker's next heartbeat — one transaction. The state
    does not change here: the worker interrupts the tool (SIGINT, a graceful stop), reports
    `job_canceled`, and only then does `transition()` end the run. Idempotent: asking again
    re-queues the command (the first may have been lost with a restarting worker) without a second
    audit. A job holding apply credentials (an apply, a mutating command) needs `can_apply` — the
    gate of starting it — since stopping it halfway leaves the infrastructure partially changed.
    `user=None` is the system — a closed pull request stopping its proposed runs (§18.3), with
    `reason` in the audit context; it never stops a job holding apply credentials."""
    from sqlalchemy import select

    from app.models.worker_command import WorkerCommand
    from app.workers.claim import holds_apply_credentials

    # Re-read under the row lock: the job may have finished between the route's load and now.
    locked = (
        await session.execute(
            select(Run)
            .where(Run.id == run.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if locked.state not in WORKER_EXECUTING_STATES or locked.worker_id is None:
        raise ProblemException(
            409, "Cannot cancel", f"A {locked.state.value} run cannot be canceled."
        )
    if holds_apply_credentials(locked):
        if user is None:
            raise ProblemException(409, "Cannot cancel", "The system never stops an apply.")
        env = await session.get(Environment, locked.environment_id)
        assert env is not None
        membership = await _env_membership(session, user, env)
        decision = can_apply(user, env, membership, is_destroy=locked.type == RunType.destroy)
        if not decision.allowed:
            raise ProblemException(403, "Forbidden", f"Stopping an apply: {decision.reason}")
    first = locked.cancel_requested_at is None
    if first:
        locked.cancel_requested_at = datetime.now(UTC)
        await record_audit(
            session,
            action="run.cancel_requested",
            actor_kind=AuditActorKind.user if user else AuditActorKind.system,
            actor_id=user.id if user else None,
            actor_email=user.email if user else None,
            target_kind="run",
            target_id=locked.id,
            context={
                "state": locked.state.value,
                "worker_id": str(locked.worker_id),
                "environment_id": str(locked.environment_id),
                **({"reason": reason} if reason else {}),
            },
        )
    session.add(
        WorkerCommand(
            worker_id=locked.worker_id, type="cancel_job", payload={"job_id": str(locked.id)}
        )
    )
    await session.commit()
    await session.refresh(locked)
    return locked
