"""runs, run_events, workers, hooks, run_logs

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")

run_type = postgresql.ENUM("tracked", "proposed", "destroy", name="run_type", create_type=False)
run_state = postgresql.ENUM(
    "queued",
    "preparing",
    "planning",
    "checking",
    "unconfirmed",
    "confirmed",
    "applying",
    "finished",
    "failed",
    "discarded",
    "canceled",
    name="run_state",
    create_type=False,
)
triggered_by = postgresql.ENUM(
    "manual", "webhook", "dependency", "api", name="triggered_by", create_type=False
)
run_event_actor = postgresql.ENUM(
    "system", "user", "worker", name="run_event_actor", create_type=False
)
worker_status = postgresql.ENUM("idle", "busy", "offline", name="worker_status", create_type=False)
hook_stage = postgresql.ENUM(
    "before_init",
    "after_init",
    "before_plan",
    "after_plan",
    "before_apply",
    "after_apply",
    name="hook_stage",
    create_type=False,
)
hook_on_failure = postgresql.ENUM("fail", "warn", name="hook_on_failure", create_type=False)

ALL_ENUMS = (
    run_type,
    run_state,
    triggered_by,
    run_event_actor,
    worker_status,
    hook_stage,
    hook_on_failure,
)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7)


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ALL_ENUMS:
        # create_type defaults False on these objects; create explicitly here.
        postgresql.ENUM(*enum.enums, name=enum.name).create(bind, checkfirst=True)

    attachment_target = postgresql.ENUM(name="attachment_target", create_type=False)  # from 0002

    op.create_table(
        "worker_pools",
        _uuid_pk(),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("labels", postgresql.JSONB(), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        _ts("created_at"),
    )

    op.create_table(
        "workers",
        _uuid_pk(),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worker_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", worker_status, nullable=False, server_default="idle"),
        sa.Column("labels", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        _ts("last_heartbeat_at", nullable=True),
        _ts("registered_at"),
    )

    op.create_table(
        "runs",
        _uuid_pk(),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", run_type, nullable=False, server_default="tracked"),
        sa.Column("state", run_state, nullable=False, server_default="queued"),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("commit_message", sa.String(), nullable=True),
        sa.Column("commit_author", sa.String(), nullable=True),
        sa.Column("triggered_by", triggered_by, nullable=False),
        sa.Column("trigger_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_summary", postgresql.JSONB(), nullable=True),
        sa.Column("check_results", postgresql.JSONB(), nullable=True),
        sa.Column("resolved_inputs", postgresql.JSONB(), nullable=True),
        sa.Column("used_mocks", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("variable_provenance", postgresql.JSONB(), nullable=True),
        _ts("claimed_at", nullable=True),
        _ts("confirmed_at", nullable=True),
        _ts("finished_at", nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_runs_environment_id", "runs", ["environment_id", "created_at"])
    # Concurrency invariant (§3.5): one active run per environment.
    op.execute(
        """
        CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id)
        WHERE state IN ('preparing','planning','checking','unconfirmed','confirmed','applying')
        """
    )

    op.create_table(
        "run_events",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", run_state, nullable=True),
        sa.Column("to_state", run_state, nullable=False),
        sa.Column("actor", run_event_actor, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id", "created_at"])

    op.create_table(
        "hooks",
        _uuid_pk(),
        sa.Column("target_kind", attachment_target, nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", hook_stage, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=False),
        sa.Column("on_failure", hook_on_failure, nullable=False, server_default="fail"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_hooks_target", "hooks", ["target_kind", "target_id"])

    op.create_table(
        "run_logs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("phase", sa.String(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("section", sa.String(), nullable=True),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
        _ts("created_at"),
    )


def downgrade() -> None:
    op.drop_table("run_logs")
    op.drop_index("ix_hooks_target", table_name="hooks")
    op.drop_table("hooks")
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.execute("DROP INDEX IF EXISTS one_active_run_per_env")
    op.drop_index("ix_runs_environment_id", table_name="runs")
    op.drop_table("runs")
    op.drop_table("workers")
    op.drop_table("worker_pools")
    bind = op.get_bind()
    for enum in ALL_ENUMS:
        postgresql.ENUM(name=enum.name).drop(bind, checkfirst=True)
