"""out-of-band state changes: env state baseline + out-of-band flag (SPECS §19.7)

Revision ID: 0041
Revises: 0036
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("state_serial", sa.Integer()),
    ("state_lineage", sa.String()),
    ("state_observed_at", sa.DateTime(timezone=True)),
    ("out_of_band_detected_at", sa.DateTime(timezone=True)),
    ("out_of_band_run_id", postgresql.UUID(as_uuid=True)),
    ("out_of_band_expected_serial", sa.Integer()),
    ("out_of_band_expected_lineage", sa.String()),
    ("out_of_band_observed_serial", sa.Integer()),
    ("out_of_band_observed_lineage", sa.String()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("environments", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("environments", name)
