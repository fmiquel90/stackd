"""referential integrity — FK run_id columns to runs (ON DELETE SET NULL)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# env_outputs.run_id and state_versions.created_by_run_id pointed at runs without a FK, so a run
# deletion left dangling pointers. SET NULL preserves the output/version row (history) while
# clearing the broken reference. (variable_set_attachments.target_id is polymorphic — stack OR
# environment — so it can't take a single FK; it stays app-enforced, see SPECS §3.4.)


def upgrade() -> None:
    # Null out any pre-existing dangling references first, so ADD CONSTRAINT can't fail on a
    # database that already had runs deleted (the SET NULL we now enforce, applied to history).
    op.execute(
        "UPDATE env_outputs SET run_id = NULL "
        "WHERE run_id IS NOT NULL AND run_id NOT IN (SELECT id FROM runs)"
    )
    op.create_foreign_key(
        "fk_env_outputs_run_id", "env_outputs", "runs", ["run_id"], ["id"], ondelete="SET NULL"
    )
    op.execute(
        "UPDATE state_versions SET created_by_run_id = NULL "
        "WHERE created_by_run_id IS NOT NULL AND created_by_run_id NOT IN (SELECT id FROM runs)"
    )
    op.create_foreign_key(
        "fk_state_versions_created_by_run_id",
        "state_versions",
        "runs",
        ["created_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_state_versions_created_by_run_id", "state_versions", type_="foreignkey")
    op.drop_constraint("fk_env_outputs_run_id", "env_outputs", type_="foreignkey")
