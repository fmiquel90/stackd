from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col


class Space(Base):
    """Root container (SPECS §3.0). MVP: a single `default` space, RBAC deferred to Phase 7."""

    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = pk_uuid()
    name: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
