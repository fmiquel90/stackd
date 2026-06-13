from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import CloudProvider, OidcKeyStatus


class OidcSigningKey(Base):
    """Issuer signing key (SPECS §3.11). Rotation keeps `retiring` keys in the JWKS."""

    __tablename__ = "oidc_signing_keys"

    id: Mapped[uuid.UUID] = pk_uuid()
    kid: Mapped[str] = mapped_column(String, unique=True)
    algorithm: Mapped[str] = mapped_column(String, default="RS256")
    public_jwk: Mapped[dict] = mapped_column(JSONB)
    private_key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary
    )  # AES-GCM (or KMS ref in prod)
    status: Mapped[OidcKeyStatus] = mapped_column(
        Enum(OidcKeyStatus, name="oidc_key_status"), default=OidcKeyStatus.active
    )
    created_at: Mapped[datetime] = created_at_col()
    retired_at: Mapped[datetime | None] = mapped_column(default=None)


class CloudIntegration(Base):
    """Per-env cloud workload identity (SPECS §3.10). Plan vs apply assume different roles."""

    __tablename__ = "cloud_integrations"

    id: Mapped[uuid.UUID] = pk_uuid()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[CloudProvider] = mapped_column(
        Enum(CloudProvider, name="cloud_provider"), default=CloudProvider.aws
    )
    plan_role_arn: Mapped[str] = mapped_column(String)
    apply_role_arn: Mapped[str] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String, default=None)
    session_duration: Mapped[int] = mapped_column(Integer, default=3600)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
