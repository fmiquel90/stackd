from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AuditActorKind
from app.models.audit import AuditEvent


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_kind: AuditActorKind,
    actor_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    target_kind: str | None = None,
    target_id: uuid.UUID | None = None,
    context: dict | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent:
    """Add an audit event to the current session.

    Invariant (SPECS §6.3): the caller must commit it in the SAME transaction as the
    mutating action. This function only stages the row — it never commits on its own.
    """
    event = AuditEvent(
        action=action,
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_email=actor_email,
        target_kind=target_kind,
        target_id=target_id,
        context=context,
        ip=ip,
        user_agent=user_agent,
    )
    session.add(event)
    return event
