"""cold log archiving (SPECS §5.2) + audit purge role (SPECS §6.1, §7.5)

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Set once the run's logs are in S3 (logs/{run_id}/{phase}.log.gz); the hot rows are purged
    # `log_hot_retention_days` after it.
    op.add_column("runs", sa.Column("logs_archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_runs_logs_to_archive",
        "runs",
        ["finished_at"],
        postgresql_where=sa.text("finished_at IS NOT NULL AND logs_archived_at IS NULL"),
    )

    # Retention purge goes through a distinct role (SPECS §6.1): the application role keeps
    # INSERT/SELECT only and the append-only trigger now lets exactly `stackd_audit_purge` delete.
    # The role is created NOLOGIN (an operator gives it LOGIN + a password and points
    # STACKD_AUDIT_PURGE_DATABASE_URL at it); where the migration user may not create roles the
    # step is skipped and the purge simply stays unavailable.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stackd_audit_purge') THEN
                CREATE ROLE stackd_audit_purge NOLOGIN;
            END IF;
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO stackd_audit_purge', current_database()
            );
            GRANT USAGE ON SCHEMA public TO stackd_audit_purge;
            GRANT SELECT, INSERT, DELETE ON audit_events TO stackd_audit_purge;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'stackd_audit_purge role not provisioned (insufficient privilege)';
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_user = 'stackd_audit_purge' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_events is append-only (SPECS 6.1)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (SPECS 6.1)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.drop_index("ix_runs_logs_to_archive", table_name="runs")
    op.drop_column("runs", "logs_archived_at")
