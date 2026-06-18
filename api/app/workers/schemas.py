from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import WorkerStatus


class RegisterIn(BaseModel):
    name: str
    labels: dict | None = None
    version: str | None = None


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
    event: str  # phase_started | phase_finished | job_failed
    phase: str | None = None
    exit_code: int | None = None
    result: dict | None = None


class LogIn(BaseModel):
    phase: str
    section: str | None = None
    seq: int
    lines: list


class PoolCreate(BaseModel):
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
    last_heartbeat_at: datetime | None


class QueueEntry(BaseModel):
    run_id: uuid.UUID
    environment_id: uuid.UUID
    state: str
    worker_id: uuid.UUID | None
    blocking_reason: str | None  # active_run|env_locked|no_compatible_worker|apply_affinity_hold
