from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import Role, Tier


class User(Base):
    """SPECS §2.2. `role` = global capabilities; `max_apply_tier`/`can_destroy`
    gate per-env apply (§2.4)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = pk_uuid()
    google_sub: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String, default=None)
    avatar_url: Mapped[str | None] = mapped_column(String, default=None)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.reader)
    max_apply_tier: Mapped[Tier | None] = mapped_column(Enum(Tier, name="tier"), default=None)
    can_destroy: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    onboarded_at: Mapped[datetime | None] = mapped_column(default=None)  # first-login tour seen
    created_at: Mapped[datetime] = created_at_col()

    @property
    def onboarded(self) -> bool:
        return self.onboarded_at is not None


class RefreshToken(Base):
    """Rotating refresh tokens with family reuse detection (SPECS §2.5)."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),)

    id: Mapped[uuid.UUID] = pk_uuid()  # = jti of the refresh JWT
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    token_hash: Mapped[str] = mapped_column(String)  # SHA-256, never the raw token
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    expires_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = created_at_col()
