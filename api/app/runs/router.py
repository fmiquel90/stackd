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
from app.models.run import Run
from app.models.run_log import RunLog
from app.models.vcs import VcsOutbox
from app.runs.schemas import CommandTriggerIn, LogChunkOut, PromoteIn, RunOut, TriggerRunIn
from app.runs.service import (
    cancel_run,
    confirm_run,
    discard_run,
    promote_run,
    trigger_command_run,
    trigger_run,
)
from app.spaces import guard_env_id, guard_run_id

router = APIRouter(prefix="/api/v1", tags=["runs"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/environments/{env_id}/runs",
    response_model=RunOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.writer))],
)
async def create_run(
    env_id: uuid.UUID, body: TriggerRunIn, user: CurrentUser, session: DbSession
) -> Run:
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    return await trigger_run(
        session,
        env,
        run_type=body.type,
        triggered_by=TriggeredBy.manual,
        user=user,
        commit_sha=body.commit_sha,
        group_root=body.with_downstream,
        secret_overrides=body.secret_overrides,
    )


@router.post(
    "/environments/{env_id}/commands",
    response_model=RunOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.writer))],
)
async def run_command(
    env_id: uuid.UUID, body: CommandTriggerIn, user: CurrentUser, session: DbSession
) -> Run:
    """Run a one-off allowlisted tofu/terraform subcommand (import, state rm, …) as a `command`
    run. Read-only commands need writer; mutating ones additionally require can_apply (§4.3)."""
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    return await trigger_command_run(
        session, env, user, command=body.command, args=body.args, commit_sha=body.commit_sha
    )


@router.post(
    "/environments/{env_id}/promote",
    response_model=RunOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.writer))],
)
async def promote(env_id: uuid.UUID, body: PromoteIn, user: CurrentUser, session: DbSession) -> Run:
    """Promote the commit currently applied on `from_environment_id` to this environment (same
    stack). Creates a tracked run pinned to that commit; the apply is gated as usual at confirm."""
    target = await guard_env_id(session, user, env_id, min_role=Role.writer)
    source = await guard_env_id(
        session,
        user,
        body.from_environment_id,
        min_role=Role.writer,
        not_found="Source environment not found",
    )
    return await promote_run(session, source, target, user)


@router.get("/environments/{env_id}/runs", response_model=list[RunOut])
async def list_runs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[Run]:
    await guard_env_id(session, user, env_id)
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
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await guard_run_id(session, user, run_id)


@router.post("/runs/{run_id}/confirm", response_model=RunOut)
async def confirm(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await confirm_run(session, await guard_run_id(session, user, run_id), user)


@router.post(
    "/runs/{run_id}/discard",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def discard(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    run = await guard_run_id(session, user, run_id, min_role=Role.writer)
    return await discard_run(session, run, user)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def cancel(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    run = await guard_run_id(session, user, run_id, min_role=Role.writer)
    return await cancel_run(session, run, user)


@router.post(
    "/runs/{run_id}/vcs/resync",
    status_code=202,
    dependencies=[Depends(require_role(Role.writer))],
)
async def vcs_resync(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Re-enqueue the VCS post-back for this run's current state (manual recovery, §18)."""
    run = await guard_run_id(session, user, run_id, min_role=Role.writer)
    if not run.vcs_provider:
        raise ProblemException(400, "Not a VCS run", "This run has no linked pull request.")
    session.add(VcsOutbox(run_id=run.id, to_state=run.state.value))
    await session.commit()
    return {"queued": True}


@router.get("/runs/{run_id}/logs", response_model=list[LogChunkOut])
async def get_logs(
    run_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    phase: str | None = None,
    after_seq: int | None = None,
) -> list[LogChunkOut]:
    await guard_run_id(session, user, run_id)
    stmt = select(RunLog).where(RunLog.run_id == run_id)
    if phase:
        stmt = stmt.where(RunLog.phase == phase)
    if after_seq is not None:
        stmt = stmt.where(RunLog.seq > after_seq)
    rows = (await session.execute(stmt.order_by(RunLog.phase, RunLog.seq))).scalars().all()
    return [LogChunkOut(phase=r.phase, section=r.section, seq=r.seq, lines=r.lines) for r in rows]


@router.get("/runs/{run_id}/plan")
async def get_plan(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    run = await guard_run_id(session, user, run_id)
    return {"plan_summary": run.plan_summary, "used_mocks": run.used_mocks}


@router.get("/runs/{run_id}/checks")
async def get_checks(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    run = await guard_run_id(session, user, run_id)
    return {"check_results": run.check_results}
