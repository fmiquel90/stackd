from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, pk_uuid
from app.enums import VariableKind


class Variable(Base):
    """A variable at stack/env level or inside a variable set (SPECS §3.3).

    Exactly one parent: `stack_id` XOR `variable_set_id`. `environment_id` (an env override)
    is only valid alongside `stack_id` — a set variable's targeting comes from the attachment.
    The two partial unique indexes are created in the migration (COALESCE expression).
    """

    __tablename__ = "variables"
    __table_args__ = (
        CheckConstraint(
            "(variable_set_id IS NOT NULL) <> (stack_id IS NOT NULL)",
            name="one_parent",
        ),
        CheckConstraint(
            "variable_set_id IS NULL OR environment_id IS NULL",
            name="set_var_not_env_scoped",
        ),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    stack_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stacks.id", ondelete="CASCADE"), default=None
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE"), default=None
    )
    variable_set_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("variable_sets.id", ondelete="CASCADE"), default=None
    )

    kind: Mapped[VariableKind] = mapped_column(Enum(VariableKind, name="variable_kind"))
    name: Mapped[str] = mapped_column(String)
    value: Mapped[str | None] = mapped_column(String, default=None)  # plaintext (non-sensitive)
    value_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)  # AES-GCM
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    hcl: Mapped[bool] = mapped_column(Boolean, default=False)
