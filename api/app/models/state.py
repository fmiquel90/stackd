from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class StateVersion(Base):
    """A managed tfstate version stored in S3 (SPECS §3.9 / §11)."""

    __tablename__ = "state_versions"

    id: Mapped[uuid.UUID] = pk_uuid()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE")
    )
    serial: Mapped[int] = mapped_column(Integer)
    lineage: Mapped[str | None] = mapped_column(String, default=None)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    s3_key: Mapped[str] = mapped_column(String)
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)  # soft-delete (§11.2)
    created_at: Mapped[datetime] = created_at_col()


class StateLock(Base):
    """One advisory lock row per environment (SPECS §3.9). Visible in UI; force-unlock audited."""

    __tablename__ = "state_locks"

    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE"), primary_key=True
    )
    lock_id: Mapped[str] = mapped_column(String)
    info: Mapped[dict | None] = mapped_column(JSONB, default=None)
    locked_at: Mapped[datetime] = created_at_col()
