from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import RunState, RunType, TriggeredBy


class TriggerRunIn(BaseModel):
    type: RunType = RunType.tracked
    with_downstream: bool = False  # start a cascade run group (§9.4)
    commit_sha: str | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    environment_id: uuid.UUID
    type: RunType
    state: RunState
    commit_sha: str | None
    commit_message: str | None
    commit_author: str | None
    triggered_by: TriggeredBy
    trigger_user_id: uuid.UUID | None
    confirmed_by_user_id: uuid.UUID | None
    worker_id: uuid.UUID | None
    plan_summary: dict | None
    check_results: dict | None
    used_mocks: bool
    variable_provenance: dict | None
    error: str | None
    claimed_at: datetime | None
    confirmed_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class LogChunkOut(BaseModel):
    phase: str
    section: str | None
    seq: int
    lines: list
