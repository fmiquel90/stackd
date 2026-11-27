from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.bodies import read_capped
from app.config import get_settings
from app.db import get_session
from app.dependencies.service import DependencyError, capture_outputs, cascade
from app.drift.out_of_band import (
    clear_on_apply,
    observes_state,
    parse_observation,
    record_observation,
)
from app.drift.service import clear_drift, record_drift_result
from app.enums import (
    WORKER_EXECUTING_STATES,
    AuditActorKind,
    DriftStatus,
    RunEventActor,
    RunState,
    RunType,
    WorkerStatus,
)
from app.errors import ProblemException
from app.logging import get_logger
from app.models.audit import AuditEvent
from app.models.environment import Environment
from app.models.run import Run
from app.models.run_log import RunLog
from app.models.worker import Worker
from app.models.worker_command import WorkerCommand
from app.oidc.issuer import WorkloadSigningError, WorkloadSigningUnavailable
from app.runs.artifacts import (
    MAX_PLAN_BYTES,
    PLAN_FILE,
    SAVED_PLAN_TYPES,
    SavedPlanUnreadable,
    drop_unrecorded_plan,
    load_plan,
    record_saved_plan,
    save_plan,
)
from app.runs.transition import ALLOWED, transition
from app.secret_sources.providers import SecretUnavailable
from app.workers.auth import CurrentWorker, mint_worker_token, pool_from_token
from app.workers.claim import (
    build_job_payload,
    claim_one,
    defer_claim,
    holds_apply_credentials,
    is_transient_db_error,
)
from app.workers.reaping import fail_lost_job, reclaimable
from app.workers.schemas import (
    CommandResultIn,
    EventIn,
    HeartbeatIn,
    HeartbeatOut,
    LogIn,
    RegisterIn,
    RegisterOut,
)

router = APIRouter(prefix="/worker/v1", tags=["worker"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
_log = get_logger("stackd.worker")

# A full commit SHA (SHA-1, or SHA-256 object format) — what a worker reports it checked out.
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# The states of a run's plan job (claim → plan → after_plan checks): the only window in which its
# saved plan may be uploaded.
_PLAN_JOB_STATES = frozenset({RunState.preparing, RunState.planning, RunState.checking})

_PHASE_TO_STATE = {
    "planning": RunState.planning,
    "checking": RunState.checking,
    "applying": RunState.applying,
    "running": RunState.running,  # RunType.command execution
}


# The only runner a production API accepts (§7.4): one ephemeral, hardened container per command.
# `local` runs every command as the agent's own OS user — trusted dev only.
PRODUCTION_RUNNER = "docker"


def _runner_refused(runner: str | None) -> ProblemException:
    return ProblemException(
        403,
        "Runner not allowed",
        f"STACKD_ENV=production accepts only the `{PRODUCTION_RUNNER}` runner; this worker "
        f"declared `{runner or 'none'}`. Set STACKD_RUNNER={PRODUCTION_RUNNER} on the worker.",
        code="runner_not_allowed",
    )


@router.post("/register", response_model=RegisterOut)
async def register(body: RegisterIn, request: Request, session: DbSession) -> RegisterOut:
    pool = await pool_from_token(request, session)
    if get_settings().is_production and body.runner != PRODUCTION_RUNNER:
        # Audited and committed before refusing: an operator sees which worker tried. Once per
        # (pool, worker name) and hour — whatever runner it declares — the agent keeps retrying,
        # the audit must not grow by one row per attempt.
        context = {"name": body.name, "runner": body.runner, "reason": "runner_not_allowed"}
        since = datetime.now(UTC) - timedelta(hours=1)
        recent = (
            await session.execute(
                select(AuditEvent.id)
                .where(
                    AuditEvent.action == "worker.rejected",
                    AuditEvent.target_id == pool.id,
                    AuditEvent.created_at >= since,
                    AuditEvent.context["name"].astext == body.name,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent is None:
            await record_audit(
                session,
                action="worker.rejected",
                actor_kind=AuditActorKind.worker,
                target_kind="worker_pool",
                target_id=pool.id,
                context=context,
            )
            await session.commit()
        raise _runner_refused(body.runner)
    # Reuse the row for this (pool, name) if it exists: a worker that restarts/hot-reloads is the
    # same worker — re-inserting would litter the list with stale offline duplicates. Most-recent
    # wins if legacy duplicates predate this behaviour.
    worker = (
        await session.execute(
            select(Worker)
            .where(Worker.pool_id == pool.id, Worker.name == body.name)
            .order_by(Worker.last_heartbeat_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if worker is None:
        worker = Worker(pool_id=pool.id, name=body.name)
        session.add(worker)
    # Effective labels, what the claim matches an environment's labels against (§3.7/§7.2): the
    # worker's own, then the pool's on top. The pool wins on a key both set: its labels are set by
    # an admin, the worker's are self-declared by the host — a worker of a `tier: dev` pool must not
    # promote itself to prod runs. Computed at each registration (a pool's labels never change).
    labels = {**(body.labels or {}), **(pool.labels or {})}
    worker.labels = labels or None
    worker.version = body.version
    worker.runner = body.runner
    worker.tools = body.tools
    worker.status = WorkerStatus.idle
    worker.last_heartbeat_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(worker)
    _log.info(
        "worker registered",
        extra={
            "event": "worker.registered",
            "worker_id": str(worker.id),
            "worker_name": worker.name,
            "pool": pool.name,
        },
    )
    return RegisterOut(worker_id=worker.id, worker_token=mint_worker_token(worker.id, pool.id))


# A command delivered without a result (lost response, worker restarted, failed result post) is
# delivered again after this long, up to `_COMMAND_MAX_DELIVERIES` times, then failed. The worker
# handles every command idempotently (§7.1).
_COMMAND_REDELIVERY = timedelta(seconds=90)
_COMMAND_MAX_DELIVERIES = 3


async def _count_unlisted_jobs(
    session: AsyncSession, worker: Worker, job_ids: list[uuid.UUID], now: datetime
) -> None:
    """The heartbeat lists the jobs the worker is running (§7.1). Each executing run of this
    worker it leaves out counts one more unlisted heartbeat — only once its claim is past
    `stackd_worker_lost_seconds`, so a claim response still in flight or a beat sent before the
    claim never counts; a listed run starts over. The scheduler fails the run once it has been left
    out `UNLISTED_HEARTBEATS_TO_REAP` times in a row (§3.7)."""
    mine = (Run.worker_id == worker.id) & Run.state.in_(WORKER_EXECUTING_STATES)
    claim_grace = now - timedelta(seconds=get_settings().stackd_worker_lost_seconds)
    await session.execute(
        update(Run)
        .where(mine, Run.id.in_(job_ids), Run.unlisted_heartbeats > 0)
        .values(unlisted_heartbeats=0)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Run)
        .where(mine, Run.id.not_in(job_ids), Run.claimed_at < claim_grace)
        .values(unlisted_heartbeats=Run.unlisted_heartbeats + 1)
        .execution_options(synchronize_session=False)
    )


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    worker: CurrentWorker, session: DbSession, body: HeartbeatIn | None = None
) -> HeartbeatOut:
    now = datetime.now(UTC)
    worker.last_heartbeat_at = now
    # The worker is authoritative for busy/idle via its reported in-flight count (§7, Phase E): a
    # multi-job worker stays busy while ≥1 job runs and flips to idle when all finish. A heartbeat
    # without a body (or count) just means online.
    if body is not None and body.in_flight is not None:
        worker.status = WorkerStatus.busy if body.in_flight > 0 else WorkerStatus.idle
    elif worker.status == WorkerStatus.offline:
        worker.status = WorkerStatus.idle
    # An agent that predates the job listing sends none: its runs are reaped once it is offline.
    if body is not None and body.job_ids is not None:
        await _count_unlisted_jobs(session, worker, body.job_ids, now)

    # Deliver pending downward commands (diagnostics, cancel_job), and again those delivered a
    # while ago that never got a result.
    due = (
        (
            await session.execute(
                select(WorkerCommand)
                .where(
                    WorkerCommand.worker_id == worker.id,
                    or_(
                        WorkerCommand.status == "pending",
                        and_(
                            WorkerCommand.status == "sent",
                            WorkerCommand.sent_at < now - _COMMAND_REDELIVERY,
                        ),
                    ),
                )
                .order_by(WorkerCommand.created_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    commands = []
    for c in due:
        if c.attempts >= _COMMAND_MAX_DELIVERIES:
            c.status = "failed"
            c.result = {"error": f"no result from the worker after {c.attempts} deliveries"}
            c.completed_at = now
            continue
        c.status = "sent"
        c.sent_at = now
        c.attempts += 1
        commands.append({"id": str(c.id), "type": c.type, "payload": c.payload})
    await session.commit()
    return HeartbeatOut(commands=commands)


@router.post("/commands/{command_id}/result")
async def command_result(
    command_id: uuid.UUID, body: CommandResultIn, worker: CurrentWorker, session: DbSession
) -> dict:
    cmd = await session.get(WorkerCommand, command_id)
    if cmd is None or cmd.worker_id != worker.id:
        raise ProblemException(404, "Command not found", None)
    cmd.status = body.status
    cmd.result = body.result
    cmd.completed_at = datetime.now(UTC)
    await session.commit()
    _log.info(
        "command result",
        extra={"event": "worker.command_result", "worker_id": str(worker.id), "type": cmd.type},
    )
    if (
        cmd.type == "cancel_job"
        and body.status == "done"
        and (body.result or {}).get("running_here") is False
    ):
        await _end_job_not_running(session, worker, (cmd.payload or {}).get("job_id"))
    return {"ok": True}


async def _end_job_not_running(session: AsyncSession, worker: Worker, job_id: object) -> None:
    """The worker the run is assigned to answered a stop request with `running_here: false`: the
    job runs nowhere and the run would otherwise never end (§4.1). It is failed `job_not_running`
    under the reaper's guards (`reclaimable`): the claim must be old enough that its response can
    no longer be in flight, and a job holding apply credentials waits for the apply window."""
    try:
        run = await session.get(Run, uuid.UUID(str(job_id)))
    except ValueError:
        return
    if (
        run is None
        or run.worker_id != worker.id
        or run.state not in WORKER_EXECUTING_STATES
        or not reclaimable(run, datetime.now(UTC))
    ):
        return
    try:
        await fail_lost_job(session, run, "job_not_running")
        await session.commit()
    except ProblemException:  # the run moved on underneath (its job reported after all)
        await session.rollback()


@router.post("/jobs/claim", response_model=None)
async def claim(worker: CurrentWorker, session: DbSession, wait: int = 0) -> dict | Response:
    settings = get_settings()
    # A worker registered before the API switched to production (its token is still valid) must
    # not take a job either: the runner is checked where the job is handed out.
    if settings.is_production and worker.runner != PRODUCTION_RUNNER:
        raise _runner_refused(worker.runner)
    affinity = settings.stackd_apply_affinity_seconds
    worker_id = worker.id
    deadline = max(0, min(wait, settings.stackd_claim_max_wait_seconds))
    while True:
        run = await claim_one(session, worker, affinity)
        if run is not None:
            return await _hand_out(session, worker, run)
        if deadline <= 0:
            return Response(status_code=204)
        # End the transaction before waiting: a long-poll must not sit idle in transaction on a
        # pooled connection — a pool's worth of idle workers would starve every other request.
        await session.rollback()
        await asyncio.sleep(min(2, deadline))
        deadline -= 2
        # The rollback expired the worker (labels, tools): reload it for the next attempt.
        reloaded = await session.get(Worker, worker_id, populate_existing=True)
        if reloaded is None:
            raise ProblemException(401, "Not authenticated", "Worker unregistered.")
        worker = reloaded


async def _hand_out(session: AsyncSession, worker: Worker, run: Run) -> dict[str, Any] | Response:
    """Build the claimed run's job and hand it out, or undo / fail the claim (§7.2, §3.11). The
    worker's row is written at the commit only: the payload build (secret fetches, KMS) must not
    hold its lock, which the worker's own heartbeat and the scheduler's worker-lost sweep take."""
    run_id = run.id
    try:
        # In a savepoint: what the payload build stages on the run (used_mocks, inputs,
        # provenance, used_secret_fallback) and its audit events (`dependency.mock_consumed`,
        # `secret.fallback_used`) only stand for a job actually handed out.
        async with session.begin_nested():
            payload = await build_job_payload(session, run)
    except Exception as exc:
        if isinstance(exc, WorkloadSigningUnavailable) or is_transient_db_error(exc):
            # Transient (KMS throttled/unreachable, the database, §3.11): undo the claim — the run
            # stays queued/confirmed, an approved apply keeps its approval —, keep the run out of
            # the claim for its backoff so the runs behind it are claimed meanwhile, and tell the
            # worker to come back.
            code = exc.code if isinstance(exc, WorkloadSigningUnavailable) else "db_unavailable"
            await session.rollback()
            try:
                await defer_claim(session, run_id)
            except Exception as defer_exc:
                await session.rollback()
                _log.warning(
                    "claim backoff not recorded",
                    extra={
                        "event": "worker.claim_defer_failed",
                        "run_id": str(run_id),
                        "error_type": type(defer_exc).__name__,
                    },
                )
            _log.warning(
                "job deferred",
                extra={
                    "event": "worker.claim_deferred",
                    "run_id": str(run_id),
                    "code": code,
                    "error_type": type(exc).__name__,
                },
            )
            raise ProblemException(
                503,
                "Job unavailable",
                "The job cannot be built right now; retry later.",
                code=code,
                retryable=True,
            ) from exc
        # Anything else fails the run closed, the worker getting nothing: an unresolvable
        # dependency (§9.3), external secret (§15.2) or workload token (§10) — or an unexpected
        # error (an undecryptable stored secret, a bug), which would otherwise fail every poll on
        # the same run at the head of the queue.
        if isinstance(exc, (DependencyError, SecretUnavailable, WorkloadSigningError)):
            error = str(exc)
            reason = (
                "secret"
                if isinstance(exc, SecretUnavailable)
                else "oidc"
                if isinstance(exc, WorkloadSigningError)
                else "dependency"
            )
        else:
            # The exception's type only, on the run and in the audit: its message may carry a
            # value. The stack goes to the server log.
            error, reason = f"job_build_failed:{type(exc).__name__}", "internal"
            _log.exception(
                "job build failed",
                extra={"event": "worker.claim_build_failed", "run_id": str(run_id)},
            )
        await session.refresh(run)  # the savepoint's rollback expired the staged fields
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.system,
            fields={"error": error},
            audit_action="run.apply_failed",
            audit_context={"reason": reason, "detail": error},
        )
        await session.commit()
        return Response(status_code=204)
    worker.status = WorkerStatus.busy
    await session.commit()
    _log.info(
        "job claimed",
        extra={
            "event": "worker.claim",
            "worker_id": str(worker.id),
            "run_id": payload["job_id"],
            "phase": payload["phase"],
        },
    )
    return payload


async def _load_owned_run(session: AsyncSession, worker: Worker, job_id: uuid.UUID) -> Run:
    run = await session.get(Run, job_id)
    if run is None:
        raise ProblemException(404, "Run not found", None)
    if run.worker_id != worker.id:
        raise ProblemException(409, "Not your job", "This run is claimed by another worker.")
    return run


async def _pin_commit(session: AsyncSession, worker: Worker, run: Run, reported: object) -> str:
    """The commit the plan job checked out (§7.2/§7.3), to record on the run: the apply job then
    clones exactly it, and staleness (§9.6) compares it with the branch head. A run already pinned
    to a full SHA must have checked out that very commit — a mismatch fails the run (409). A ref
    (branch/tag) the API can't resolve itself is replaced by the commit it resolved to."""
    sha = reported.lower() if isinstance(reported, str) else ""
    if not _FULL_SHA.match(sha):
        raise ProblemException(400, "Invalid commit", "commit_sha must be a full commit SHA.")
    pinned = (run.commit_sha or "").lower()
    if _FULL_SHA.match(pinned) and pinned != sha:
        error = (
            f"commit_mismatch: the run is pinned to {run.commit_sha}, the worker checked out {sha}"
        )
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields={"error": error},
            audit_action="run.apply_failed",
            audit_context={
                "reason": "commit_mismatch",
                "commit": run.commit_sha,
                "checked_out": sha,
            },
        )
        await session.commit()
        raise ProblemException(409, "Commit mismatch", error)
    return sha


def _check_results(run: Run, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """`runs.check_results` with a job's `checks` added to what earlier jobs left (§8.3).
    `results`: every check result (ok, warn, fail), each with the stage it ran at; `checks`: the
    warn/fail ones, what needs attention — the field's meaning before `results` existed."""
    prior = run.check_results or {}
    results = [*(prior.get("results", prior.get("checks")) or []), *checks]
    return {"checks": [c for c in results if c.get("status") != "ok"], "results": results}


async def _audit_warned_checks(
    session: AsyncSession, worker: Worker, run: Run, checks: list[dict[str, Any]]
) -> bool:
    """Audit `run.check_warned` (§8.3) when any of `checks` warned — with the stages they ran
    at, so a plan's warning tells from an apply's; True if one did."""
    warned = [c for c in checks if c.get("status") == "warn"]
    if warned:
        await record_audit(
            session,
            action="run.check_warned",
            actor_kind=AuditActorKind.worker,
            actor_id=worker.id,
            target_kind="run",
            target_id=run.id,
            context={
                "checks": [c.get("name") for c in warned],
                "stages": list(dict.fromkeys(c["stage"] for c in warned if c.get("stage"))),
            },
        )
    return bool(warned)


async def _decide_after_plan(session: AsyncSession, worker: Worker, run: Run, result: dict) -> None:
    """Plan finished: set summary/checks, route to finished / unconfirmed / confirmed (§4.2)."""
    checks = result.get("checks", [])
    fields = {"plan_summary": result.get("summary"), "check_results": _check_results(run, checks)}
    warn = await _audit_warned_checks(session, worker, run, checks)

    if run.is_drift:
        # A drift plan (§19): record state-vs-reality on the env, then finish like any proposed run.
        env = await session.get(Environment, run.environment_id)
        assert env is not None
        status = DriftStatus.drifted if result.get("has_changes", False) else DriftStatus.in_sync
        await record_drift_result(session, run, env, status)

    if not result.get("has_changes", False) or run.type == RunType.proposed:
        # Empty diff, or a proposed (PR) run → plan-only, terminal. Terminal → audited (§4.2).
        await transition(
            session,
            run,
            RunState.finished,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields=fields,
            audit_action="run.plan_finished",
            audit_context={"has_changes": result.get("has_changes", False)},
        )
        # An empty plan in a run group may be the last parent a downstream waits for (§9.2).
        if run.run_group_id is not None:
            await cascade(session, run)
        return

    # A rollout member (§21.5): a creates-only plan is confirmed under the rollout's approval.
    # (A rollout member never carries plan options; the guard keeps a targeted plan human-only.)
    if run.rollout_id is not None and not run.plan_options:
        from app.rollouts.service import confirm_under_approval

        if await confirm_under_approval(session, run, fields=fields):
            return

    env = await session.get(Environment, run.environment_id)
    assert env is not None
    # A rollout member that did not pass the rollout gate stops for a human (§21.4 step 4) — the
    # env's own autodeploy never confirms it (a changing or destroying plan, a canceled rollout).
    # Never a destroy either: only an approver passing can_apply (+ can_destroy) confirms one, and
    # never a plan its apply gates would refuse (mocks §9.3, a secret fallback §15.5) — those stop
    # at `unconfirmed`, where the block is shown, rather than fail at the apply claim. Never a
    # targeted / replace plan (plan options, §3.5): a partial or forced change is always a human's.
    autoconfirm = (
        run.rollout_id is None
        and run.type == RunType.tracked
        and not run.plan_options
        and env.autodeploy
        and not env.protected
        and not run.used_mocks
        # Like a mocked plan: a fallback secret never autodeploys, even where the env allows
        # fallback applies — that flag lets an approver apply one, not the system (§15.5).
        and not run.used_secret_fallback
        and not warn
    )
    if autoconfirm:
        await transition(
            session,
            run,
            RunState.confirmed,
            actor=RunEventActor.system,
            fields={**fields, "confirmed_at": datetime.now(UTC)},
            audit_action="run.confirmed",
            audit_context={"autodeploy": True},
        )
    else:
        await transition(
            session,
            run,
            RunState.unconfirmed,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields=fields,
        )


async def _end_canceled(
    session: AsyncSession,
    worker: Worker,
    run: Run,
    phase: str | None,
    checks: list[dict[str, Any]],
) -> None:
    """End a run whose cancel was requested (§4.1). Before any apply credentials were used it is
    `canceled`. A job holding them (an apply, a mutating command) may have changed infrastructure
    before it stopped, so it is `failed` — `applying` is never `canceled` (§4.2) — with the
    `canceled_during_apply` error, which the UI words as a possibly partial change; nothing
    cascades. The requester is in the `run.cancel_requested` audit event."""
    fields: dict[str, Any] | None = None
    if checks:
        fields = {"check_results": _check_results(run, checks)}
        await _audit_warned_checks(session, worker, run, checks)
    context = {"phase": phase, "reason": "cancel_requested"}
    if holds_apply_credentials(run):
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields={**(fields or {}), "error": "canceled_during_apply"},
            audit_action="run.apply_failed",
            audit_context=context,
        )
    else:
        await transition(
            session,
            run,
            RunState.canceled,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields=fields,
            audit_action="run.canceled",
            audit_context=context,
        )


@router.post("/jobs/{job_id}/events")
async def post_event(
    job_id: uuid.UUID, body: EventIn, worker: CurrentWorker, session: DbSession
) -> dict:
    run = await _load_owned_run(session, worker, job_id)
    result = body.result or {}

    if body.event == "phase_started":
        target = _PHASE_TO_STATE.get(body.phase or "")
        if target and target in ALLOWED.get(run.state, set()):
            fields = None
            if result.get("commit_sha") is not None:
                fields = {
                    "commit_sha": await _pin_commit(session, worker, run, result["commit_sha"])
                }
            await transition(
                session, run, target, actor=RunEventActor.worker, actor_id=worker.id, fields=fields
            )
            await session.commit()
        return {"ok": True}

    if body.event == "job_failed":
        # Only a job a worker is executing can fail, and only its worker reports it (checked
        # above): a late or duplicate report on a plan already awaiting a human, confirmed, or
        # over is stale — 409, like any other event the run's state no longer accepts (§7.3).
        if run.state not in WORKER_EXECUTING_STATES:
            raise ProblemException(
                409, "Job not executing", f"A {run.state.value} run has no job to fail."
            )
        if run.state == RunState.applying and body.phase == "after_apply":
            # The apply succeeded, an after_apply hook failed (§8.3): the run fails and nothing
            # cascades, but the infra has changed — its outputs are recorded (§9.1), and the state
            # matches reality again, so any drift is cleared as a finished apply does (§19) —
            # unless the apply was targeted (§19.4: it reconciled only part of the env).
            await capture_outputs(session, run, result.get("outputs") or {})
            applied_env = await session.get(Environment, run.environment_id)
            if applied_env is not None and not run.is_targeted:
                await clear_drift(session, applied_env, datetime.now(UTC))
                await clear_on_apply(session, applied_env, run)
        checks = result.get("checks") or []
        failed_check = any(c.get("status") == "fail" for c in checks)
        action = "run.check_failed" if failed_check else "run.apply_failed"
        failed_fields: dict[str, Any] = {"error": result.get("error") or body.phase or "job failed"}
        if checks:
            # The job's checks so far, whatever ended it (a failing check, a tofu error, a
            # timeout…): recorded, and a warning among them audited, like a finished job's (§8.3)
            # — in the failure's transaction.
            failed_fields["check_results"] = _check_results(run, checks)
            await _audit_warned_checks(session, worker, run, checks)
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields=failed_fields,
            audit_action=action,
            audit_context={"phase": body.phase},
        )
        if run.is_drift:
            env = await session.get(Environment, run.environment_id)
            if env is not None:
                await record_drift_result(session, run, env, DriftStatus.error)
        await session.commit()
        return {"ok": True}

    if body.event == "job_canceled":
        # The worker stopped the job on a user's request (§4.1, §7.1). Never on the worker's own
        # say-so: a cancel must have been requested.
        if run.cancel_requested_at is None:
            raise ProblemException(409, "No cancel requested", "Nobody asked to stop this run.")
        await _end_canceled(session, worker, run, body.phase, result.get("checks") or [])
        await session.commit()
        return {"ok": True}

    if body.event == "phase_finished":
        if run.cancel_requested_at is not None and run.state in (
            RunState.preparing,
            RunState.planning,
            RunState.checking,
        ):
            # The plan finished before the stop reached it: honour the request — never confirm
            # (autodeploy) or hold for approval a plan the user asked to stop.
            await _end_canceled(session, worker, run, body.phase, [])
            await session.commit()
            return {"ok": True}
        if run.state == RunState.running:
            # A `command` run finished (no outputs/cascade — it's a one-off subcommand).
            await transition(
                session,
                run,
                RunState.finished,
                actor=RunEventActor.worker,
                actor_id=worker.id,
                audit_action="run.command_executed",
                audit_context={"command": (run.command or {}).get("name")},
            )
        elif run.state == RunState.applying:
            # Capture outputs (§9.1) then finish, then cascade downstream (§9.2) — same txn.
            await capture_outputs(session, run, result.get("outputs") or {})
            # Apply-stage checks (before_apply/after_apply, §8.3) join the plan's in check_results
            # and are audited like them; the run is already confirmed.
            apply_checks = result.get("checks") or []
            check_fields: dict[str, Any] | None = None
            if apply_checks:
                check_fields = {"check_results": _check_results(run, apply_checks)}
                await _audit_warned_checks(session, worker, run, apply_checks)
            await transition(
                session,
                run,
                RunState.finished,
                actor=RunEventActor.worker,
                actor_id=worker.id,
                fields=check_fields,
                audit_action="run.applied",
                audit_context={"environment_id": str(run.environment_id), "commit": run.commit_sha},
            )
            # A successful apply reconciles state with reality → clears any drift (§19). Not a
            # targeted apply: it reconciled only its targets, the rest may still drift (§19.4).
            env = await session.get(Environment, run.environment_id)
            if env is not None and not run.is_targeted:
                await clear_drift(session, env, datetime.now(UTC))
                # Its plan saw the out-of-band change and was reviewed (§19.7).
                await clear_on_apply(session, env, run)
            await cascade(session, run)
        else:
            await _decide_after_plan(session, worker, run, result)
        await session.commit()
        return {"ok": True}

    if body.event == "state_observed":
        await _observe_state(session, run, result)
        await session.commit()
        return {"ok": True}

    raise ProblemException(400, "Unknown event", body.event)


async def _observe_state(session: AsyncSession, run: Run, result: dict[str, Any]) -> None:
    """A job's reading of its state's serial/lineage (§19.7) — no run state changes. Only from a
    job that was handed `observe_state`, while it executes; a baseline only from a job holding
    apply credentials (it wrote the state)."""
    serial, lineage = parse_observation(result)
    # Env then run, the claim's lock order: a run reclaimed meanwhile (worker_lost) and its env
    # handed to the next run can't have its late observation recorded.
    env = (
        await session.execute(
            select(Environment)
            .where(Environment.id == run.environment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    run = (
        await session.execute(
            select(Run)
            .where(Run.id == run.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if run.state not in WORKER_EXECUTING_STATES:
        raise ProblemException(
            409, "Job not executing", f"A {run.state.value} run has no job to observe the state."
        )
    if not observes_state(run, env):
        raise ProblemException(
            409, "State not observed", "This job's state is not observed (§19.7)."
        )
    baseline = result.get("baseline") is True
    if baseline and not holds_apply_credentials(run):
        raise ProblemException(
            409, "Not a state write", "Only a job holding apply credentials records a baseline."
        )
    await record_observation(
        session, env, run, serial, lineage, baseline=baseline, now=datetime.now(UTC)
    )


@router.post("/jobs/{job_id}/logs", status_code=202)
async def ingest_logs(
    job_id: uuid.UUID, body: LogIn, worker: CurrentWorker, session: DbSession
) -> dict:
    await _load_owned_run(session, worker, job_id)
    # Idempotent on (run_id, phase, seq) — chunk retries are no-ops (§5.1).
    stmt = (
        pg_insert(RunLog)
        .values(
            run_id=job_id, phase=body.phase, seq=body.seq, section=body.section, lines=body.lines
        )
        .on_conflict_do_nothing(index_elements=["run_id", "phase", "seq"])
    )
    await session.execute(stmt)
    signal = f'{{"kind":"log","run_id":"{job_id}","phase":"{body.phase}","max_seq":{body.seq}}}'
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(chan=f"run_{job_id}", payload=signal)
    )
    await session.commit()
    return {"accepted": True}


def _check_plan_upload(run: Run, worker: Worker) -> None:
    if run.worker_id != worker.id:
        raise ProblemException(409, "Not your job", "This run is claimed by another worker.")
    if run.type not in SAVED_PLAN_TYPES:
        raise ProblemException(409, "No saved plan", f"A {run.type.value} run is never applied.")
    if run.state not in _PLAN_JOB_STATES:
        # Once planned, the reviewed plan is frozen: no worker can replace it before the apply.
        raise ProblemException(
            409, "Plan upload refused", "A saved plan is accepted only during the run's plan job."
        )


@router.put("/jobs/{job_id}/artifacts/{name}", status_code=204)
async def put_artifact(
    job_id: uuid.UUID, name: str, request: Request, worker: CurrentWorker, session: DbSession
) -> Response:
    run = await _load_owned_run(session, worker, job_id)
    if name != PLAN_FILE:
        await request.body()  # drain — S3 archival of plan.json/outputs.json is a follow-up (§11.3)
        return Response(status_code=204)

    # The saved plan (§7.4): the exact plan the apply job will apply. Encrypted at rest — it holds
    # the run's variable values in clear (app.runs.artifacts).
    _check_plan_upload(run, worker)
    await session.commit()  # hold no DB connection while the body streams in
    data = await read_capped(request, MAX_PLAN_BYTES, "Artifact too large")
    if not data:
        raise ProblemException(400, "Empty artifact", "The plan file is empty.")
    # Encrypt and upload before taking the row lock (a fresh key per upload: the reviewed plan's
    # object is never overwritten), then re-check under that lock, held until commit: a concurrent
    # terminal transition either runs first — this upload is refused and its object dropped — or
    # after, and then finds the key to purge. Never a saved plan left behind on a terminal run.
    key = await save_plan(job_id, data)
    try:
        locked = (
            await session.execute(
                select(Run)
                .where(Run.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        _check_plan_upload(locked, worker)
    except ProblemException:
        await session.rollback()
        await drop_unrecorded_plan(key)
        raise
    record_saved_plan(session, locked, key)
    await session.commit()
    _log.info(
        "saved plan stored",
        extra={"event": "worker.plan_saved", "run_id": str(job_id), "size_bytes": len(data)},
    )
    return Response(status_code=204)


@router.get("/jobs/{job_id}/artifacts/{name}")
async def get_artifact(
    job_id: uuid.UUID, name: str, worker: CurrentWorker, session: DbSession
) -> Response:
    """The saved plan, for the run's apply job only (§7.4) — the worker that holds the run in
    `applying`. Missing or unreadable → an error, never anything else to apply."""
    run = await _load_owned_run(session, worker, job_id)
    if name != PLAN_FILE:
        raise ProblemException(404, "Artifact not found", f"No downloadable artifact {name!r}.")
    if run.state != RunState.applying:
        raise ProblemException(
            409, "Not an apply job", "The saved plan is served only to the run's apply job."
        )
    if run.plan_artifact_key is None:
        raise ProblemException(
            404, "No saved plan", "This run has no saved plan to apply; trigger a new plan."
        )
    try:
        data = await load_plan(run.plan_artifact_key)
    except SavedPlanUnreadable:
        _log.error(
            "saved plan unreadable",
            extra={"event": "worker.plan_unreadable", "run_id": str(run.id)},
        )
        raise ProblemException(
            500,
            "Saved plan unreadable",
            "The stored plan failed its integrity check: not applied. Trigger a new plan.",
        ) from None
    if data is None:
        raise ProblemException(
            404, "Saved plan missing", "The stored plan object is missing; trigger a new plan."
        )
    return Response(content=data, media_type="application/octet-stream")
