"""notifications — outbound targets + transactional outbox

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# attachment_target already exists (created in 0002 for hooks/variable sets); reuse it.
_attachment_target = postgresql.ENUM(
    "stack", "environment", name="attachment_target", create_type=False
)
# create_type=False: we create it explicitly below, so create_table must not re-emit CREATE TYPE.
_notification_kind = postgresql.ENUM(
    "slack", "webhook", name="notification_kind", create_type=False
)


def upgrade() -> None:
    _notification_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "notification_targets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("target_kind", _attachment_target, nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", _notification_kind, nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("on_states", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_notification_targets_target",
        "notification_targets",
        ["target_kind", "target_id"],
    )

    op.create_table(
        "notification_outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("to_state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The dispatcher drains unsent rows; a partial index keeps the poll cheap.
    op.create_index(
        "ix_notification_outbox_pending",
        "notification_outbox",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_pending", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("ix_notification_targets_target", table_name="notification_targets")
    op.drop_table("notification_targets")
    _notification_kind.drop(op.get_bind(), checkfirst=True)
