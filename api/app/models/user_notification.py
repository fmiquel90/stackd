from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class UserNotification(Base):
    """An in-app notification addressed to one user (SPECS §17) — the inbound counterpart to the
    outbound Slack/webhook outbox. A side record: created in the same txn as its trigger, never
    changes run state. `kind` ∈ approval_request | run_finished | run_failed | comment_reply |
    mention."""

    __tablename__ = "user_notifications"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), default=None
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("run_comments.id", ondelete="CASCADE"), default=None
    )
    context: Mapped[dict | None] = mapped_column(JSONB, default=None)  # e.g. stack/env names
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = created_at_col()
