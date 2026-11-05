from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.enums import WorkerStatus
from app.runs.logs_archive import valid_phase


class RegisterIn(BaseModel):
    name: str
    labels: dict | None = None
    version: str | None = None
    # The execution runner (§7.4). Absent from an agent that predates it: a production API treats
    # that as "not docker" and refuses the worker (fail closed).
    runner: Literal["local", "docker"] | None = None
    # The tool versions it can run (§7.4), e.g. {"opentofu": ["1.12.2"]}: the claim hands it only
    # runs whose stack's tool_version it has.
    tools: dict[str, list[str]] | None = None


class RegisterOut(BaseModel):
    worker_id: uuid.UUID
    worker_token: str


class HeartbeatIn(BaseModel):
    # Reported by the worker (§7, Phase E). in_flight drives busy/idle; capacity is advertised for
    # the scheduler to reason about (not persisted this phase — no schema change).
    in_flight: int | None = None
    capacity: int | None = None


class HeartbeatOut(BaseModel):
    commands: list[dict] = []


class CommandResultIn(BaseModel):
    status: str = "done"  # done | failed
    result: dict | None = None


class EventIn(BaseModel):
    event: str  # phase_started | phase_finished | job_failed | job_canceled
    phase: str | None = None
    exit_code: int | None = None
    result: dict | None = None


class LogIn(BaseModel):
    phase: str
    section: str | None = None
    seq: int
    lines: list

    @field_validator("phase")
    @classmethod
    def _phase_is_a_key_segment(cls, v: str) -> str:
        # The phase names the chunk's archive object (logs/{run_id}/{phase}.log.gz, §5.2): refused
        # here (422) rather than stored and then unreadable, or a path, once archived.
        if not valid_phase(v):
            raise ValueError("phase must match [A-Za-z0-9_.-]{1,64}")
        return v


class PoolCreate(BaseModel):
    space_id: uuid.UUID | None = None  # target space (§6); defaults to the bootstrap space
    name: str
    labels: dict | None = None


class PoolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    labels: dict | None
    created_at: datetime


class PoolCreated(PoolOut):
    token: str  # returned once at creation


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pool_id: uuid.UUID
    name: str
    status: WorkerStatus
    labels: dict | None
    version: str | None
    runner: str | None
    tools: dict | None
    last_heartbeat_at: datetime | None


class QueueEntry(BaseModel):
    run_id: uuid.UUID
    environment_id: uuid.UUID
    state: str
    worker_id: uuid.UUID | None
    blocking_reason: str | None  # active_run|env_locked|no_compatible_worker|apply_affinity_hold
