"""state_versions, state_locks

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")


def upgrade() -> None:
    op.create_table(
        "state_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("serial", sa.Integer(), nullable=False),
        sa.Column("lineage", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s3_key", sa.String(), nullable=False),
        sa.Column("created_by_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_state_versions_env", "state_versions", ["environment_id", "serial"])

    op.create_table(
        "state_locks",
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("lock_id", sa.String(), nullable=False),
        sa.Column("info", postgresql.JSONB(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("state_locks")
    op.drop_index("ix_state_versions_env", table_name="state_versions")
    op.drop_table("state_versions")
