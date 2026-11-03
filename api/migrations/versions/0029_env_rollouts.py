"""environment rollouts (SPECS §21): rollouts, their members, runs.rollout_id, trigger 'rollout'

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")


def upgrade() -> None:
    op.execute("ALTER TYPE triggered_by ADD VALUE IF NOT EXISTS 'rollout'")
    state = postgresql.ENUM(
        "awaiting_approval",
        "running",
        "paused",
        "succeeded",
        "failed",
        "canceled",
        name="rollout_state",
    )
    status = postgresql.ENUM(
        "pending",
        "planning",
        "needs_confirmation",
        "applying",
        "done",
        "failed",
        "skipped",
        "canceled",
        name="rollout_member_status",
    )
    op.create_table(
        "env_rollouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),  # display name: the env name rolled out
        sa.Column("state", state, nullable=False, server_default="awaiting_approval"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "env_rollout_members",
        sa.Column(
            "rollout_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("env_rollouts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False, server_default="pending"),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # What the approval saw of the member's environment (§21.5): a change since refuses the
        # auto-confirmation until a re-approval.
        sa.Column("approved_tier", sa.String(), nullable=True),
        sa.Column("approved_protected", sa.Boolean(), nullable=True),
        sa.Column("approved_four_eyes", sa.Boolean(), nullable=True),
    )
    # One active rollout per environment (§21.3).
    op.create_index(
        "one_active_rollout_per_env",
        "env_rollout_members",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.add_column(
        "runs",
        sa.Column(
            "rollout_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("env_rollouts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_runs_rollout_id", "runs", ["rollout_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_rollout_id", table_name="runs")
    op.drop_column("runs", "rollout_id")
    op.drop_index("one_active_rollout_per_env", table_name="env_rollout_members")
    op.drop_table("env_rollout_members")
    op.drop_table("env_rollouts")
    op.execute("DROP TYPE rollout_member_status")
    op.execute("DROP TYPE rollout_state")
    # The enum value 'rollout' of triggered_by is left in place (PG can't drop an enum value).
