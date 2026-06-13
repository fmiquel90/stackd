"""stacks, environments, variable sets, variables

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")
NIL_UUID = "00000000-0000-0000-0000-000000000000"

# create_type=False so create_table does not re-emit CREATE TYPE; we create them explicitly below.
repo_auth_kind = postgresql.ENUM(
    "none", "token", "deploy_key", name="repo_auth_kind", create_type=False
)
tool_enum = postgresql.ENUM("opentofu", "terraform", name="tool", create_type=False)
variable_kind = postgresql.ENUM("terraform", "environment", name="variable_kind", create_type=False)
attachment_target = postgresql.ENUM(
    "stack", "environment", name="attachment_target", create_type=False
)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now())


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (repo_auth_kind, tool_enum, variable_kind, attachment_target):
        enum.create(bind, checkfirst=True)

    tier = postgresql.ENUM(name="tier", create_type=False)  # created in 0001

    op.create_table(
        "stacks",
        _uuid_pk(),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("repo_auth_kind", repo_auth_kind, nullable=False, server_default="none"),
        sa.Column("repo_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("webhook_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("project_root", sa.String(), nullable=False, server_default="."),
        sa.Column("tool", tool_enum, nullable=False, server_default="opentofu"),
        sa.Column("tool_version", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("space_id", "name", name="uq_stacks_space_id_name"),
    )

    op.create_table(
        "environments",
        _uuid_pk(),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stacks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("tier", tier, nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("autodeploy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "require_second_pair_of_eyes", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("managed_state", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_mock_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("head_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commits_ahead", sa.Integer(), nullable=True),
        sa.Column("affects_project_root", sa.Boolean(), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("labels", postgresql.JSONB(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("stack_id", "name", name="uq_environments_stack_id_name"),
    )

    op.create_table(
        "variable_sets",
        _uuid_pk(),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("auto_attach", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("space_id", "name", name="uq_variable_sets_space_id_name"),
    )

    op.create_table(
        "variable_set_attachments",
        _uuid_pk(),
        sa.Column(
            "variable_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_kind", attachment_target, nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "variable_set_id", "target_kind", "target_id", name="uq_variable_set_attachments_target"
        ),
    )

    op.create_table(
        "variables",
        _uuid_pk(),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stacks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "variable_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", variable_kind, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("hcl", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "(variable_set_id IS NOT NULL) <> (stack_id IS NOT NULL)",
            name="ck_variables_one_parent",
        ),
        sa.CheckConstraint(
            "variable_set_id IS NULL OR environment_id IS NULL",
            name="ck_variables_set_var_not_env_scoped",
        ),
    )

    # Partial unique indexes (§3.3) — COALESCE so two stack vars with NULL env collide as intended.
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_variables_stack_scope ON variables
          (stack_id, COALESCE(environment_id, '{NIL_UUID}'::uuid), kind, name)
          WHERE stack_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_variables_set_scope ON variables
          (variable_set_id, kind, name)
          WHERE variable_set_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("variables")
    op.drop_table("variable_set_attachments")
    op.drop_table("variable_sets")
    op.drop_table("environments")
    op.drop_table("stacks")
    bind = op.get_bind()
    for enum in (attachment_target, variable_kind, tool_enum, repo_auth_kind):
        enum.drop(bind, checkfirst=True)
