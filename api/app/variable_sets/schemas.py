from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import AttachmentTarget


class VariableSetCreate(BaseModel):
    name: str
    description: str | None = None
    auto_attach: bool = False
    selector: dict | None = None


class VariableSetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    auto_attach: bool | None = None
    selector: dict | None = None


class VariableSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    name: str
    description: str | None
    auto_attach: bool
    selector: dict | None
    created_at: datetime
    updated_at: datetime


class AttachmentCreate(BaseModel):
    target_kind: AttachmentTarget
    target_id: uuid.UUID
    priority: int = 0


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variable_set_id: uuid.UUID
    target_kind: AttachmentTarget
    target_id: uuid.UUID
    priority: int
