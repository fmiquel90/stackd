"""claim backoff columns + claimable-runs partial index (SPECS §3.5, §7.2)

Revision ID: 0043
Revises: 0046
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("claimable_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runs",
        sa.Column("claim_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # The claim's candidate set, in its ORDER BY order (after the affinity key): every poll of
    # every idle worker reads it, and it stays small while `runs` grows with history.
    op.create_index(
        "ix_runs_claimable",
        "runs",
        ["is_drift", "created_at"],
        postgresql_where=sa.text("state IN ('queued', 'confirmed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_claimable", table_name="runs")
    op.drop_column("runs", "claim_attempts")
    op.drop_column("runs", "claimable_after")
