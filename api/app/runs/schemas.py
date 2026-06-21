from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.enums import RunState, RunType, TriggeredBy

# A git ref/SHA must start alphanumeric (so it can never be parsed as a git `-option`) and stay
# within a safe charset — blocks argument injection into the worker's git fetch/checkout.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


def _validate_commit_sha(v: str | None) -> str | None:
    if v is not None and not _SAFE_REF.match(v):
        raise ValueError("commit_sha contains invalid characters")
    return v


class TriggerRunIn(BaseModel):
    type: RunType = RunType.tracked
    with_downstream: bool = False  # start a cascade run group (§9.4)
    commit_sha: str | None = None
    # Break-glass overrides for down secret sources (§15.4): variable name → value. Requires apply.
    secret_overrides: dict[str, str] | None = None

    _check_sha = field_validator("commit_sha")(_validate_commit_sha)


class CommandTriggerIn(BaseModel):
    command: str  # one of the allowlisted subcommands (app.runs.commands.ALLOWED_COMMANDS)
    args: list[str] = []
    commit_sha: str | None = None

    _check_sha = field_validator("commit_sha")(_validate_commit_sha)


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
