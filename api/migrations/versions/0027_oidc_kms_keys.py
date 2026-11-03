"""OIDC signing keys held in AWS KMS, and the time a key started retiring

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A key is either local (its private key AES-GCM encrypted here) or held in KMS (only its ARN
    # here: the private key never leaves KMS, signing goes through kms:Sign) — exactly one.
    op.alter_column("oidc_signing_keys", "private_key_encrypted", nullable=True)
    op.add_column("oidc_signing_keys", sa.Column("kms_key_arn", sa.String(), nullable=True))
    op.create_check_constraint(
        "oidc_key_one_source",
        "oidc_signing_keys",
        "num_nonnulls(private_key_encrypted, kms_key_arn) = 1",
    )
    # When the key stopped signing: it leaves the JWKS once the longest token it signed expired.
    op.add_column(
        "oidc_signing_keys",
        sa.Column("retiring_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One signing key at a time (§3.11), now that rotation exists.
    op.create_index(
        "one_active_oidc_key",
        "oidc_signing_keys",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("one_active_oidc_key", table_name="oidc_signing_keys")
    op.drop_column("oidc_signing_keys", "retiring_at")
    op.drop_constraint("oidc_key_one_source", "oidc_signing_keys", type_="check")
    op.drop_column("oidc_signing_keys", "kms_key_arn")
    op.alter_column("oidc_signing_keys", "private_key_encrypted", nullable=False)
