"""environment inline backend-config key/values (unmanaged state)

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Inline backend settings passed as `-backend-config=key=value` at init for managed_state=false
    # envs (bucket/key/region/dynamodb_table/use_lockfile…) — when there's no .config file yet.
    op.add_column("environments", sa.Column("backend_config", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("environments", "backend_config")
