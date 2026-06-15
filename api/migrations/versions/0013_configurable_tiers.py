"""configurable, non-linear tiers — tiers catalog, users.allowed_tiers, env.tier as text

Replaces the fixed `dev < staging < prod` enum and the single `users.max_apply_tier` ceiling with a
configurable `tiers` catalog and a per-user set of allowed tiers (SPECS §2.4). The data migration
preserves current access: because tiers used to be linear, a ceiling of X becomes the set of every
tier ranked ≤ X.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")


def upgrade() -> None:
    op.create_table(
        "tiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("requires_four_eyes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Seed the previous fixed set; prod keeps the old hardcoded four-eyes behavior.
    op.execute(
        "INSERT INTO tiers (name, requires_four_eyes, position) VALUES "
        "('dev', false, 0), ('staging', false, 1), ('prod', true, 2)"
    )

    # environments.tier: enum -> text (the value names are unchanged).
    op.alter_column(
        "environments",
        "tier",
        type_=sa.String(),
        existing_type=postgresql.ENUM("dev", "staging", "prod", name="tier"),
        postgresql_using="tier::text",
    )

    # users: add the set, backfill from the linear ceiling, drop the ceiling.
    op.add_column(
        "users",
        sa.Column(
            "allowed_tiers",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.execute(
        "UPDATE users SET allowed_tiers = CASE max_apply_tier::text "
        "WHEN 'prod' THEN ARRAY['dev','staging','prod'] "
        "WHEN 'staging' THEN ARRAY['dev','staging'] "
        "WHEN 'dev' THEN ARRAY['dev'] "
        "ELSE ARRAY[]::text[] END"
    )
    op.drop_column("users", "max_apply_tier")

    # The enum type is now unused by any column.
    op.execute("DROP TYPE tier")


def downgrade() -> None:
    tier_enum = postgresql.ENUM("dev", "staging", "prod", name="tier")
    tier_enum.create(op.get_bind(), checkfirst=True)

    # Collapse the set back to a single ceiling = highest-ranked allowed tier (lossy for custom).
    op.add_column("users", sa.Column("max_apply_tier", tier_enum, nullable=True))
    op.execute(
        "UPDATE users SET max_apply_tier = CASE "
        "WHEN 'prod' = ANY(allowed_tiers) THEN 'prod'::tier "
        "WHEN 'staging' = ANY(allowed_tiers) THEN 'staging'::tier "
        "WHEN 'dev' = ANY(allowed_tiers) THEN 'dev'::tier "
        "ELSE NULL END"
    )
    op.drop_column("users", "allowed_tiers")

    # environments.tier back to the enum (rows referencing custom tiers would fail — acceptable on a
    # downgrade of a feature whose whole point is custom tiers).
    op.alter_column(
        "environments",
        "tier",
        type_=tier_enum,
        existing_type=sa.String(),
        postgresql_using="tier::tier",
    )
    op.drop_table("tiers")
