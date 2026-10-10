from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.dependencies.service import DependencyError, capture_outputs, cascade
from app.drift.service import clear_drift, record_drift_result
from app.enums import (
    AuditActorKind,
    DriftStatus,
    RunEventActor,
    RunState,
    RunType,
    WorkerStatus,
)
from app.errors import ProblemException
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.run_log import RunLog
from app.models.worker import Worker
from app.models.worker_command import WorkerCommand
from app.runs.artifacts import (
    MAX_PLAN_BYTES,
    PLAN_FILE,
    SAVED_PLAN_TYPES,
    SavedPlanUnreadable,
    load_plan,
    save_plan,
)
from app.runs.transition import ALLOWED, transition
from app.secret_sources.providers import SecretUnavailable
from app.workers.auth import CurrentWorker, mint_worker_token, pool_from_token
from app.workers.claim import build_job_payload, claim_one
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


@router.post("/register", response_model=RegisterOut)
async def register(body: RegisterIn, request: Request, session: DbSession) -> RegisterOut:
    pool = await pool_from_token(request, session)
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
    worker.labels = body.labels
    worker.version = body.version
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


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    worker: CurrentWorker, session: DbSession, body: HeartbeatIn | None = None
) -> HeartbeatOut:
    worker.last_heartbeat_at = datetime.now(UTC)
    # The worker is authoritative for busy/idle via its reported in-flight count (§7, Phase E): a
    # multi-job worker stays busy while ≥1 job runs and flips to idle when all finish. A heartbeat
    # without a body (or count) just means online.
    if body is not None and body.in_flight is not None:
        worker.status = WorkerStatus.busy if body.in_flight > 0 else WorkerStatus.idle
    elif worker.status == WorkerStatus.offline:
        worker.status = WorkerStatus.idle

    # Deliver pending downward commands (diagnostics today; cancel_job later) and mark them sent.
    pending = (
        (
            await session.execute(
                select(WorkerCommand).where(
                    WorkerCommand.worker_id == worker.id, WorkerCommand.status == "pending"
                )
            )
        )
        .scalars()
        .all()
    )
    commands = [{"id": str(c.id), "type": c.type, "payload": c.payload} for c in pending]
    for c in pending:
        c.status = "sent"
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
    return {"ok": True}


@router.post("/jobs/claim", response_model=None)
async def claim(worker: CurrentWorker, session: DbSession, wait: int = 0) -> dict | Response:
    settings = get_settings()
    affinity = settings.stackd_apply_affinity_seconds
    deadline = wait
    while True:
        run = await claim_one(session, worker, affinity)
        if run is not None:
            worker.status = WorkerStatus.busy
            try:
                payload = await build_job_payload(session, run)
            except (DependencyError, SecretUnavailable) as exc:
                # Unresolvable dependency (§9.3) or external secret (§15.2) → fail the run closed,
                # hand the worker nothing.
                reason = "secret" if isinstance(exc, SecretUnavailable) else "dependency"
                await transition(
                    session,
                    run,
                    RunState.failed,
                    actor=RunEventActor.system,
                    fields={"error": str(exc)},
                    audit_action="run.apply_failed",
                    audit_context={"reason": reason, "detail": str(exc)},
                )
                await session.commit()
                return Response(status_code=204)
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
        if deadline <= 0:
            return Response(status_code=204)
        await asyncio.sleep(min(2, deadline))
        deadline -= 2


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
        return

    env = await session.get(Environment, run.environment_id)
    assert env is not None
    autoconfirm = env.autodeploy and not env.protected and not run.used_mocks and not warn
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
        if run.state == RunState.applying and body.phase == "after_apply":
            # The apply succeeded, an after_apply hook failed (§8.3): the run fails and nothing
            # cascades, but the infra has changed — its outputs are recorded (§9.1), and the state
            # matches reality again, so any drift is cleared as a finished apply does (§19).
            await capture_outputs(session, run, result.get("outputs") or {})
            applied_env = await session.get(Environment, run.environment_id)
            if applied_env is not None:
                await clear_drift(session, applied_env, datetime.now(UTC))
        checks = result.get("checks") or []
        failed_check = any(c.get("status") == "fail" for c in checks)
        action = "run.check_failed" if failed_check else "run.apply_failed"
        failed_fields: dict[str, Any] = {"error": result.get("error") or body.phase or "job failed"}
        if checks:  # the failing check, and the job's others: recorded like a finished job's
            failed_fields["check_results"] = _check_results(run, checks)
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

    if body.event == "phase_finished":
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
            # A successful apply reconciles state with reality → clears any drift (§19).
            env = await session.get(Environment, run.environment_id)
            if env is not None:
                await clear_drift(session, env, datetime.now(UTC))
            await cascade(session, run)
        else:
            await _decide_after_plan(session, worker, run, result)
        await session.commit()
        return {"ok": True}

    raise ProblemException(400, "Unknown event", body.event)


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


async def _read_capped(request: Request, limit: int) -> bytes:
    """The request body, refused (413) past `limit` bytes without buffering more than that."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ProblemException(413, "Artifact too large", f"The limit is {limit} bytes.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ProblemException(413, "Artifact too large", f"The limit is {limit} bytes.")
    return bytes(body)


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
    data = await _read_capped(request, MAX_PLAN_BYTES)
    if not data:
        raise ProblemException(400, "Empty artifact", "The plan file is empty.")
    # Re-check under the run's row lock, held until commit: a concurrent terminal transition
    # (worker_lost, cancel) either runs first — and this upload is refused — or after, and then
    # finds the key to purge. Never a saved plan left behind on a terminal run.
    locked = (
        await session.execute(
            select(Run)
            .where(Run.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    _check_plan_upload(locked, worker)
    locked.plan_artifact_key = await save_plan(locked.id, data)
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
