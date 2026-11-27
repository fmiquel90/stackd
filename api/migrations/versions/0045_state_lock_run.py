"""state lock holder run: released when that run ends (SPECS §3.9 / §11.2)

Revision ID: 0045
Revises: 0042
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Locks held at upgrade time keep a NULL holder run: they are never auto-released, as before.
    op.add_column("state_locks", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_state_locks_run_id", "state_locks", "runs", ["run_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_state_locks_run_id", "state_locks", type_="foreignkey")
    op.drop_column("state_locks", "run_id")
