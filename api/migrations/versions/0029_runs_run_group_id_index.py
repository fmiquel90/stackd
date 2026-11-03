"""index runs by run group

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A run group's members are looked up by `run_group_id` (§9.2/§9.4): on every cascade, every
    # group status, and every GET /runs/{id} (does this run root a group?). Most runs carry no
    # group, so the index only holds the ones that do.
    op.create_index(
        "ix_runs_run_group_id",
        "runs",
        ["run_group_id"],
        postgresql_where=sa.text("run_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_run_group_id", table_name="runs")
