"""worker tools (the IaC tool versions a worker can run)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # {"opentofu": ["1.12.2"], "terraform": ["1.14.9"]} as declared by the agent (§7.4); the claim
    # only hands a worker a run whose stack's tool_version it can run. NULL: an agent predating it.
    op.add_column("workers", sa.Column("tools", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "tools")
