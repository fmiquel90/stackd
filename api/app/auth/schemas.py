from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import Role, Tier


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    avatar_url: str | None
    role: Role
    max_apply_tier: Tier | None
    can_destroy: bool
    disabled: bool
    onboarded: bool
    last_login_at: datetime | None


class SessionOut(BaseModel):
    access_token: str
    user: UserOut


class UserUpdate(BaseModel):
    role: Role | None = None
    max_apply_tier: Tier | None = None
    can_destroy: bool | None = None
    disabled: bool | None = None


class DevLoginIn(BaseModel):
    persona: str  # admin | alice | bob
