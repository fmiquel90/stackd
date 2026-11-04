from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import RolloutMemberStatus, RolloutState


class EnvRollout(Base):
    """An environment rollout (SPECS §21.3): an ordered, approved plan → apply of a set of
    environments along their dependency edges. Its state changes only via `advance_rollout()`."""

    __tablename__ = "env_rollouts"

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String)
    state: Mapped[RolloutState] = mapped_column(
        Enum(RolloutState, name="rollout_state"), default=RolloutState.awaiting_approval
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    approval_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    pause_reason: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class EnvRolloutMember(Base):
    __tablename__ = "env_rollout_members"

    rollout_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("env_rollouts.id", ondelete="CASCADE"), primary_key=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE"), primary_key=True
    )
    level: Mapped[int] = mapped_column(Integer)
    status: Mapped[RolloutMemberStatus] = mapped_column(
        Enum(RolloutMemberStatus, name="rollout_member_status"),
        default=RolloutMemberStatus.pending,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), default=None
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # What the approval saw (§21.5): the env's tier, `protected`, and whether four-eyes applied to
    # it. The gate refuses an auto-confirmation once one of them changed, until a re-approval.
    approved_tier: Mapped[str | None] = mapped_column(String, default=None)
    approved_protected: Mapped[bool | None] = mapped_column(Boolean, default=None)
    approved_four_eyes: Mapped[bool | None] = mapped_column(Boolean, default=None)
