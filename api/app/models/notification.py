from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import AttachmentTarget, NotificationKind


class NotificationTarget(Base):
    """An outbound destination for run events (SPECS §5 follow-up). Scoped to a stack or an
    environment exactly like platform hooks (`target_kind`/`target_id`). A run matches the
    env-level targets of its environment plus the stack-level targets of its stack."""

    __tablename__ = "notification_targets"

    id: Mapped[uuid.UUID] = pk_uuid()
    target_kind: Mapped[AttachmentTarget] = mapped_column(
        Enum(AttachmentTarget, name="attachment_target", create_type=False)
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[NotificationKind] = mapped_column(Enum(NotificationKind, name="notification_kind"))
    url: Mapped[str] = mapped_column(String)
    # Run states that fire this target; defaults to the human-relevant ones at creation.
    on_states: Mapped[list] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()


class NotificationOutbox(Base):
    """Transactional outbox (CLAUDE §4 #2 spirit: enqueued in the SAME txn as the transition,
    no external I/O in the request path). A scheduler task drains it after commit and POSTs to
    the matching targets — at-least-once, deduped across replicas via FOR UPDATE SKIP LOCKED."""

    __tablename__ = "notification_outbox"

    id: Mapped[uuid.UUID] = pk_uuid()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    to_state: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at_col()
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
