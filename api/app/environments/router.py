from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.environments.schemas import EnvironmentCreate, EnvironmentOut, EnvironmentUpdate
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.stack import Stack
from app.stacks.git import ls_remote_sha
from app.variables.crud import create_variable, get_variable, update_variable, variables_for
from app.variables.resolution import resolve_variables
from app.variables.schemas import (
    ResolvedVariableOut,
    VariableCreate,
    VariableOut,
    VariableUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["environments"])
Writer = Depends(require_role(Role.writer))
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _get_env(session: AsyncSession, env_id: uuid.UUID) -> Environment:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    return env


def _env_audit_ctx(env: Environment) -> dict:
    return {"name": env.name, "tier": env.tier.value}


@router.get("/stacks/{stack_id}/environments", response_model=list[EnvironmentOut])
async def list_environments(
    stack_id: uuid.UUID, _: CurrentUser, session: DbSession
) -> list[EnvironmentOut]:
    rows = (
        (
            await session.execute(
                select(Environment)
                .where(Environment.stack_id == stack_id)
                .order_by(Environment.position, Environment.name)
            )
        )
        .scalars()
        .all()
    )
    return [EnvironmentOut.of(e) for e in rows]


@router.post(
    "/stacks/{stack_id}/environments",
    response_model=EnvironmentOut,
    status_code=201,
    dependencies=[Writer],
)
async def create_environment(
    stack_id: uuid.UUID, body: EnvironmentCreate, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    if (await session.get(Stack, stack_id)) is None:
        raise ProblemException(404, "Stack not found", None)
    env = Environment(stack_id=stack_id, **body.model_dump())
    if env.protected:
        env.autodeploy = False  # protected forces manual confirmation (§3.2)
    session.add(env)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Environment already exists", f"'{body.name}' exists on this stack."
        ) from exc
    await record_audit(
        session,
        action="environment.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env.id,
        context=_env_audit_ctx(env),
    )
    await session.commit()
    await session.refresh(env)
    return EnvironmentOut.of(env)


@router.get("/environments/{env_id}", response_model=EnvironmentOut)
async def get_environment(env_id: uuid.UUID, _: CurrentUser, session: DbSession) -> EnvironmentOut:
    return EnvironmentOut.of(await _get_env(session, env_id))


@router.patch("/environments/{env_id}", response_model=EnvironmentOut, dependencies=[Writer])
async def update_environment(
    env_id: uuid.UUID, body: EnvironmentUpdate, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    env = await _get_env(session, env_id)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(env, field, value)
    if env.protected:
        env.autodeploy = False
    await record_audit(
        session,
        action="environment.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env.id,
        context={"fields": sorted(changes), **_env_audit_ctx(env)},
    )
    await session.commit()
    await session.refresh(env)
    return EnvironmentOut.of(env)


@router.delete("/environments/{env_id}", status_code=204, dependencies=[Writer])
async def delete_environment(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    env = await _get_env(session, env_id)
    ctx = _env_audit_ctx(env)
    await session.delete(env)
    await record_audit(
        session,
        action="environment.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
        context=ctx,
    )
    await session.commit()


@router.post(
    "/environments/{env_id}/refresh-head", response_model=EnvironmentOut, dependencies=[Writer]
)
async def refresh_head(env_id: uuid.UUID, _: CurrentUser, session: DbSession) -> EnvironmentOut:
    env = await _get_env(session, env_id)
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    from app.crypto import decrypt

    secret = decrypt(stack.repo_secret_encrypted) if stack.repo_secret_encrypted else None
    sha = await ls_remote_sha(stack.repo_url, stack.repo_auth_kind, secret, env.branch)
    if sha is None:
        raise ProblemException(422, "Head refresh failed", "Could not read the branch head.")
    env.head_sha = sha
    env.head_updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(env)
    return EnvironmentOut.of(env)


# --- environment variables (override slot: stack_id + environment_id both set) ---


@router.get("/environments/{env_id}/variables", response_model=list[VariableOut])
async def list_env_variables(
    env_id: uuid.UUID, _: CurrentUser, session: DbSession
) -> list[VariableOut]:
    await _get_env(session, env_id)
    return [VariableOut.of(v) for v in await variables_for(session, environment_id=env_id)]


@router.post(
    "/environments/{env_id}/variables",
    response_model=VariableOut,
    status_code=201,
    dependencies=[Writer],
)
async def create_env_variable(
    env_id: uuid.UUID, body: VariableCreate, user: CurrentUser, session: DbSession
) -> VariableOut:
    env = await _get_env(session, env_id)
    var = await create_variable(session, body, stack_id=env.stack_id, environment_id=env.id)
    await record_audit(
        session,
        action="variable.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable",
        target_id=var.id,
        context={
            "name": var.name,
            "kind": var.kind.value,
            "sensitive": var.sensitive,
            "scope": "env",
        },
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.patch(
    "/environments/{env_id}/variables/{var_id}", response_model=VariableOut, dependencies=[Writer]
)
async def update_env_variable(
    env_id: uuid.UUID,
    var_id: uuid.UUID,
    body: VariableUpdate,
    user: CurrentUser,
    session: DbSession,
) -> VariableOut:
    var = await get_variable(session, var_id)
    if var.environment_id != env_id:
        raise ProblemException(404, "Variable not found", None)
    update_variable(var, body)
    await record_audit(
        session,
        action="variable.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable",
        target_id=var.id,
        context={
            "name": var.name,
            "kind": var.kind.value,
            "sensitive": var.sensitive,
            "scope": "env",
        },
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.delete("/environments/{env_id}/variables/{var_id}", status_code=204, dependencies=[Writer])
async def delete_env_variable(
    env_id: uuid.UUID, var_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    var = await get_variable(session, var_id)
    if var.environment_id != env_id:
        raise ProblemException(404, "Variable not found", None)
    name = var.name
    await session.delete(var)
    await record_audit(
        session,
        action="variable.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable",
        target_id=var_id,
        context={"name": name, "scope": "env"},
    )
    await session.commit()


@router.get("/environments/{env_id}/resolved-variables", response_model=list[ResolvedVariableOut])
async def resolved_variables(
    env_id: uuid.UUID, _: CurrentUser, session: DbSession
) -> list[ResolvedVariableOut]:
    env = await _get_env(session, env_id)
    resolved = await resolve_variables(session, env)
    return [ResolvedVariableOut.of(rv) for rv in resolved]
