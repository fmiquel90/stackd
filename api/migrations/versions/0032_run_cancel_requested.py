"""cancelling an executing run: runs.cancel_requested_at (SPECS §4.1, §7.1)

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("runs", "cancel_requested_at")
