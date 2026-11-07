"""service accounts: unique name (SPECS §23)

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A service account's email is derived from its unique name: two concurrent creates of one
    # name must not both succeed (a check-then-insert alone races).
    op.create_index(
        "uq_users_service_account_email",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("kind = 'service'"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_service_account_email", table_name="users")
