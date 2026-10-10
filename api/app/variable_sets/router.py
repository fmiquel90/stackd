from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AttachmentTarget, AuditActorKind, Role
from app.environments.router import lock_attach_target
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.spaces import (
    accessible_space_ids,
    guard_env,
    guard_stack,
    require_space_access,
    resolve_target_space,
)
from app.variable_sets.schemas import (
    AttachmentCreate,
    AttachmentOut,
    VariableSetCreate,
    VariableSetOut,
    VariableSetUpdate,
)
from app.variables.crud import create_variable, get_variable, update_variable, variables_for
from app.variables.schemas import VariableCreate, VariableOut, VariableUpdate

router = APIRouter(prefix="/api/v1/variable-sets", tags=["variable-sets"])
Writer = Depends(require_role(Role.writer))
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def _get_set(
    session: AsyncSession, user: User, set_id: uuid.UUID, *, min_role: Role = Role.reader
) -> VariableSet:
    vset = await session.get(VariableSet, set_id)
    if vset is None:
        raise ProblemException(404, "Variable set not found", None)
    await require_space_access(session, user, vset.space_id, min_role=min_role)
    return vset


@router.get("", response_model=list[VariableSetOut])
async def list_sets(user: CurrentUser, session: DbSession) -> list[VariableSet]:
    q = select(VariableSet).order_by(VariableSet.name)
    ids = await accessible_space_ids(session, user)
    if ids is not None:
        q = q.where(VariableSet.space_id.in_(ids))
    return list((await session.execute(q)).scalars().all())


@router.post("", response_model=VariableSetOut, status_code=201, dependencies=[Writer])
async def create_set(body: VariableSetCreate, user: CurrentUser, session: DbSession) -> VariableSet:
    payload = body.model_dump()
    space_id = await resolve_target_space(
        session, user, payload.pop("space_id", None), min_role=Role.writer
    )
    vset = VariableSet(space_id=space_id, **payload)
    session.add(vset)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Variable set already exists", f"'{body.name}' exists."
        ) from exc
    await record_audit(
        session,
        action="variable_set.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable_set",
        target_id=vset.id,
        context={"name": vset.name, "auto_attach": vset.auto_attach},
    )
    await session.commit()
    await session.refresh(vset)
    return vset


@router.get("/{set_id}", response_model=VariableSetOut)
async def get_set(set_id: uuid.UUID, user: CurrentUser, session: DbSession) -> VariableSet:
    return await _get_set(session, user, set_id)


@router.patch("/{set_id}", response_model=VariableSetOut, dependencies=[Writer])
async def update_set(
    set_id: uuid.UUID, body: VariableSetUpdate, user: CurrentUser, session: DbSession
) -> VariableSet:
    vset = await _get_set(session, user, set_id, min_role=Role.writer)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(vset, field, value)
    await record_audit(
        session,
        action="variable_set.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable_set",
        target_id=vset.id,
        context={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(vset)
    return vset


@router.delete("/{set_id}", status_code=204, dependencies=[Writer])
async def delete_set(set_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    vset = await _get_set(session, user, set_id, min_role=Role.writer)
    attachments = (
        (
            await session.execute(
                select(VariableSetAttachment).where(VariableSetAttachment.variable_set_id == set_id)
            )
        )
        .scalars()
        .all()
    )
    if attachments:
        # Explicit detachment required first (SPECS §3.4) — surface where it is used.
        raise ProblemException(
            409,
            "Variable set is attached",
            "Detach it everywhere before deleting.",
            type_="about:blank",
            attachments=[
                {"target_kind": a.target_kind.value, "target_id": str(a.target_id)}
                for a in attachments
            ],
        )
    name = vset.name
    await session.delete(vset)
    await record_audit(
        session,
        action="variable_set.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable_set",
        target_id=set_id,
        context={"name": name},
    )
    await session.commit()


# --- set variables ---


@router.get("/{set_id}/variables", response_model=list[VariableOut])
async def list_set_variables(
    set_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[VariableOut]:
    await _get_set(session, user, set_id)
    return [VariableOut.of(v) for v in await variables_for(session, variable_set_id=set_id)]


@router.post(
    "/{set_id}/variables", response_model=VariableOut, status_code=201, dependencies=[Writer]
)
async def create_set_variable(
    set_id: uuid.UUID, body: VariableCreate, user: CurrentUser, session: DbSession
) -> VariableOut:
    await _get_set(session, user, set_id, min_role=Role.writer)
    var = await create_variable(session, body, variable_set_id=set_id)
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
            "scope": "set",
        },
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.patch("/{set_id}/variables/{var_id}", response_model=VariableOut, dependencies=[Writer])
async def update_set_variable(
    set_id: uuid.UUID,
    var_id: uuid.UUID,
    body: VariableUpdate,
    user: CurrentUser,
    session: DbSession,
) -> VariableOut:
    await _get_set(session, user, set_id, min_role=Role.writer)
    var = await get_variable(session, var_id)
    if var.variable_set_id != set_id:
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
        context={"name": var.name, "scope": "set"},
    )
    await session.commit()
    await session.refresh(var)
    return VariableOut.of(var)


@router.delete("/{set_id}/variables/{var_id}", status_code=204, dependencies=[Writer])
async def delete_set_variable(
    set_id: uuid.UUID, var_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _get_set(session, user, set_id, min_role=Role.writer)
    var = await get_variable(session, var_id)
    if var.variable_set_id != set_id:
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
        context={"name": name, "scope": "set"},
    )
    await session.commit()


# --- attachments ---


@router.get("/{set_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    set_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[VariableSetAttachment]:
    await _get_set(session, user, set_id)
    return list(
        (
            await session.execute(
                select(VariableSetAttachment).where(VariableSetAttachment.variable_set_id == set_id)
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/{set_id}/attachments", response_model=AttachmentOut, status_code=201, dependencies=[Writer]
)
async def attach(
    set_id: uuid.UUID, body: AttachmentCreate, user: CurrentUser, session: DbSession
) -> VariableSetAttachment:
    vset = await _get_set(session, user, set_id, min_role=Role.writer)
    target_model = {
        AttachmentTarget.stack: Stack,
        AttachmentTarget.environment: Environment,
        AttachmentTarget.tier: Tier,
    }[body.target_kind]
    # No orphan on a racing deletion of the stack/env (§3.4).
    await lock_attach_target(
        session, body.target_kind, body.target_id, not_found="Attachment target not found"
    )
    target = await session.get(target_model, body.target_id)
    if target is None:
        raise ProblemException(404, "Attachment target not found", f"No {body.target_kind.value}.")
    # A set only reaches its own space (§3.4, §6 Phase F): a stack/env target must be writable by
    # the caller (both ends guarded, like create_dependency) AND live in the set's space — else a
    # writer in space A could inject A's variables into space B's runs. A tier is a global catalog
    # row with no space: the attachment is allowed, and resolution scopes it to the set's space.
    target_space_id = vset.space_id
    if isinstance(target, Stack):
        await guard_stack(session, user, target, min_role=Role.writer)
        target_space_id = target.space_id
    elif isinstance(target, Environment):
        await guard_env(session, user, target, min_role=Role.writer)
        stack = await session.get(Stack, target.stack_id)
        assert stack is not None
        target_space_id = stack.space_id
    if target_space_id != vset.space_id:
        raise ProblemException(
            400, "Cross-space attachment", "The target must belong to the variable set's space."
        )
    attachment = VariableSetAttachment(
        variable_set_id=set_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        priority=body.priority,
    )
    session.add(attachment)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409, "Already attached", "This set is already attached here."
        ) from exc
    await record_audit(
        session,
        action="variable_set.attached",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable_set",
        target_id=set_id,
        context={
            "target_kind": body.target_kind.value,
            "target_id": str(body.target_id),
            "priority": body.priority,
        },
    )
    await session.commit()
    await session.refresh(attachment)
    return attachment


@router.delete("/{set_id}/attachments/{attachment_id}", status_code=204, dependencies=[Writer])
async def detach(
    set_id: uuid.UUID, attachment_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _get_set(session, user, set_id, min_role=Role.writer)
    attachment = await session.get(VariableSetAttachment, attachment_id)
    if attachment is None or attachment.variable_set_id != set_id:
        raise ProblemException(404, "Attachment not found", None)
    ctx = {"target_kind": attachment.target_kind.value, "target_id": str(attachment.target_id)}
    await session.delete(attachment)
    await record_audit(
        session,
        action="variable_set.detached",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="variable_set",
        target_id=set_id,
        context=ctx,
    )
    await session.commit()
