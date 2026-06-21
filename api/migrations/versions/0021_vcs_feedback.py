"""VCS feedback loop — run PR metadata + vcs_outbox (Phase A)

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")


def upgrade() -> None:
    op.add_column("runs", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("vcs_provider", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("vcs_comment_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("vcs_head_sha", sa.String(), nullable=True))

    # Transactional outbox for VCS post-back (mirrors notification_outbox): enqueued in the run
    # transition txn, drained by the scheduler after commit (at-least-once).
    op.create_table(
        "vcs_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("to_state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_vcs_outbox_pending",
        "vcs_outbox",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_vcs_outbox_pending", table_name="vcs_outbox")
    op.drop_table("vcs_outbox")
    op.drop_column("runs", "vcs_head_sha")
    op.drop_column("runs", "vcs_comment_id")
    op.drop_column("runs", "vcs_provider")
    op.drop_column("runs", "pr_number")
