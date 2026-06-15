from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import RunState, RunType, TriggeredBy


class TriggerRunIn(BaseModel):
    type: RunType = RunType.tracked
    with_downstream: bool = False  # start a cascade run group (§9.4)
    commit_sha: str | None = None
    # Break-glass overrides for down secret sources (§15.4): variable name → value. Requires apply.
    secret_overrides: dict[str, str] | None = None


class CommandTriggerIn(BaseModel):
    command: str  # one of the allowlisted subcommands (app.runs.commands.ALLOWED_COMMANDS)
    args: list[str] = []
    commit_sha: str | None = None


class PromoteIn(BaseModel):
    from_environment_id: uuid.UUID  # promote that env's currently-applied commit to this one


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    environment_id: uuid.UUID
    type: RunType
    state: RunState
    command: dict | None
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
    used_secret_fallback: bool
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
