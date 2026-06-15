"""external secret sources — secret_sources table, variable references, fallback apply gate

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")
secret_provider = postgresql.ENUM("proton_pass", name="secret_provider", create_type=False)
secret_fallback = postgresql.ENUM(
    "error", "static", "break_glass", name="secret_fallback", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("proton_pass", name="secret_provider").create(bind, checkfirst=True)
    postgresql.ENUM("error", "static", "break_glass", name="secret_fallback").create(
        bind, checkfirst=True
    )

    op.create_table(
        "secret_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider", secret_provider, nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("bootstrap_secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("space_id", "name", name="uq_secret_sources_space_id_name"),
    )

    # Variable can carry its value by reference instead of by stored value (§15.1).
    op.add_column(
        "variables",
        sa.Column(
            "secret_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_sources.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("variables", sa.Column("secret_ref", sa.String(), nullable=True))
    op.add_column(
        "variables",
        sa.Column(
            "secret_fallback_mode",
            secret_fallback,
            nullable=False,
            server_default="error",
        ),
    )
    op.add_column(
        "variables", sa.Column("secret_fallback_encrypted", sa.LargeBinary(), nullable=True)
    )
    op.create_check_constraint(
        "one_value_source",
        "variables",
        "(value IS NOT NULL)::int + (value_encrypted IS NOT NULL)::int "
        "+ (secret_source_id IS NOT NULL)::int = 1",
    )

    op.add_column(
        "runs",
        sa.Column("used_secret_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("runs", sa.Column("secret_overrides_encrypted", sa.LargeBinary(), nullable=True))
    op.add_column(
        "environments",
        sa.Column("allow_fallback_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("environments", "allow_fallback_apply")
    op.drop_column("runs", "secret_overrides_encrypted")
    op.drop_column("runs", "used_secret_fallback")
    op.drop_constraint("one_value_source", "variables", type_="check")
    op.drop_column("variables", "secret_fallback_encrypted")
    op.drop_column("variables", "secret_fallback_mode")
    op.drop_column("variables", "secret_ref")
    op.drop_column("variables", "secret_source_id")
    op.drop_table("secret_sources")
    postgresql.ENUM(name="secret_fallback").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="secret_provider").drop(op.get_bind(), checkfirst=True)
