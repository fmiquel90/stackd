from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import RepoAuthKind, Tool


class Stack(Base):
    """The template: repo + code (SPECS §3.1). Branch/state/autodeploy live on the env."""

    __tablename__ = "stacks"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_stacks_space_id_name"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, default=None)

    repo_url: Mapped[str] = mapped_column(String)
    repo_auth_kind: Mapped[RepoAuthKind] = mapped_column(
        Enum(RepoAuthKind, name="repo_auth_kind"), default=RepoAuthKind.none
    )
    repo_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    webhook_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    project_root: Mapped[str] = mapped_column(String, default=".")
    tool: Mapped[Tool] = mapped_column(Enum(Tool, name="tool"), default=Tool.opentofu)
    tool_version: Mapped[str] = mapped_column(String)

    # Org/selection tags (e.g. {"team": "payments"}), matched by a variable set's selector (§3.4).
    labels: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), default=None)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
