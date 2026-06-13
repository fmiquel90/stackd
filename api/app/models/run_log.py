from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col


class RunLog(Base):
    """Hot log storage (SPECS §3.9 / §5.2). PK (run_id, phase, seq) → idempotent chunk retries."""

    __tablename__ = "run_logs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    phase: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str | None] = mapped_column(String, default=None)  # e.g. "hook:infracost"
    lines: Mapped[list] = mapped_column(JSONB)  # [{"t": "...", "msg": "..."}]
    created_at: Mapped[datetime] = created_at_col()
