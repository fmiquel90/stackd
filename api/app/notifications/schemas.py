from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.enums import AttachmentTarget, NotificationKind
from app.models.notification import NotificationTarget

# The only states that ever fire (transition() enqueues exactly these — see NOTIFY_STATES).
ALLOWED_STATES = {"unconfirmed", "finished", "failed"}
DEFAULT_STATES = ["unconfirmed", "failed"]


class NotificationCreate(BaseModel):
    name: str
    kind: NotificationKind
    url: str
    on_states: list[str] = DEFAULT_STATES
    enabled: bool = True

    @field_validator("on_states")
    @classmethod
    def _valid_states(cls, v: list[str]) -> list[str]:
        bad = set(v) - ALLOWED_STATES
        if bad:
            raise ValueError(f"unsupported states {sorted(bad)}; allowed: {sorted(ALLOWED_STATES)}")
        if not v:
            raise ValueError("on_states must list at least one state")
        return v


class NotificationUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    on_states: list[str] | None = None
    enabled: bool | None = None

    @field_validator("on_states")
    @classmethod
    def _valid_states(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return NotificationCreate._valid_states(v)


class NotificationOut(BaseModel):
    id: uuid.UUID
    target_kind: AttachmentTarget
    target_id: uuid.UUID
    name: str
    kind: NotificationKind
    url: str
    on_states: list[str]
    enabled: bool
    created_at: datetime

    @classmethod
    def of(cls, t: NotificationTarget) -> NotificationOut:
        return cls(
            id=t.id,
            target_kind=t.target_kind,
            target_id=t.target_id,
            name=t.name,
            kind=t.kind,
            url=t.url,
            on_states=t.on_states,
            enabled=t.enabled,
            created_at=t.created_at,
        )
