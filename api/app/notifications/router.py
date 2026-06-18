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
from app.models.environment import Environment
from app.models.notification import NotificationTarget
from app.models.stack import Stack
from app.models.user import User
from app.notifications.dispatcher import deliver
from app.notifications.schemas import NotificationCreate, NotificationOut, NotificationUpdate
from app.spaces import guard_env, guard_stack

# Outbound notification targets: where run events (awaiting-confirmation / finished / failed) are
# delivered. Scoped to a stack or an environment, mirroring platform hooks.
router = APIRouter(prefix="/api/v1", tags=["notifications"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Writer = Depends(require_role(Role.writer))


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


async def _list(
    session: AsyncSession, target_kind: AttachmentTarget, target_id: uuid.UUID
) -> list[NotificationTarget]:
    rows = (
        (
            await session.execute(
                select(NotificationTarget)
                .where(
                    NotificationTarget.target_kind == target_kind,
                    NotificationTarget.target_id == target_id,
                )
                .order_by(NotificationTarget.created_at)
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
    body: NotificationCreate,
) -> NotificationTarget:
    target = NotificationTarget(
        target_kind=target_kind,
        target_id=target_id,
        name=body.name,
        kind=body.kind,
        url=body.url,
        on_states=body.on_states,
        enabled=body.enabled,
    )
    session.add(target)
    await session.flush()
    await record_audit(
        session,
        action="notification_target.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="notification_target",
        target_id=target.id,
        context={"on": target_kind.value, "kind": target.kind.value, "name": target.name},
    )
    await session.commit()
    await session.refresh(target)
    return target


async def _get_owned(
    session: AsyncSession,
    target_id_pk: uuid.UUID,
    target_kind: AttachmentTarget,
    target_id: uuid.UUID,
) -> NotificationTarget:
    t = await session.get(NotificationTarget, target_id_pk)
    if t is None or t.target_kind != target_kind or t.target_id != target_id:
        raise ProblemException(404, "Notification target not found", None)
    return t


async def _update(
    session: AsyncSession, user: CurrentUser, t: NotificationTarget, body: NotificationUpdate
) -> NotificationTarget:
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(t, field, value)
    await record_audit(
        session,
        action="notification_target.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="notification_target",
        target_id=t.id,
    )
    await session.commit()
    await session.refresh(t)
    return t


async def _delete(session: AsyncSession, user: CurrentUser, t: NotificationTarget) -> None:
    tid = t.id
    await session.delete(t)
    await record_audit(
        session,
        action="notification_target.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="notification_target",
        target_id=tid,
    )
    await session.commit()


def _test_payload(t: NotificationTarget) -> dict:
    """A delivery clearly flagged as a test, so a real run is never mistaken for one."""
    if t.kind.value == "slack":
        return {"text": "🧪 *Stackd test notification* — your Slack webhook is configured (test)."}
    return {
        "event": "notification.test",
        "test": True,
        "message": "Stackd test notification — your webhook is configured.",
    }


async def _send_test(t: NotificationTarget) -> dict:
    try:
        await deliver(t, _test_payload(t))
    except Exception as exc:  # external endpoint failure → surface as a 502 with the reason
        raise ProblemException(502, "Test delivery failed", str(exc)) from exc
    return {"ok": True}


# --- stack-level ---


@router.get("/stacks/{stack_id}/notifications", response_model=list[NotificationOut])
async def list_stack_notifications(
    stack_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[NotificationOut]:
    await _guard_stack_id(session, user, stack_id)
    return [NotificationOut.of(t) for t in await _list(session, AttachmentTarget.stack, stack_id)]


@router.post(
    "/stacks/{stack_id}/notifications",
    response_model=NotificationOut,
    status_code=201,
    dependencies=[Writer],
)
async def create_stack_notification(
    stack_id: uuid.UUID, body: NotificationCreate, user: CurrentUser, session: DbSession
) -> NotificationOut:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    return NotificationOut.of(await _create(session, user, AttachmentTarget.stack, stack_id, body))


@router.patch(
    "/stacks/{stack_id}/notifications/{target_id}",
    response_model=NotificationOut,
    dependencies=[Writer],
)
async def update_stack_notification(
    stack_id: uuid.UUID,
    target_id: uuid.UUID,
    body: NotificationUpdate,
    user: CurrentUser,
    session: DbSession,
) -> NotificationOut:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    t = await _get_owned(session, target_id, AttachmentTarget.stack, stack_id)
    return NotificationOut.of(await _update(session, user, t, body))


@router.delete(
    "/stacks/{stack_id}/notifications/{target_id}", status_code=204, dependencies=[Writer]
)
async def delete_stack_notification(
    stack_id: uuid.UUID, target_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    await _delete(
        session, user, await _get_owned(session, target_id, AttachmentTarget.stack, stack_id)
    )


@router.post("/stacks/{stack_id}/notifications/{target_id}/test", dependencies=[Writer])
async def test_stack_notification(
    stack_id: uuid.UUID, target_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict:
    await _guard_stack_id(session, user, stack_id, min_role=Role.writer)
    return await _send_test(await _get_owned(session, target_id, AttachmentTarget.stack, stack_id))


# --- environment-level ---


@router.get("/environments/{env_id}/notifications", response_model=list[NotificationOut])
async def list_env_notifications(
    env_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[NotificationOut]:
    await _guard_env_id(session, user, env_id)
    return [
        NotificationOut.of(t) for t in await _list(session, AttachmentTarget.environment, env_id)
    ]


@router.post(
    "/environments/{env_id}/notifications",
    response_model=NotificationOut,
    status_code=201,
    dependencies=[Writer],
)
async def create_env_notification(
    env_id: uuid.UUID, body: NotificationCreate, user: CurrentUser, session: DbSession
) -> NotificationOut:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    return NotificationOut.of(
        await _create(session, user, AttachmentTarget.environment, env_id, body)
    )


@router.patch(
    "/environments/{env_id}/notifications/{target_id}",
    response_model=NotificationOut,
    dependencies=[Writer],
)
async def update_env_notification(
    env_id: uuid.UUID,
    target_id: uuid.UUID,
    body: NotificationUpdate,
    user: CurrentUser,
    session: DbSession,
) -> NotificationOut:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    t = await _get_owned(session, target_id, AttachmentTarget.environment, env_id)
    return NotificationOut.of(await _update(session, user, t, body))


@router.delete(
    "/environments/{env_id}/notifications/{target_id}", status_code=204, dependencies=[Writer]
)
async def delete_env_notification(
    env_id: uuid.UUID, target_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    await _delete(
        session,
        user,
        await _get_owned(session, target_id, AttachmentTarget.environment, env_id),
    )


@router.post("/environments/{env_id}/notifications/{target_id}/test", dependencies=[Writer])
async def test_env_notification(
    env_id: uuid.UUID, target_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict:
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    return await _send_test(
        await _get_owned(session, target_id, AttachmentTarget.environment, env_id)
    )
