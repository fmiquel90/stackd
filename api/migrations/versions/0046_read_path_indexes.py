"""read-path indexes: run list keyset, last applied run, non-terminal runs, FKs to runs

Revision ID: 0046
Revises: 0042
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors app.enums.TERMINAL_STATES; /metrics renders its terminal states as literals so that the
# planner matches the partial index even on a generic plan.
_NON_TERMINAL = "state NOT IN ('finished', 'failed', 'discarded', 'canceled')"

# Cascading FKs to runs / run_comments with no index: deleting a stack or an environment cascades
# to its runs, and each deleted run then scanned these tables. Nullable columns index their
# non-null rows only (the cascade's `col = $1` implies it).
_FK_INDEXES = (
    ("ix_user_notifications_run_id", "user_notifications", "run_id", "run_id IS NOT NULL"),
    (
        "ix_user_notifications_comment_id",
        "user_notifications",
        "comment_id",
        "comment_id IS NOT NULL",
    ),
    ("ix_notification_outbox_run_id", "notification_outbox", "run_id", None),
    ("ix_vcs_outbox_run_id", "vcs_outbox", "run_id", None),
    ("ix_env_rollout_members_run_id", "env_rollout_members", "run_id", "run_id IS NOT NULL"),
)


def upgrade() -> None:
    # GET /environments/{id}/runs pages on (created_at, id) (SPECS §3.5): the old
    # (environment_id, created_at) index is a prefix of this one, so it goes.
    op.create_index("ix_runs_env_created_id", "runs", ["environment_id", "created_at", "id"])
    op.drop_index("ix_runs_environment_id", table_name="runs")

    # An environment's last finished tracked run (its last applied commit, §9.6): DISTINCT ON
    # (environment_id) ordered by finished_at DESC, id DESC.
    op.create_index(
        "ix_runs_last_finished_tracked",
        "runs",
        ["environment_id", sa.text("finished_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("type = 'tracked' AND state = 'finished'"),
    )

    # /metrics counts the non-terminal runs at every scrape (§20.1): a few rows, not the table.
    op.create_index(
        "ix_runs_non_terminal_state", "runs", ["state"], postgresql_where=sa.text(_NON_TERMINAL)
    )

    for name, table, column, where in _FK_INDEXES:
        op.create_index(
            name,
            table,
            [column],
            postgresql_where=sa.text(where) if where else None,
        )


def downgrade() -> None:
    for name, table, _column, _where in reversed(_FK_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_index("ix_runs_non_terminal_state", table_name="runs")
    op.drop_index("ix_runs_last_finished_tracked", table_name="runs")
    op.create_index("ix_runs_environment_id", "runs", ["environment_id", "created_at"])
    op.drop_index("ix_runs_env_created_id", table_name="runs")
