from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import Role
from app.models.audit import AuditEvent

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


def _filtered(
    *,
    actor: uuid.UUID | None,
    action: str | None,
    target_kind: str | None,
    target_id: uuid.UUID | None,
    from_: datetime | None,
    to: datetime | None,
):  # type: ignore[no-untyped-def]
    stmt = select(AuditEvent)
    if actor:
        stmt = stmt.where(AuditEvent.actor_id == actor)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if target_kind:
        stmt = stmt.where(AuditEvent.target_kind == target_kind)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if from_:
        stmt = stmt.where(AuditEvent.created_at >= from_)
    if to:
        stmt = stmt.where(AuditEvent.created_at <= to)
    return stmt.order_by(AuditEvent.created_at.desc())


@router.get("")
async def list_audit(
    _: CurrentUser,
    session: DbSession,
    actor: uuid.UUID | None = None,
    action: str | None = None,
    target_kind: str | None = None,
    target_id: uuid.UUID | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    stmt = _filtered(
        actor=actor, action=action, target_kind=target_kind, target_id=target_id, from_=from_, to=to
    ).limit(min(limit, 1000))
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(e.id),
            "actor_kind": e.actor_kind.value,
            "actor_email": e.actor_email,
            "action": e.action,
            "target_kind": e.target_kind,
            "target_id": str(e.target_id) if e.target_id else None,
            "context": e.context,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


@router.get("/export", dependencies=[Depends(require_role(Role.admin))])
async def export_csv(
    session: DbSession,
    actor: uuid.UUID | None = None,
    action: str | None = None,
    target_kind: str | None = None,
    target_id: uuid.UUID | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> StreamingResponse:
    rows = (
        (
            await session.execute(
                _filtered(
                    actor=actor,
                    action=action,
                    target_kind=target_kind,
                    target_id=target_id,
                    from_=from_,
                    to=to,
                )
            )
        )
        .scalars()
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "actor_email", "action", "target_kind", "target_id"])
    for e in rows:
        writer.writerow(
            [
                e.created_at.isoformat(),
                e.actor_email or "",
                e.action,
                e.target_kind or "",
                str(e.target_id or ""),
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit.csv"},
    )
