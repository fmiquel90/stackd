"""referential integrity — environments.tier → tiers.name (ON DELETE RESTRICT)

DB backstop for the app-level guards (env-create validation + the delete-tier-in-use 409), so an
env can never point at a missing tier (which would weaken the fail-closed four-eyes check, §2.4).

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_environments_tier",
        "environments",
        "tiers",
        ["tier"],
        ["name"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_environments_tier", "environments", type_="foreignkey")
