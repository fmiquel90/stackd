from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid


class Tier(Base):
    """A deployment tier — a configurable, non-ordered classification of environments (SPECS §2.4).

    Replaces the old fixed `dev < staging < prod` enum: tiers are data, environments reference one
    by `name`, and apply permission is set membership (`users.allowed_tiers`), not a linear ceiling.
    `requires_four_eyes` carries what used to be hardcoded on the `prod` tier.
    """

    __tablename__ = "tiers"

    id: Mapped[uuid.UUID] = pk_uuid()
    name: Mapped[str] = mapped_column(String, unique=True)
    requires_four_eyes: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)  # display order only, not a rank
    created_at: Mapped[datetime] = created_at_col()
