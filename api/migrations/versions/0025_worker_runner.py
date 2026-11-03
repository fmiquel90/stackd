"""worker runner (the execution runner a worker declares at registration)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `local` | `docker` as declared by the agent (§7.4); NULL for an agent that predates the field.
    # A production API refuses any worker whose runner is not `docker`.
    op.add_column("workers", sa.Column("runner", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "runner")
