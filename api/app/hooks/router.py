from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AttachmentTarget, AuditActorKind, Role
from app.errors import ProblemException
from app.hooks.schemas import HookCreate, HookOut, HookUpdate
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.stack import Stack
from app.models.user import User
from app.spaces import guard_env, guard_stack


async def _guard_stack_id(
    session: AsyncSession, user: User, stack_id: uuid.UUID, *, min_role: Role = Role.reader
) -> None:
    stack = await session.get(Stack, stack_id)
    if stack is None:
        raise ProblemException(404, "Stack not found", None)
    await guard_stack(session, user, stack, min_role=min_role)


async def _guard_env_id(
    session: AsyncSession, user: User, env_id: uuid.UUID, *, min_role: Role = Role.reader
) -> None:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env, min_role=min_role)


# Platform hooks (SPECS §8.1): UI/API-defined governance, non-bypassable by a PR. The agent merges
# them ahead of repo (.stackd.yml) hooks at claim time (see app/workers/hooks.py).
router = APIRouter(prefix="/api/v1", tags=["hooks"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Writer = Depends(require_role(Role.writer))


async def _list(
    session: AsyncSession, target_kind: AttachmentTarget, target_id: uuid.UUID
) -> list[Hook]:
    rows = (
        (
            await session.execute(
                select(Hook)
                .where(Hook.target_kind == target_kind, Hook.target_id == target_id)
                .order_by(Hook.stage, Hook.position)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _create(
    session: AsyncSession,
    user: CurrentUser,
    target_kind: AttachmentTarget,
    target_id: uuid.UUID,
    body: HookCreate,
) -> Hook:
    hook = Hook(
        target_kind=target_kind,
        target_id=target_id,
        stage=body.stage,
        name=body.name,
        command=body.command,
        on_failure=body.on_failure,
        position=body.position,
    )
    session.add(hook)
    await session.flush()
    await record_audit(
        session,
        action="hook.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="hook",
        target_id=hook.id,
        context={"stage": hook.stage.value, "name": hook.name, "on": target_kind.value},
    )
    await session.commit()
    await session.refresh(hook)
    return hook


async def _get_owned(
    session: AsyncSession, hook_id: uuid.UUID, target_kind: AttachmentTarget, target_id: uuid.UUID
) -> Hook:
    hook = await session.get(Hook, hook_id)
    if hook is None or hook.target_kind != target_kind or hook.target_id != target_id:
        raise ProblemException(404, "Hook not found", None)
    return hook


async def _update(session: AsyncSession, user: CurrentUser, hook: Hook, body: HookUpdate) -> Hook:
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(hook, field, value)
    await record_audit(
        session,
        action="hook.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="hook",
        target_id=hook.id,
    )
    await session.commit()
    await session.refresh(hook)
    return hook


async def _delete(session: AsyncSession, user: CurrentUser, hook: Hook) -> None:
    hook_id = hook.id
    await session.delete(hook)
    await record_audit(
        session,
        action="hook.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="hook",
        target_id=hook_id,
    )
    await session.commit()


# --- stack-level hooks ---


@router.get("/stacks/{stack_id}/hooks", response_model=list[HookOut])
async def list_stack_hooks(
    stack_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[HookOut]:
    await _guard_stack_id(session, user, stack_id)
    return [HookOut.of(h) for h in await _list(session, AttachmentTarget.stack, stack_id)]


@router.post(
    "/stacks/{stack_id}/hooks", response_model=HookOut, status_code=201, dependencies=[Writer]
)
async def create_stack_hook(
    stack_id: uuid.UUID, body: HookCreate, user: CurrentUser, session: DbSession
) -> HookOut:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    return HookOut.of(await _create(session, user, AttachmentTarget.stack, stack_id, body))


@router.patch("/stacks/{stack_id}/hooks/{hook_id}", response_model=HookOut, dependencies=[Writer])
async def update_stack_hook(
    stack_id: uuid.UUID, hook_id: uuid.UUID, body: HookUpdate, user: CurrentUser, session: DbSession
) -> HookOut:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    hook = await _get_owned(session, hook_id, AttachmentTarget.stack, stack_id)
    return HookOut.of(await _update(session, user, hook, body))


@router.delete("/stacks/{stack_id}/hooks/{hook_id}", status_code=204, dependencies=[Writer])
async def delete_stack_hook(
    stack_id: uuid.UUID, hook_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    await _delete(
        session, user, await _get_owned(session, hook_id, AttachmentTarget.stack, stack_id)
    )


# --- environment-level hooks ---


@router.get("/environments/{env_id}/hooks", response_model=list[HookOut])
async def list_env_hooks(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[HookOut]:
    await _guard_env_id(session, user, env_id)
    return [HookOut.of(h) for h in await _list(session, AttachmentTarget.environment, env_id)]


@router.post(
    "/environments/{env_id}/hooks", response_model=HookOut, status_code=201, dependencies=[Writer]
)
async def create_env_hook(
    env_id: uuid.UUID, body: HookCreate, user: CurrentUser, session: DbSession
) -> HookOut:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    return HookOut.of(await _create(session, user, AttachmentTarget.environment, env_id, body))


@router.patch(
    "/environments/{env_id}/hooks/{hook_id}", response_model=HookOut, dependencies=[Writer]
)
async def update_env_hook(
    env_id: uuid.UUID, hook_id: uuid.UUID, body: HookUpdate, user: CurrentUser, session: DbSession
) -> HookOut:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    hook = await _get_owned(session, hook_id, AttachmentTarget.environment, env_id)
    return HookOut.of(await _update(session, user, hook, body))


@router.delete("/environments/{env_id}/hooks/{hook_id}", status_code=204, dependencies=[Writer])
async def delete_env_hook(
    env_id: uuid.UUID, hook_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    await _delete(
        session, user, await _get_owned(session, hook_id, AttachmentTarget.environment, env_id)
    )
