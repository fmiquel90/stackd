from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.dependencies.service import DependencyError, capture_outputs, cascade
from app.enums import (
    AuditActorKind,
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
from app.runs.transition import ALLOWED, transition
from app.workers.auth import CurrentWorker, mint_worker_token, pool_from_token
from app.workers.claim import build_job_payload, claim_one
from app.workers.schemas import (
    CommandResultIn,
    EventIn,
    HeartbeatOut,
    LogIn,
    RegisterIn,
    RegisterOut,
)

router = APIRouter(prefix="/worker/v1", tags=["worker"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
_log = get_logger("stackd.worker")

_PHASE_TO_STATE = {
    "planning": RunState.planning,
    "checking": RunState.checking,
    "applying": RunState.applying,
}


@router.post("/register", response_model=RegisterOut)
async def register(body: RegisterIn, request: Request, session: DbSession) -> RegisterOut:
    pool = await pool_from_token(request, session)
    worker = Worker(
        pool_id=pool.id,
        name=body.name,
        labels=body.labels,
        version=body.version,
        last_heartbeat_at=datetime.now(UTC),
    )
    session.add(worker)
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
async def heartbeat(worker: CurrentWorker, session: DbSession) -> HeartbeatOut:
    worker.last_heartbeat_at = datetime.now(UTC)
    if worker.status == WorkerStatus.offline:
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
            except DependencyError as exc:
                # Unresolvable dependency (§9.3) → fail the run, hand the worker nothing.
                await transition(
                    session,
                    run,
                    RunState.failed,
                    actor=RunEventActor.system,
                    fields={"error": str(exc)},
                    audit_action="run.apply_failed",
                    audit_context={"reason": "dependency"},
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


async def _decide_after_plan(session: AsyncSession, worker: Worker, run: Run, result: dict) -> None:
    """Plan finished: set summary/checks, route to finished / unconfirmed / confirmed (§4.2)."""
    checks = result.get("checks", [])
    warn = any(c.get("status") == "warn" for c in checks)
    fields = {"plan_summary": result.get("summary"), "check_results": {"checks": checks}}

    if warn:
        await record_audit(
            session,
            action="run.check_warned",
            actor_kind=AuditActorKind.worker,
            actor_id=worker.id,
            target_kind="run",
            target_id=run.id,
            context={"checks": [c.get("name") for c in checks if c.get("status") == "warn"]},
        )

    if not result.get("has_changes", False) or run.type == RunType.proposed:
        # Empty diff, or a proposed (PR) run → plan-only, terminal.
        await transition(
            session,
            run,
            RunState.finished,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields=fields,
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
            await transition(session, run, target, actor=RunEventActor.worker, actor_id=worker.id)
            await session.commit()
        return {"ok": True}

    if body.event == "job_failed":
        failed_check = any(c.get("status") == "fail" for c in result.get("checks", []))
        action = "run.check_failed" if failed_check else "run.apply_failed"
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields={"error": result.get("error") or body.phase or "job failed"},
            audit_action=action,
            audit_context={"phase": body.phase},
        )
        await session.commit()
        return {"ok": True}

    if body.event == "phase_finished":
        if run.state == RunState.applying:
            # Capture outputs (§9.1) then finish, then cascade downstream (§9.2) — same txn.
            await capture_outputs(session, run, result.get("outputs") or {})
            await transition(
                session,
                run,
                RunState.finished,
                actor=RunEventActor.worker,
                actor_id=worker.id,
                audit_action="run.applied",
                audit_context={"environment_id": str(run.environment_id), "commit": run.commit_sha},
            )
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


@router.put("/jobs/{job_id}/artifacts/{name}", status_code=204)
async def put_artifact(
    job_id: uuid.UUID, name: str, request: Request, worker: CurrentWorker, session: DbSession
) -> Response:
    await _load_owned_run(session, worker, job_id)
    await request.body()  # drain — S3 archival of artifacts is a follow-up (§11.3)
    _ = name
    return Response(status_code=204)
