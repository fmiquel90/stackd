"""environment backend-config file (unmanaged state)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Repo-relative file passed as `-backend-config=<file>` at init for managed_state=false envs
    # whose repo uses a partial backend (e.g. backend "s3" {} + values in a .config file).
    op.add_column("environments", sa.Column("backend_config_file", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("environments", "backend_config_file")
