from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.config import get_settings
from app.crypto import decrypt
from app.db import get_session
from app.enums import AuditActorKind, Role, VariableKind
from app.environments.schemas import EnvironmentCreate, EnvironmentOut, EnvironmentUpdate
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.ratelimit import rate_limit
from app.spaces import guard_env, require_space_access
from app.stacks.git import clone_shallow, enforce_clone_budget, ls_remote_sha
from app.variables.crud import create_variable, get_variable, update_variable, variables_for
from app.variables.discovery import parse_inputs, placeholder
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


async def _get_env(
    session: AsyncSession, user: User, env_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Environment:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env, min_role=min_role)
    return env


async def _guard_stack_id(
    session: AsyncSession, user: User, stack_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Stack:
    stack = await session.get(Stack, stack_id)
    if stack is None:
        raise ProblemException(404, "Stack not found", None)
    await require_space_access(session, user, stack.space_id, min_role=min_role)
    return stack


def _env_audit_ctx(env: Environment) -> dict:
    return {"name": env.name, "tier": env.tier}


async def _require_tier(session: AsyncSession, name: str) -> None:
    exists = (await session.execute(select(Tier).where(Tier.name == name))).scalar_one_or_none()
    if exists is None:
        raise ProblemException(422, "Unknown tier", f"No tier named '{name}'. Define it first.")


@router.get("/stacks/{stack_id}/environments", response_model=list[EnvironmentOut])
async def list_environments(
    stack_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[EnvironmentOut]:
    await _guard_stack_id(session, user, stack_id)
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
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    await _require_tier(session, body.tier)
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
async def get_environment(
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    return EnvironmentOut.of(await _get_env(session, user, env_id))


@router.patch("/environments/{env_id}", response_model=EnvironmentOut, dependencies=[Writer])
async def update_environment(
    env_id: uuid.UUID, body: EnvironmentUpdate, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    env = await _get_env(session, user, env_id, min_role=Role.writer)
    changes = body.model_dump(exclude_unset=True)
    if "tier" in changes:
        await _require_tier(session, changes["tier"])
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
    env = await _get_env(session, user, env_id, min_role=Role.writer)
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
async def refresh_head(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> EnvironmentOut:
    env = await _get_env(session, user, env_id, min_role=Role.writer)
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
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[VariableOut]:
    await _get_env(session, user, env_id)
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
    env = await _get_env(session, user, env_id, min_role=Role.writer)
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
    await _get_env(session, user, env_id, min_role=Role.writer)
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
    await _get_env(session, user, env_id, min_role=Role.writer)
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
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ResolvedVariableOut]:
    env = await _get_env(session, user, env_id)
    resolved = await resolve_variables(session, env)
    return [ResolvedVariableOut.of(rv) for rv in resolved]


@router.post(
    "/environments/{env_id}/discover-inputs",
    dependencies=[Writer, Depends(rate_limit("discover", per_minute=10, burst=5))],
)
async def discover_inputs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Introspect the repo (shallow clone + HCL parse, no terraform run) and create the REQUIRED
    root-module inputs that aren't already resolved, as env variables with empty placeholders."""
    env = await _get_env(session, user, env_id, min_role=Role.writer)
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    settings = get_settings()
    secret = decrypt(stack.repo_secret_encrypted) if stack.repo_secret_encrypted else None
    dest = await clone_shallow(stack.repo_url, stack.repo_auth_kind, secret, env.branch)
    try:
        enforce_clone_budget(dest, max_mb=settings.stackd_discovery_max_repo_mb)  # §H size cap
        root = (dest / stack.project_root).resolve()
        if not str(root).startswith(str(dest.resolve())):  # project_root must stay inside the repo
            raise ProblemException(400, "Invalid project root", None)
        tf_count = sum(1 for _ in root.glob("*.tf"))
        if tf_count > settings.stackd_discovery_max_tf_files:
            raise ProblemException(
                413,
                "Too many Terraform files",
                f"{tf_count} .tf files exceed the discovery cap "
                f"({settings.stackd_discovery_max_tf_files}).",
            )
        required = [i for i in parse_inputs(root) if i.required]
        resolved = await resolve_variables(session, env)
        present = {rv.name for rv in resolved if rv.kind == VariableKind.terraform}
        created: list[str] = []
        skipped: list[str] = []
        for inp in required:
            if inp.name in present:
                skipped.append(inp.name)
                continue
            await create_variable(
                session,
                VariableCreate(
                    kind=VariableKind.terraform,
                    name=inp.name,
                    value=placeholder(inp),
                    sensitive=inp.sensitive,
                    hcl=inp.hcl,
                ),
                stack_id=env.stack_id,
                environment_id=env.id,
            )
            created.append(inp.name)
        if created:
            await record_audit(
                session,
                action="environment.inputs_discovered",
                actor_kind=AuditActorKind.user,
                actor_id=user.id,
                actor_email=user.email,
                target_kind="environment",
                target_id=env.id,
                context={"created": created, "skipped": skipped},
            )
        await session.commit()
        return {"created": created, "skipped": skipped, "required_total": len(required)}
    finally:
        shutil.rmtree(dest, ignore_errors=True)
