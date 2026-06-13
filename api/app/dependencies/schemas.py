from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.enums import TriggerPolicy


class OutputReferenceIn(BaseModel):
    output_name: str
    input_name: str
    mock_value: object | None = None


class DependencyCreate(BaseModel):
    upstream_env_id: uuid.UUID
    trigger_policy: TriggerPolicy = TriggerPolicy.on_output_change
    references: list[OutputReferenceIn] = []


class LinkByNameIn(BaseModel):
    upstream_stack_id: uuid.UUID
    trigger_policy: TriggerPolicy = TriggerPolicy.on_output_change
