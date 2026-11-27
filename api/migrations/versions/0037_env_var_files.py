"""environment repo var files (§3.4)

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ordered repo tfvars files passed as `-var-file=` (relative to the stack's project_root).
    # Empty for every existing env: nothing changes until one is set.
    op.add_column(
        "environments",
        sa.Column("var_files", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("environments", "var_files")
