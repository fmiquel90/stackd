from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, pk_uuid
from app.enums import SecretFallback, VariableKind


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
        # Exactly one value source (§15.1): plaintext, encrypted secret, or external reference.
        CheckConstraint(
            "(value IS NOT NULL)::int + (value_encrypted IS NOT NULL)::int "
            "+ (secret_source_id IS NOT NULL)::int = 1",
            name="one_value_source",
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

    # External secret reference (§15): the value is fetched live at claim time, never stored here.
    # A referenced variable is always sensitive. `secret_fallback_encrypted` holds the static
    # fallback value (mode=static) used when the provider is unreachable.
    secret_source_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("secret_sources.id", ondelete="RESTRICT"), default=None
    )
    secret_ref: Mapped[str | None] = mapped_column(String, default=None)
    secret_fallback_mode: Mapped[SecretFallback] = mapped_column(
        Enum(SecretFallback, name="secret_fallback"), default=SecretFallback.error
    )
    secret_fallback_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
