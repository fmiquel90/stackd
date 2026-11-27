"""cloud integrations: named AWS profiles (SPECS §10.6)

Revision ID: 0038
Revises: 0036
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cloud_integrations",
        sa.Column(
            "profiles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_cloud_integrations_profiles_object",
        "cloud_integrations",
        "jsonb_typeof(profiles) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cloud_integrations_profiles_object", "cloud_integrations", type_="check")
    op.drop_column("cloud_integrations", "profiles")
