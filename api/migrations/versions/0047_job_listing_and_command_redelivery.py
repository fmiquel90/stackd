"""heartbeat job listing + command redelivery (SPECS §3.7, §7.1)

runs.unlisted_heartbeats: consecutive heartbeats of the run's worker that did not list its job.
worker_commands.sent_at / attempts: a command delivered without a result is delivered again.
ix_runs_worker_executing: the heartbeat looks up the executing runs of one worker on every beat.

Revision ID: 0047
Revises: 0042
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXECUTING = "'preparing','planning','checking','applying','running'"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("unlisted_heartbeats", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "worker_commands", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "worker_commands",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        f"CREATE INDEX ix_runs_worker_executing ON runs (worker_id) WHERE state IN ({_EXECUTING})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_worker_executing")
    op.drop_column("worker_commands", "attempts")
    op.drop_column("worker_commands", "sent_at")
    op.drop_column("runs", "unlisted_heartbeats")
