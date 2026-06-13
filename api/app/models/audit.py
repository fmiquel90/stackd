from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import AuditActorKind


class AuditEvent(Base):
    """Append-only audit trail (SPECS §6.1). DB-level immutability enforced in the migration."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        Enum(AuditActorKind, name="audit_actor_kind")
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    actor_email: Mapped[str | None] = mapped_column(String, default=None)  # denormalized
    action: Mapped[str] = mapped_column(String)  # taxonomy §6.2
    target_kind: Mapped[str | None] = mapped_column(String, default=None)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    context: Mapped[dict | None] = mapped_column(JSONB, default=None)
    ip: Mapped[str | None] = mapped_column(String, default=None)
    user_agent: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = created_at_col()
