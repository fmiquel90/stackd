"""oidc_signing_keys, cloud_integrations

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")
key_status = postgresql.ENUM(
    "active", "retiring", "retired", name="oidc_key_status", create_type=False
)
cloud_provider = postgresql.ENUM("aws", name="cloud_provider", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("active", "retiring", "retired", name="oidc_key_status").create(
        bind, checkfirst=True
    )
    postgresql.ENUM("aws", name="cloud_provider").create(bind, checkfirst=True)

    op.create_table(
        "oidc_signing_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column("kid", sa.String(), nullable=False, unique=True),
        sa.Column("algorithm", sa.String(), nullable=False, server_default="RS256"),
        sa.Column("public_jwk", postgresql.JSONB(), nullable=False),
        sa.Column("private_key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("status", key_status, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "cloud_integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", cloud_provider, nullable=False, server_default="aws"),
        sa.Column("plan_role_arn", sa.String(), nullable=False),
        sa.Column("apply_role_arn", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("session_duration", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("cloud_integrations")
    op.drop_table("oidc_signing_keys")
    postgresql.ENUM(name="cloud_provider").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="oidc_key_status").drop(op.get_bind(), checkfirst=True)
