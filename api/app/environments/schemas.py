from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import Tier
from app.models.environment import Environment


class EnvironmentCreate(BaseModel):
    name: str
    tier: Tier
    branch: str
    autodeploy: bool = False
    protected: bool = False
    require_second_pair_of_eyes: bool = False
    managed_state: bool = True
    allow_mock_apply: bool = False
    labels: dict | None = None
    position: int = 0


class EnvironmentUpdate(BaseModel):
    name: str | None = None
    tier: Tier | None = None
    branch: str | None = None
    autodeploy: bool | None = None
    protected: bool | None = None
    require_second_pair_of_eyes: bool | None = None
    managed_state: bool | None = None
    allow_mock_apply: bool | None = None
    labels: dict | None = None
    position: int | None = None


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stack_id: uuid.UUID
    name: str
    tier: Tier
    branch: str
    autodeploy: bool
    protected: bool
    require_second_pair_of_eyes: bool
    managed_state: bool
    allow_mock_apply: bool
    head_sha: str | None
    commits_ahead: int | None
    affects_project_root: bool | None
    stale: bool
    locked: bool
    labels: dict | None
    position: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, e: Environment, *, last_applied_sha: str | None = None) -> EnvironmentOut:
        # stale = head known AND differs from last applied commit (§9.6).
        stale = bool(e.head_sha and last_applied_sha and e.head_sha != last_applied_sha)
        return cls(
            id=e.id,
            stack_id=e.stack_id,
            name=e.name,
            tier=e.tier,
            branch=e.branch,
            autodeploy=e.autodeploy,
            protected=e.protected,
            require_second_pair_of_eyes=e.require_second_pair_of_eyes,
            managed_state=e.managed_state,
            allow_mock_apply=e.allow_mock_apply,
            head_sha=e.head_sha,
            commits_ahead=e.commits_ahead,
            affects_project_root=e.affects_project_root,
            stale=stale,
            locked=e.locked,
            labels=e.labels,
            position=e.position,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
