from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import RepoAuthKind, Tool
from app.models.stack import Stack


class StackCreate(BaseModel):
    name: str
    description: str | None = None
    repo_url: str
    repo_auth_kind: RepoAuthKind = RepoAuthKind.none
    repo_secret: str | None = None  # write-only: token / deploy key (encrypted at rest)
    project_root: str = "."
    tool: Tool = Tool.opentofu
    tool_version: str
    labels: dict | None = None


class StackUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    repo_url: str | None = None
    repo_auth_kind: RepoAuthKind | None = None
    repo_secret: str | None = None
    webhook_secret: str | None = None  # write-only HMAC secret (§3.1, §5)
    project_root: str | None = None
    tool: Tool | None = None
    tool_version: str | None = None
    labels: dict | None = None


class StackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    name: str
    description: str | None
    repo_url: str
    repo_auth_kind: RepoAuthKind
    has_repo_secret: bool
    has_webhook_secret: bool
    project_root: str
    tool: Tool
    tool_version: str
    labels: dict | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: Stack) -> StackOut:
        return cls(
            id=s.id,
            space_id=s.space_id,
            name=s.name,
            description=s.description,
            repo_url=s.repo_url,
            repo_auth_kind=s.repo_auth_kind,
            has_repo_secret=s.repo_secret_encrypted is not None,
            has_webhook_secret=s.webhook_secret_encrypted is not None,
            project_root=s.project_root,
            tool=s.tool,
            tool_version=s.tool_version,
            labels=s.labels,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class CheckRepoResult(BaseModel):
    ok: bool
    branches: list[str] = []
    detail: str | None = None
