from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import WorkerStatus


class WorkerPool(Base):
    __tablename__ = "worker_pools"

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String, unique=True)
    labels: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), default=None)
    token_hash: Mapped[str] = mapped_column(String)  # SHA-256 of the pool registration token
    created_at: Mapped[datetime] = created_at_col()


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = pk_uuid()
    pool_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("worker_pools.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String)
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus, name="worker_status"), default=WorkerStatus.idle
    )
    labels: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), default=None)
    version: Mapped[str | None] = mapped_column(String, default=None)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)
    registered_at: Mapped[datetime] = created_at_col()
