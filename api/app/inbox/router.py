from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.user_notification import UserNotification

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications-inbox"])
DbSession = Annotated[AsyncSession, Depends(get_session)]

# How many recent notifications the bell loads (older ones drop off the feed).
_FEED_LIMIT = 50


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    run_id: uuid.UUID | None
    comment_id: uuid.UUID | None
    context: dict | None
    read: bool
    created_at: datetime

    @classmethod
    def of(cls, n: UserNotification) -> NotificationOut:
        return cls(
            id=n.id,
            kind=n.kind,
            run_id=n.run_id,
            comment_id=n.comment_id,
            context=n.context,
            read=n.read_at is not None,
            created_at=n.created_at,
        )


class MarkRead(BaseModel):
    ids: list[uuid.UUID] | None = None  # None → mark all of the user's unread as read


@router.get("", response_model=list[NotificationOut])
async def list_notifications(user: CurrentUser, session: DbSession) -> list[NotificationOut]:
    rows = (
        (
            await session.execute(
                select(UserNotification)
                .where(UserNotification.user_id == user.id)
                .order_by(UserNotification.created_at.desc())
                .limit(_FEED_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return [NotificationOut.of(n) for n in rows]


@router.post("/read", status_code=204)
async def mark_read(body: MarkRead, user: CurrentUser, session: DbSession) -> None:
    stmt = (
        update(UserNotification)
        .where(UserNotification.user_id == user.id, UserNotification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    if body.ids:
        stmt = stmt.where(UserNotification.id.in_(body.ids))
    await session.execute(stmt)
    await session.commit()
