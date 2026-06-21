"""Per-space RBAC: space_memberships + backfill from instance defaults (Phase F)

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "space_memberships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Reuses the existing `role` enum (created in 0001).
        sa.Column(
            "role",
            postgresql.ENUM(name="role", create_type=False),
            nullable=False,
            server_default="reader",
        ),
        sa.Column(
            "allowed_tiers",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("can_destroy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("space_id", "user_id", name="uq_space_membership"),
    )
    op.create_index("ix_space_memberships_user", "space_memberships", ["user_id"])

    # Backfill: one membership per (existing space x existing user), copying the user's instance
    # role/allowed_tiers/can_destroy so current behaviour (everyone sees everything) is preserved.
    op.execute(
        """
        INSERT INTO space_memberships (id, space_id, user_id, role, allowed_tiers, can_destroy)
        SELECT uuidv7(), s.id, u.id, u.role, u.allowed_tiers, u.can_destroy
        FROM spaces s CROSS JOIN users u
        ON CONFLICT (space_id, user_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_space_memberships_user", table_name="space_memberships")
    op.drop_table("space_memberships")
