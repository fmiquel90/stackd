"""initial schema: spaces, users, refresh_tokens, audit_events

Revision ID: 0001
Revises:
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUIDV7 = sa.text("uuidv7()")

role_enum = postgresql.ENUM("reader", "writer", "approver", "admin", name="role")
tier_enum = postgresql.ENUM("dev", "staging", "prod", name="tier")
actor_kind_enum = postgresql.ENUM("user", "worker", "system", "webhook", name="audit_actor_kind")


def upgrade() -> None:
    bind = op.get_bind()
    role_enum.create(bind, checkfirst=True)
    tier_enum.create(bind, checkfirst=True)
    actor_kind_enum.create(bind, checkfirst=True)

    uuid_pk = lambda: sa.Column(  # noqa: E731
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7
    )

    op.create_table(
        "spaces",
        uuid_pk(),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        uuid_pk(),
        sa.Column("google_sub", sa.String(), nullable=False, unique=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("avatar_url", sa.String(), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM(name="role", create_type=False),
            nullable=False,
            server_default="reader",
        ),
        sa.Column("max_apply_tier", postgresql.ENUM(name="tier", create_type=False), nullable=True),
        sa.Column("can_destroy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "audit_events",
        uuid_pk(),
        sa.Column(
            "actor_kind",
            postgresql.ENUM(name="audit_actor_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_email", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_id", "created_at"])
    op.create_index(
        "ix_audit_events_target", "audit_events", ["target_kind", "target_id", "created_at"]
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action", "created_at"])

    # DB-level append-only enforcement (SPECS §6.1). In prod the app role is additionally
    # REVOKE'd UPDATE/DELETE and a separate role handles retention purge; the trigger is the
    # backstop that holds even for a compromised application role.
    op.execute(
        """
        CREATE FUNCTION audit_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (SPECS 6.1)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_mutate
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_mutate ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable")
    op.drop_table("audit_events")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("spaces")
    actor_kind_enum.drop(op.get_bind(), checkfirst=True)
    tier_enum.drop(op.get_bind(), checkfirst=True)
    role_enum.drop(op.get_bind(), checkfirst=True)
