"""variable-set label selector + stack labels

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Org/selection tags on a stack (envs already carry `labels`); matched by a set's selector.
    op.add_column("stacks", sa.Column("labels", postgresql.JSONB(), nullable=True))
    # Rule-based auto-attach: a set attaches to any env whose effective labels (stack + env)
    # contain every key=value in this selector. NULL/empty = no rule (explicit/auto_attach only).
    op.add_column("variable_sets", sa.Column("selector", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("variable_sets", "selector")
    op.drop_column("stacks", "labels")
