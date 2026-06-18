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
from app.models.user import User
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
from app.spaces import guard_env, guard_run

router = APIRouter(prefix="/api/v1", tags=["runs"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _get_run(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise ProblemException(404, "Run not found", None)
    await guard_run(session, user, run, min_role=min_role)
    return run


async def _get_env_scoped(
    session: AsyncSession, user: User, env_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Environment:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env, min_role=min_role)
    return env


@router.post(
    "/environments/{env_id}/runs",
    response_model=RunOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.writer))],
)
async def create_run(
    env_id: uuid.UUID, body: TriggerRunIn, user: CurrentUser, session: DbSession
) -> Run:
    env = await _get_env_scoped(session, user, env_id, min_role=Role.writer)
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
    env = await _get_env_scoped(session, user, env_id, min_role=Role.writer)
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
    target = await _get_env_scoped(session, user, env_id, min_role=Role.writer)
    source = await session.get(Environment, body.from_environment_id)
    if source is None:
        raise ProblemException(404, "Source environment not found", None)
    await guard_env(session, user, source, min_role=Role.writer)
    return await promote_run(session, source, target, user)


@router.get("/environments/{env_id}/runs", response_model=list[RunOut])
async def list_runs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[Run]:
    await _get_env_scoped(session, user, env_id)
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
    return await _get_run(session, user, run_id)


@router.post("/runs/{run_id}/confirm", response_model=RunOut)
async def confirm(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    return await confirm_run(session, await _get_run(session, user, run_id), user)


@router.post(
    "/runs/{run_id}/discard",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def discard(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    run = await _get_run(session, user, run_id, min_role=Role.writer)
    return await discard_run(session, run, user)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    dependencies=[Depends(require_role(Role.writer))],
)
async def cancel(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Run:
    run = await _get_run(session, user, run_id, min_role=Role.writer)
    return await cancel_run(session, run, user)


@router.post(
    "/runs/{run_id}/vcs/resync",
    status_code=202,
    dependencies=[Depends(require_role(Role.writer))],
)
async def vcs_resync(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Re-enqueue the VCS post-back for this run's current state (manual recovery, §18)."""
    run = await _get_run(session, user, run_id, min_role=Role.writer)
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
    await _get_run(session, user, run_id)
    stmt = select(RunLog).where(RunLog.run_id == run_id)
    if phase:
        stmt = stmt.where(RunLog.phase == phase)
    if after_seq is not None:
        stmt = stmt.where(RunLog.seq > after_seq)
    rows = (await session.execute(stmt.order_by(RunLog.phase, RunLog.seq))).scalars().all()
    return [LogChunkOut(phase=r.phase, section=r.section, seq=r.seq, lines=r.lines) for r in rows]


@router.get("/runs/{run_id}/plan")
async def get_plan(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    run = await _get_run(session, user, run_id)
    return {"plan_summary": run.plan_summary, "used_mocks": run.used_mocks}


@router.get("/runs/{run_id}/checks")
async def get_checks(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    run = await _get_run(session, user, run_id)
    return {"check_results": run.check_results}
