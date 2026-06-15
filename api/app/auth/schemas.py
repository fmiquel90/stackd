from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    avatar_url: str | None
    role: Role
    allowed_tiers: list[str]
    can_destroy: bool
    disabled: bool
    onboarded: bool
    last_login_at: datetime | None


class SessionOut(BaseModel):
    access_token: str
    user: UserOut


class UserUpdate(BaseModel):
    role: Role | None = None
    allowed_tiers: list[str] | None = None
    can_destroy: bool | None = None
    disabled: bool | None = None


class DevLoginIn(BaseModel):
    persona: str  # admin | alice | bob
