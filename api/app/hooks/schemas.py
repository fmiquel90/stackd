from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.enums import HookOnFailure, HookStage
from app.models.hook import Hook


class HookCreate(BaseModel):
    stage: HookStage
    name: str
    command: str
    on_failure: HookOnFailure = HookOnFailure.fail
    position: int = 0


class HookUpdate(BaseModel):
    stage: HookStage | None = None
    name: str | None = None
    command: str | None = None
    on_failure: HookOnFailure | None = None
    position: int | None = None


class HookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    stage: HookStage
    name: str
    command: str
    on_failure: HookOnFailure
    position: int

    @classmethod
    def of(cls, h: Hook) -> HookOut:
        return cls(
            id=h.id,
            target_kind=h.target_kind.value,
            target_id=h.target_id,
            stage=h.stage,
            name=h.name,
            command=h.command,
            on_failure=h.on_failure,
            position=h.position,
        )
