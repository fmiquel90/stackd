from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import SecretProvider


class SecretSource(Base):
    """A configured external secrets-manager connection, scoped to a space (SPECS §15.1).

    Holds the single bootstrap credential used to fetch the referenced secrets (e.g. a Proton Pass
    Personal Access Token). The credential is AES-256-GCM at rest and write-only — never returned.
    Variables point at a source via `secret_source_id` + a provider-specific `secret_ref`.
    """

    __tablename__ = "secret_sources"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_secret_sources_space_id_name"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String)
    provider: Mapped[SecretProvider] = mapped_column(Enum(SecretProvider, name="secret_provider"))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)  # non-sensitive provider config
    bootstrap_secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)  # AES-GCM, write-only
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
