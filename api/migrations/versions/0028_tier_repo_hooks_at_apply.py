"""tier flag: may repo hooks run at the apply stages

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fail closed (§8.3): a repo hook at before_apply/after_apply runs with the apply role's
    # credentials in reach of the job, so no tier allows it until an admin says so — existing tiers
    # included. A missing tier row counts as false too.
    op.add_column(
        "tiers",
        sa.Column("repo_hooks_at_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tiers", "repo_hooks_at_apply")
