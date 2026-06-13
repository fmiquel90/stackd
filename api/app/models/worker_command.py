from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class WorkerCommand(Base):
    """Downward command for a worker, delivered on its next heartbeat (SPECS §7.1 — no inbound).

    Generic queue; today carries `diagnostics` (a read-only debug bundle), reusable later for
    `cancel_job`. Status: pending → sent (delivered) → done | failed.
    """

    __tablename__ = "worker_commands"

    id: Mapped[uuid.UUID] = pk_uuid()
    worker_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workers.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String)  # "diagnostics" | "cancel_job" | ...
    status: Mapped[str] = mapped_column(String, default="pending")
    payload: Mapped[dict | None] = mapped_column(JSONB, default=None)
    result: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
