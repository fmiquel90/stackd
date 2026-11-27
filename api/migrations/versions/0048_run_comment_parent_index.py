"""index run_comments.parent_id (cascading self-reference)

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Deleting a comment (or its run) cascades to its replies: without an index each deleted
    # comment scanned the table. Replies only: a top-level comment has no parent.
    op.create_index(
        "ix_run_comments_parent_id",
        "run_comments",
        ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_comments_parent_id", table_name="run_comments")
