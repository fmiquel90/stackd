"""Drift detection — env drift status + run drift flag + schedule trigger (Phase B)

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New trigger source for scheduler-driven runs (drift). PG12+ allows ADD VALUE inside a txn as
    # long as the value isn't used in the same migration (it isn't).
    op.execute("ALTER TYPE triggered_by ADD VALUE IF NOT EXISTS 'schedule'")

    op.add_column(
        "environments",
        sa.Column("drift_status", sa.String(), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "environments",
        sa.Column("last_drift_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "environments", sa.Column("drift_run_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "environments",
        sa.Column("drift_check_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "runs", sa.Column("is_drift", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("runs", "is_drift")
    op.drop_column("environments", "drift_check_enabled")
    op.drop_column("environments", "drift_run_id")
    op.drop_column("environments", "last_drift_checked_at")
    op.drop_column("environments", "drift_status")
    # The enum value 'schedule' is left in place (PG can't drop an enum value).
