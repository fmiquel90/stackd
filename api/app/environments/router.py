from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.config import get_settings
from app.crypto import decrypt
from app.db import get_session
from app.enums import (
    ACTIVE_STATES,
    AttachmentTarget,
    AuditActorKind,
    Role,
    RunState,
    RunType,
    VariableKind,
)
from app.environments.schemas import EnvironmentCreate, EnvironmentOut, EnvironmentUpdate
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.notification import NotificationTarget
from app.models.run import Run
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.models.variable_set import VariableSetAttachment
from app.ratelimit import rate_limit
from app.spaces import guard_env_id, guard_stack_id
from app.stacks.git import clone_shallow, enforce_clone_budget, ls_remote_sha
from app.variables.crud import create_variable, get_variable, update_variable, variables_for
from app.variables.discovery import DiscoveredInput, parse_inputs, placeholder, tf_files
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


def _env_audit_ctx(env: Environment) -> dict:
    return {"name": env.name, "tier": env.tier}


def _scan_clone(
    dest: Path, project_root: str, *, max_mb: int, max_tf_files: int
) -> list[DiscoveredInput]:
    """Size-cap the clone (§H), locate the project root and parse its REQUIRED inputs. Synchronous
    filesystem work — run via asyncio.to_thread, never directly on the event loop."""
    enforce_clone_budget(dest, max_mb=max_mb)
    # project_root (or a symlinked dir on its way) must resolve inside the clone — a path-wise
    # check, not a string prefix (`/tmp/x` would otherwise admit `/tmp/x2`).
    root = (dest / project_root).resolve()
    if not root.is_relative_to(dest.resolve()):
        raise ProblemException(400, "Invalid project root", None)
    tf_count = len(tf_files(root, dest))  # exactly the files parse_inputs will read
    if tf_count > max_tf_files:
        raise ProblemException(
            413,
            "Too many Terraform files",
            f"{tf_count} .tf files exceed the discovery cap ({max_tf_files}).",
        )
    return [i for i in parse_inputs(root, dest) if i.required]


async def _envs_out(session: AsyncSession, envs: Sequence[Environment]) -> list[EnvironmentOut]:
    """Serialize envs with their git staleness (§9.6). `last_applied_sha` is the commit of each
    env's last finished tracked run — a tracked run only finishes after an apply or an empty plan —
    fetched for the whole batch in one query."""
    last_applied: dict[uuid.UUID, str | None] = {}
    if envs:
        rows = await session.execute(
            select(Run.environment_id, Run.commit_sha)
            .where(
                Run.environment_id.in_([e.id for e in envs]),
                Run.type == RunType.tracked,
                Run.state == RunState.finished,
            )
            .order_by(Run.environment_id, Run.finished_at.desc(), Run.id.desc())
            .distinct(Run.environment_id)
        )
        last_applied = {env_id: sha for env_id, sha in rows.all()}
    return [EnvironmentOut.of(e, last_applied_sha=last_applied.get(e.id)) for e in envs]


async def _env_out(session: AsyncSession, env: Environment) -> EnvironmentOut:
    return (await _envs_out(session, [env]))[0]


async def _require_tier(session: AsyncSession, name: str) -> None:
    exists = (await session.execute(select(Tier).where(Tier.name == name))).scalar_one_or_none()
    if exists is None:
        raise ProblemException(422, "Unknown tier", f"No tier named '{name}'. Define it first.")


async def lock_envs_for_deletion(
    session: AsyncSession, *criteria: ColumnElement[bool]
) -> list[uuid.UUID]:
    """Lock the envs about to be deleted (a stack's, or one) and refuse with 409 while any has an
    active run (§3.5): the DB cascade would delete that run outside `transition()` (§4.2), leaving
    a worker executing a run that no longer exists. A claim locks its env row too (FOR UPDATE OF e
    SKIP LOCKED, §7.2), so no run starts on these envs until the deletion commits. Returns their
    ids. Queued runs aren't executing anywhere: they go with their env."""
    env_ids = list(
        (await session.execute(select(Environment.id).where(*criteria).with_for_update()))
        .scalars()
        .all()
    )
    if env_ids:
        active = (
            await session.execute(
                select(Run.id, Run.environment_id)
                .where(Run.environment_id.in_(env_ids), Run.state.in_(ACTIVE_STATES))
                .limit(1)
            )
        ).first()
        if active is not None:
            raise ProblemException(
                409,
                "Active run",
                "A run is in progress on this environment: cancel or discard it, or let it "
                "finish, before deleting.",
                run_id=str(active.id),
                environment_id=str(active.environment_id),
            )
    return env_ids


async def drop_target_references(
    session: AsyncSession,
    user: User,
    *,
    env_ids: Sequence[uuid.UUID],
    stack_id: uuid.UUID | None = None,
) -> None:
    """Variable-set attachments (§3.4), platform hooks (§8.1) and notification targets point at a
    stack or an env by `target_kind`/`target_id`, with no FK: remove those targeting the envs (and
    stack) being deleted, in the deletion's transaction, each audited like its manual removal."""
    reason = "stack_deleted" if stack_id is not None else "environment_deleted"

    def _targeting(
        model: type[VariableSetAttachment] | type[Hook] | type[NotificationTarget],
    ) -> ColumnElement[bool]:
        targets = [
            and_(model.target_kind == AttachmentTarget.environment, model.target_id.in_(env_ids))
        ]
        if stack_id is not None:
            targets.append(
                and_(model.target_kind == AttachmentTarget.stack, model.target_id == stack_id)
            )
        return or_(*targets)

    async def _audit(action: str, kind: str, row_id: uuid.UUID, context: dict[str, str]) -> None:
        await record_audit(
            session,
            action=action,
            actor_kind=AuditActorKind.user,
            actor_id=user.id,
            actor_email=user.email,
            target_kind=kind,
            target_id=row_id,
            context={**context, "reason": reason},
        )

    attachments = await session.execute(
        delete(VariableSetAttachment)
        .where(_targeting(VariableSetAttachment))
        .returning(
            VariableSetAttachment.variable_set_id,
            VariableSetAttachment.target_kind,
            VariableSetAttachment.target_id,
        )
        .execution_options(synchronize_session=False)
    )
    for set_id, target_kind, target_id in attachments.all():
        await _audit(
            "variable_set.detached",
            "variable_set",
            set_id,
            {"target_kind": target_kind.value, "target_id": str(target_id)},
        )
    hooks = await session.execute(
        delete(Hook)
        .where(_targeting(Hook))
        .returning(Hook.id, Hook.name, Hook.target_kind, Hook.target_id)
        .execution_options(synchronize_session=False)
    )
    for hook_id, name, target_kind, target_id in hooks.all():
        await _audit(
            "hook.deleted",
            "hook",
            hook_id,
            {"name": name, "on": target_kind.value, "target_id": str(target_id)},
        )
    notification_targets = await session.execute(
        delete(NotificationTarget)
        .where(_targeting(NotificationTarget))
        .returning(
            NotificationTarget.id,
            NotificationTarget.name,
            NotificationTarget.target_kind,
            NotificationTarget.target_id,
        )
        .execution_options(synchronize_session=False)
    )
    for nt_id, name, target_kind, target_id in notification_targets.all():
        await _audit(
            "notification_target.deleted",
            "notification_target",
            nt_id,
            {"name": name, "on": target_kind.value, "target_id": str(target_id)},
        )


@router.get("/stacks/{stack_id}/environments", response_model=list[EnvironmentOut])
async def list_environments(
    stack_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[EnvironmentOut]:
    await guard_stack_id(session, user, stack_id)
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
    return await _envs_out(session, rows)


@router.post(
    "/stacks/{stack_id}/environments",
    response_model=EnvironmentOut,
    status_code=201,
    dependencies=[Writer],
)
async def create_environment(
    stack_id: uuid.UUID, body: EnvironmentCreate, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    await guard_stack_id(session, user, stack_id, min_role=Role.writer)
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
    return await _env_out(session, env)


@router.get("/environments/{env_id}", response_model=EnvironmentOut)
async def get_environment(
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    return await _env_out(session, await guard_env_id(session, user, env_id))


@router.patch("/environments/{env_id}", response_model=EnvironmentOut, dependencies=[Writer])
async def update_environment(
    env_id: uuid.UUID, body: EnvironmentUpdate, user: CurrentUser, session: DbSession
) -> EnvironmentOut:
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    changes = body.model_dump(exclude_unset=True)
    if "tier" in changes:
        await _require_tier(session, changes["tier"])
    if "branch" in changes and changes["branch"] != env.branch:
        # Staleness (§9.6): the recorded head is the old branch's, so `stale` must not compare
        # against it. No head_updated_at makes the env due at the next head poll.
        env.head_sha = None
        env.head_updated_at = None
        env.commits_ahead = None
        env.affects_project_root = None
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
    return await _env_out(session, env)


@router.delete("/environments/{env_id}", status_code=204, dependencies=[Writer])
async def delete_environment(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    await lock_envs_for_deletion(session, Environment.id == env.id)
    await drop_target_references(session, user, env_ids=[env.id])
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
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    secret = decrypt(stack.repo_secret_encrypted) if stack.repo_secret_encrypted else None
    branch = env.branch
    # No DB transaction is held across the git call (§9.6): end the read one first (the session
    # doesn't expire on commit, so env and stack stay loaded).
    await session.commit()
    sha = await ls_remote_sha(stack.repo_url, stack.repo_auth_kind, secret, branch)
    if sha is None:
        raise ProblemException(422, "Head refresh failed", "Could not read the branch head.")
    # Only while the env still tracks that branch: one re-pointed meanwhile isn't tracking it.
    await session.execute(
        update(Environment)
        .where(Environment.id == env.id, Environment.branch == branch)
        .values(head_sha=sha, head_updated_at=datetime.now(UTC))
    )
    await session.commit()
    await session.refresh(env)
    return await _env_out(session, env)


# --- environment variables (override slot: stack_id + environment_id both set) ---


@router.get("/environments/{env_id}/variables", response_model=list[VariableOut])
async def list_env_variables(
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[VariableOut]:
    await guard_env_id(session, user, env_id)
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
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
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
    await guard_env_id(session, user, env_id, min_role=Role.writer)
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
    await guard_env_id(session, user, env_id, min_role=Role.writer)
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
    env = await guard_env_id(session, user, env_id)
    resolved = await resolve_variables(session, env)
    return [ResolvedVariableOut.of(rv) for rv in resolved]


@router.post(
    "/environments/{env_id}/discover-inputs",
    dependencies=[Writer, Depends(rate_limit("discover", per_minute=10, burst=5))],
)
async def discover_inputs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Introspect the repo (shallow clone + HCL parse, no terraform run) and create the REQUIRED
    root-module inputs that aren't already resolved, as env variables with empty placeholders."""
    env = await guard_env_id(session, user, env_id, min_role=Role.writer)
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    settings = get_settings()
    secret = decrypt(stack.repo_secret_encrypted) if stack.repo_secret_encrypted else None
    dest = await clone_shallow(stack.repo_url, stack.repo_auth_kind, secret, env.branch)
    try:
        # The full-clone walk + HCL parse are synchronous filesystem work — off the event loop.
        required = await asyncio.to_thread(
            _scan_clone,
            dest,
            stack.project_root,
            max_mb=settings.stackd_discovery_max_repo_mb,
            max_tf_files=settings.stackd_discovery_max_tf_files,
        )
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
        # Removing the whole clone is blocking filesystem work too — never on the event loop.
        await asyncio.to_thread(shutil.rmtree, dest, ignore_errors=True)
