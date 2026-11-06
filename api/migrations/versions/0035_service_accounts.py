"""service accounts + API tokens (SPECS §23)

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_kind = postgresql.ENUM("human", "service", name="user_kind")


def upgrade() -> None:
    user_kind.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column(
            "kind",
            postgresql.ENUM(name="user_kind", create_type=False),
            nullable=False,
            server_default="human",
        ),
    )
    # A service account has no Google identity.
    op.alter_column("users", "google_sub", existing_type=sa.String(), nullable=True)
    op.create_check_constraint(
        "ck_users_google_sub_human", "users", "kind = 'service' OR google_sub IS NOT NULL"
    )
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_constraint("ck_users_google_sub_human", "users", type_="check")
    op.execute("DELETE FROM users WHERE kind = 'service'")
    op.alter_column("users", "google_sub", existing_type=sa.String(), nullable=False)
    op.drop_column("users", "kind")
    user_kind.drop(op.get_bind(), checkfirst=True)
