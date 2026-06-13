from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import TriggerPolicy


class EnvDependency(Base):
    """A directed edge between two environments (SPECS §3.8). Same-stack edges are valid."""

    __tablename__ = "env_dependencies"
    __table_args__ = (
        UniqueConstraint("upstream_env_id", "downstream_env_id", name="uq_env_dependencies_edge"),
        CheckConstraint("upstream_env_id <> downstream_env_id", name="ck_env_dependencies_no_self"),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    upstream_env_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE")
    )
    downstream_env_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE")
    )
    trigger_policy: Mapped[TriggerPolicy] = mapped_column(
        Enum(TriggerPolicy, name="trigger_policy"), default=TriggerPolicy.on_output_change
    )


class OutputReference(Base):
    """Maps an upstream output → a downstream input, with an optional mock for bootstrap (§9.3)."""

    __tablename__ = "output_references"
    __table_args__ = (
        UniqueConstraint("dependency_id", "input_name", name="uq_output_references_input"),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    dependency_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("env_dependencies.id", ondelete="CASCADE")
    )
    output_name: Mapped[str] = mapped_column(String)
    input_name: Mapped[str] = mapped_column(String)  # downstream var, without TF_VAR_
    mock_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )


class EnvOutput(Base):
    """Captured outputs after apply (SPECS §9.1). Sensitive: value NULL, never propagated."""

    __tablename__ = "env_outputs"
    __table_args__ = (UniqueConstraint("environment_id", "name", name="uq_env_outputs_name"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    name: Mapped[str] = mapped_column(String)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    value_hash: Mapped[str | None] = mapped_column(String, default=None)
    sensitive: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = created_at_col()
