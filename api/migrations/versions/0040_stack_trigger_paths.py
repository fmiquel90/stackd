"""stacks.trigger_paths: extra repo paths that concern a stack (SPECS §3.1, §9.6)

Revision ID: 0040
Revises: 0036
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stacks",
        sa.Column(
            "trigger_paths",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("stacks", "trigger_paths")
