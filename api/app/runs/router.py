from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import Role, TriggeredBy
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.run_log import RunLog
from app.runs.schemas import LogChunkOut, RunOut, TriggerRunIn
from app.runs.service import cancel_run, confirm_run, discard_run, trigger_run

router = APIRouter(prefix="/api/v1", tags=["runs"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise ProblemException(404, "Run not found", None)
    return run


@router.post(
    "/environments/{env_id}/runs",
    response_model=RunOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.writer))],
)
async def create_run(
    env_id: uuid.UUID, body: TriggerRunIn, user: CurrentUser, session: DbSession
) -> Run:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    return await trigger_run(
        session,
        env,
        run_type=body.type,
        triggered_by=TriggeredBy.manual,
        user=user,
        commit_sha=body.commit_sha,
        group_root=body.with_downstream,
    )


@router.get("/environments/{env_id}/runs", response_model=list[RunOut])
async def list_runs(env_id: uuid.UUID, _: CurrentUser, session: DbSession) -> list[Run]:
    rows = (
        (
            await session.execute(
                select(Run).where(Run.environment_id == env_id).order_by(Run.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _: CurrentUser, session: DbSession) -> Run:
    return await _get_run(session, run_id)


@router.post("/runs/{run_id}/confirm", response_model=RunOut)
async def confirm(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await confirm_run(session, await _get_run(session, run_id), user)


@router.post(
    "/runs/{run_id}/discard",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def discard(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await discard_run(session, await _get_run(session, run_id), user)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def cancel(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await cancel_run(session, await _get_run(session, run_id), user)


@router.get("/runs/{run_id}/logs", response_model=list[LogChunkOut])
async def get_logs(
    run_id: uuid.UUID,
    _: CurrentUser,
    session: DbSession,
    phase: str | None = None,
    after_seq: int | None = None,
) -> list[LogChunkOut]:
    stmt = select(RunLog).where(RunLog.run_id == run_id)
    if phase:
        stmt = stmt.where(RunLog.phase == phase)
    if after_seq is not None:
        stmt = stmt.where(RunLog.seq > after_seq)
    rows = (await session.execute(stmt.order_by(RunLog.phase, RunLog.seq))).scalars().all()
    return [LogChunkOut(phase=r.phase, section=r.section, seq=r.seq, lines=r.lines) for r in rows]


@router.get("/runs/{run_id}/plan")
async def get_plan(run_id: uuid.UUID, _: CurrentUser, session: DbSession) -> dict:
    run = await _get_run(session, run_id)
    return {"plan_summary": run.plan_summary, "used_mocks": run.used_mocks}


@router.get("/runs/{run_id}/checks")
async def get_checks(run_id: uuid.UUID, _: CurrentUser, session: DbSession) -> dict:
    run = await _get_run(session, run_id)
    return {"check_results": run.check_results}
