"""in-app notification center — user_notifications (inbound, per-user)

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")


def upgrade() -> None:
    op.create_table(
        "user_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "comment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run_comments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # The feed query is "my unread/recent, newest first".
    op.create_index(
        "ix_user_notifications_user_created",
        "user_notifications",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_user_notifications_user_created", table_name="user_notifications")
    op.drop_table("user_notifications")
