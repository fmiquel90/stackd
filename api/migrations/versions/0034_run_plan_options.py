"""runs.plan_options: targeted / replace plans (SPECS §3.5, §7.4)

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("plan_options", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "plan_options")
