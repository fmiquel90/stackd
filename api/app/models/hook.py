from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid, updated_at_col
from app.enums import AttachmentTarget, HookOnFailure, HookStage


class Hook(Base):
    """Platform hook (SPECS §8.1): UI/API-defined, NOT bypassable by a PR. Repo hooks (.stackd.yml)
    are merged in by the agent at run time."""

    __tablename__ = "hooks"

    id: Mapped[uuid.UUID] = pk_uuid()
    target_kind: Mapped[AttachmentTarget] = mapped_column(
        Enum(AttachmentTarget, name="attachment_target")
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    stage: Mapped[HookStage] = mapped_column(Enum(HookStage, name="hook_stage"))
    name: Mapped[str] = mapped_column(String)
    command: Mapped[str] = mapped_column(String)
    on_failure: Mapped[HookOnFailure] = mapped_column(
        Enum(HookOnFailure, name="hook_on_failure"), default=HookOnFailure.fail
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
