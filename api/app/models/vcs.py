from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class VcsOutbox(Base):
    """Transactional outbox for VCS post-back (Phase A / §18). Enqueued in the SAME txn as the run
    transition (no external I/O there); the scheduler drains it after commit and posts the commit
    status + upserts the PR comment — at-least-once, deduped across replicas via SKIP LOCKED."""

    __tablename__ = "vcs_outbox"

    id: Mapped[uuid.UUID] = pk_uuid()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    to_state: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at_col()
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
