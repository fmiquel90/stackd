from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import Tier


class Environment(Base):
    """The executable instance: state + variables + protections (SPECS §3.2)."""

    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("stack_id", "name", name="uq_environments_stack_id_name"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    stack_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stacks.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String)
    tier: Mapped[Tier] = mapped_column(Enum(Tier, name="tier"))  # §2.4 apply/destroy permissions
    branch: Mapped[str] = mapped_column(String)

    autodeploy: Mapped[bool] = mapped_column(Boolean, default=False)  # forced false if protected
    protected: Mapped[bool] = mapped_column(Boolean, default=False)  # forces confirm + 4-eyes
    require_second_pair_of_eyes: Mapped[bool] = mapped_column(Boolean, default=False)
    managed_state: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_mock_apply: Mapped[bool] = mapped_column(Boolean, default=False)  # §9.3
    allow_fallback_apply: Mapped[bool] = mapped_column(Boolean, default=False)  # §15.5

    # Git staleness (§9.6) — populated from Phase 5; columns exist now.
    head_sha: Mapped[str | None] = mapped_column(String, default=None)
    head_updated_at: Mapped[datetime | None] = mapped_column(default=None)
    commits_ahead: Mapped[int | None] = mapped_column(Integer, default=None)
    affects_project_root: Mapped[bool | None] = mapped_column(Boolean, default=None)

    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    labels: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), default=None)
    position: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
