from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class RunComment(Base):
    """A comment on a run's plan (SPECS §16). Optionally anchored to a part of the plan via a
    polymorphic `anchor` (a log-line range or, later, a resource address). Comments are a side
    record — they never change run state."""

    __tablename__ = "run_comments"

    id: Mapped[uuid.UUID] = pk_uuid()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("run_comments.id", ondelete="CASCADE"), default=None
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    author_email: Mapped[str | None] = mapped_column(String, default=None)
    body: Mapped[str] = mapped_column(Text)
    anchor: Mapped[dict | None] = mapped_column(JSONB, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None
    )
    created_at: Mapped[datetime] = created_at_col()
    edited_at: Mapped[datetime | None] = mapped_column(default=None)
