"""outbox retries: per-row backoff schedule + per-target notification delivery (SPECS §7.5)

Revision ID: 0044
Revises: 0042
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTBOXES = ("notification_outbox", "vcs_outbox")


def upgrade() -> None:
    for table in _OUTBOXES:
        op.add_column(
            table,
            sa.Column(
                "next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        # The drain claims the due unsent rows: the partial index now leads on the due time.
        op.drop_index(f"ix_{table}_pending", table_name=table)
        op.create_index(
            f"ix_{table}_pending",
            table,
            ["next_attempt_at", "created_at"],
            postgresql_where=sa.text("sent_at IS NULL"),
        )
    op.add_column(
        "notification_outbox",
        sa.Column(
            "delivered_target_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_outbox", "delivered_target_ids")
    for table in _OUTBOXES:
        op.drop_index(f"ix_{table}_pending", table_name=table)
        op.create_index(
            f"ix_{table}_pending",
            table,
            ["created_at"],
            postgresql_where=sa.text("sent_at IS NULL"),
        )
        op.drop_column(table, "next_attempt_at")
