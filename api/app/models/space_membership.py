from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import String

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import Role


class SpaceMembership(Base):
    """Per-space RBAC grant (SPECS §2/§6, Phase F). When a membership exists for (space, user) it
    is the effective permission in that space, overriding the user's instance defaults
    (`users.role`/`allowed_tiers`/`can_destroy`). One membership per (space, user)."""

    __tablename__ = "space_memberships"
    __table_args__ = (UniqueConstraint("space_id", "user_id", name="uq_space_membership"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.reader)
    # Per-space tier ceiling (set membership, §2.4) — overrides the user's instance allowed_tiers.
    allowed_tiers: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    can_destroy: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
