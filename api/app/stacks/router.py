from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.crypto import encrypt
from app.db import get_session
from app.enums import AuditActorKind, RepoAuthKind, Role
from app.errors import ProblemException
from app.models.stack import Stack
from app.spaces import get_default_space
from app.stacks.git import check_repo
from app.stacks.schemas import CheckRepoResult, StackCreate, StackOut, StackUpdate
from app.variables.crud import (
    create_variable,
    get_variable,
    update_variable,
    variables_for,
)
from app.variables.schemas import VariableCreate, VariableOut, VariableUpdate

router = APIRouter(prefix="/api/v1/stacks", tags=["stacks"])
Writer = Depends(require_role(Role.writer))
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _get_stack(session: AsyncSession, stack_id: uuid.UUID) -> Stack:
    stack = await session.get(Stack, stack_id)
    if stack is None:
        raise ProblemException(404, "Stack not found", None)
    return stack


def _client_ctx(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


@router.get("", response_model=list[StackOut])
async def list_stacks(_: CurrentUser, session: DbSession) -> list[StackOut]:
    rows = (await session.execute(select(Stack).order_by(Stack.name))).scalars().all()
    return [StackOut.of(s) for s in rows]


@router.post("", response_model=StackOut, status_code=201, dependencies=[Writer])
async def create_stack(
    body: StackCreate, user: CurrentUser, session: DbSession, request: Request
) -> StackOut:
    space = await get_default_space(session)
    stack = Stack(
        space_id=space.id,
        name=body.name,
        description=body.description,
        repo_url=body.repo_url,
        repo_auth_kind=body.repo_auth_kind,
        project_root=body.project_root,
        tool=body.tool,
        tool_version=body.tool_version,
        labels=body.labels,
    )
    if body.repo_auth_kind != RepoAuthKind.none and body.repo_secret:
        stack.repo_secret_encrypted = encrypt(body.repo_secret)
    session.add(stack)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Stack already exists", f"'{body.name}' exists in this space."
        ) from exc
    await record_audit(
        session,
        action="stack.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="stack",
        target_id=stack.id,
        context={"name": stack.name, "repo_url": stack.repo_url, **_client_ctx(request)},
    )
    await session.commit()
    await session.refresh(stack)
    return StackOut.of(stack)


@router.get("/{stack_id}", response_model=StackOut)
async def get_stack(stack_id: uuid.UUID, _: CurrentUser, session: DbSession) -> StackOut:
    return StackOut.of(await _get_stack(session, stack_id))


@router.patch("/{stack_id}", response_model=StackOut, dependencies=[Writer])
async def update_stack(
    stack_id: uuid.UUID, body: StackUpdate, user: CurrentUser, session: DbSession
) -> StackOut:
    stack = await _get_stack(session, stack_id)
    changes = body.model_dump(exclude_unset=True)
    secret = changes.pop("repo_secret", None)
    webhook_secret = changes.pop("webhook_secret", None)
    for field, value in changes.items():
        setattr(stack, field, value)
    if secret is not None:
        stack.repo_secret_encrypted = encrypt(secret) if secret else None
    if webhook_secret is not None:
        stack.webhook_secret_encrypted = encrypt(webhook_secret) if webhook_secret else None
    await record_audit(
        session,
        action="stack.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="stack",
        target_id=stack.id,
        context={"fields": sorted(changes) + (["repo_secret"] if secret is not None else [])},
    )
    await session.commit()
    await session.refresh(stack)
    return StackOut.of(stack)


@router.delete("/{stack_id}", status_code=204, dependencies=[Writer])
async def delete_stack(stack_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    stack = await _get_stack(session, stack_id)
    name = stack.name
    await session.delete(stack)
    await record_audit(
        session,
        action="stack.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="stack",
        target_id=stack_id,
        context={"name": name},
    )
    await session.commit()


@router.post("/{stack_id}/check-repo", response_model=CheckRepoResult, dependencies=[Writer])
async def check_stack_repo(
    stack_id: uuid.UUID, _: CurrentUser, session: DbSession
) -> CheckRepoResult:
    from app.crypto import decrypt

    stack = await _get_stack(session, stack_id)
    secret = decrypt(stack.repo_secret_encrypted) if stack.repo_secret_encrypted else None
    ok, branches, detail = await check_repo(stack.repo_url, stack.repo_auth_kind, secret)
    return CheckRepoResult(ok=ok, branches=branches, detail=detail)


# --- stack-level variables (environment_id NULL) ---


@router.get("/{stack_id}/variables", response_model=list[VariableOut])
async def list_stack_variables(
    stack_id: uuid.UUID, _: CurrentUser, session: DbSession
) -> list[VariableOut]:
    await _get_stack(session, stack_id)
    return [VariableOut.of(v) for v in await variables_for(session, stack_id=stack_id)]


@router.post(
    "/{stack_id}/variables", response_model=VariableOut, status_code=201, dependencies=[Writer]
)
async def create_stack_variable(
    stack_id: uuid.UUID, body: VariableCreate, user: CurrentUser, session: DbSession
) -> VariableOut:
    await _get_stack(session, stack_id)
    var = await create_variable(session, body, stack_id=stack_id)
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
            "scope": "stack",
        },
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.patch("/{stack_id}/variables/{var_id}", response_model=VariableOut, dependencies=[Writer])
async def update_stack_variable(
    stack_id: uuid.UUID,
    var_id: uuid.UUID,
    body: VariableUpdate,
    user: CurrentUser,
    session: DbSession,
) -> VariableOut:
    var = await get_variable(session, var_id)
    if var.stack_id != stack_id or var.environment_id is not None:
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
        context={"name": var.name, "kind": var.kind.value, "sensitive": var.sensitive},
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.delete("/{stack_id}/variables/{var_id}", status_code=204, dependencies=[Writer])
async def delete_stack_variable(
    stack_id: uuid.UUID, var_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    var = await get_variable(session, var_id)
    if var.stack_id != stack_id or var.environment_id is not None:
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
        context={"name": name, "scope": "stack"},
    )
    await session.commit()
