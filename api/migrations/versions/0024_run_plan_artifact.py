"""run saved-plan artifact key (the plan the apply job applies)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # S3 key of the encrypted saved plan (§7.4/§11.3) while the run can still apply it; NULL once
    # the run is terminal (the object is deleted then) or when no plan was saved.
    op.add_column("runs", sa.Column("plan_artifact_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "plan_artifact_key")
