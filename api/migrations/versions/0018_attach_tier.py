"""variable-set attachment to a tier

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Allow attaching a variable set to a whole tier (target_id = tiers.id). Additive enum value;
    # PG 12+ permits ADD VALUE inside the migration transaction since it isn't used here.
    op.execute("ALTER TYPE attachment_target ADD VALUE IF NOT EXISTS 'tier'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without recreating the type; leave the value in place.
    pass
