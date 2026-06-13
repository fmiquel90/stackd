from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import AttachmentTarget


class VariableSet(Base):
    """Factored configuration, reusable across stacks/envs (SPECS §3.4)."""

    __tablename__ = "variable_sets"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_variable_sets_space_id_name"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, default=None)
    auto_attach: Mapped[bool] = mapped_column(Boolean, default=False)  # → all stacks of the space
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class VariableSetAttachment(Base):
    __tablename__ = "variable_set_attachments"
    __table_args__ = (
        UniqueConstraint(
            "variable_set_id",
            "target_kind",
            "target_id",
            name="uq_variable_set_attachments_target",
        ),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    variable_set_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("variable_sets.id", ondelete="CASCADE")
    )
    target_kind: Mapped[AttachmentTarget] = mapped_column(
        Enum(AttachmentTarget, name="attachment_target")
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True)
    )  # stack → all envs; env → that env
    priority: Mapped[int] = mapped_column(Integer, default=0)  # orders sets at resolution
