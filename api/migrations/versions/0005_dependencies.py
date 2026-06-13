"""env_dependencies, output_references, env_outputs

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")
trigger_policy = postgresql.ENUM(
    "on_output_change", "always", "never", name="trigger_policy", create_type=False
)


def upgrade() -> None:
    postgresql.ENUM("on_output_change", "always", "never", name="trigger_policy").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "env_dependencies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "upstream_env_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "downstream_env_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "trigger_policy", trigger_policy, nullable=False, server_default="on_output_change"
        ),
        sa.UniqueConstraint(
            "upstream_env_id", "downstream_env_id", name="uq_env_dependencies_edge"
        ),
        sa.CheckConstraint(
            "upstream_env_id <> downstream_env_id", name="ck_env_dependencies_no_self"
        ),
    )

    op.create_table(
        "output_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "dependency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("env_dependencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("output_name", sa.String(), nullable=False),
        sa.Column("input_name", sa.String(), nullable=False),
        sa.Column("mock_value", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("dependency_id", "input_name", name="uq_output_references_input"),
    )

    op.create_table(
        "env_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("value_hash", sa.String(), nullable=True),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("environment_id", "name", name="uq_env_outputs_name"),
    )


def downgrade() -> None:
    op.drop_table("env_outputs")
    op.drop_table("output_references")
    op.drop_table("env_dependencies")
    postgresql.ENUM(name="trigger_policy").drop(op.get_bind(), checkfirst=True)
