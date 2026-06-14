"""command runs — RunType.command + RunState.running + runs.command

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The active states the one_active_run_per_env partial unique index guards (now incl. 'running').
_ACTIVE = "'preparing','planning','checking','unconfirmed','confirmed','applying','running'"
_ACTIVE_PREV = "'preparing','planning','checking','unconfirmed','confirmed','applying'"


def upgrade() -> None:
    # New enum values must be committed before they can be referenced (e.g. in the index WHERE),
    # and ADD VALUE cannot run in the same transaction that uses the value — use autocommit.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE run_type ADD VALUE IF NOT EXISTS 'command'")
        op.execute("ALTER TYPE run_state ADD VALUE IF NOT EXISTS 'running'")

    op.add_column("runs", sa.Column("command", postgresql.JSONB(), nullable=True))

    op.execute("DROP INDEX IF EXISTS one_active_run_per_env")
    op.execute(
        f"CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id) "
        f"WHERE state IN ({_ACTIVE})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS one_active_run_per_env")
    op.execute(
        f"CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id) "
        f"WHERE state IN ({_ACTIVE_PREV})"
    )
    op.drop_column("runs", "command")
    # Enum values are intentionally not dropped (Postgres can't drop a value in use safely).
