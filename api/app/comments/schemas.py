from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.run_comment import RunComment


class Anchor(BaseModel):
    """Where a comment is pinned in the plan (SPECS §16.2). `plan_line` (v1) targets a range of the
    rendered, masked plan log; `resource` (v2) targets a Terraform address."""

    kind: Literal["plan_line", "resource"]
    # plan_line
    phase: str | None = None
    seq: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    snippet: str | None = Field(default=None, max_length=300)  # copied from the masked log line
    # resource
    address: str | None = None
    action: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Anchor:
        if self.kind == "plan_line":
            if self.phase is None or self.seq is None:
                raise ValueError("plan_line anchor requires `phase` and `seq`")
            self.address = self.action = None  # drop foreign (resource) fields
        if self.kind == "resource":
            if not self.address:
                raise ValueError("resource anchor requires `address`")
            self.phase = self.seq = self.line_start = self.line_end = self.snippet = None
        return self


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    anchor: Anchor | None = None
    parent_id: uuid.UUID | None = None


class CommentUpdate(BaseModel):
    body: str | None = Field(default=None, max_length=10_000)
    resolved: bool | None = None


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    parent_id: uuid.UUID | None
    author_user_id: uuid.UUID | None
    author_email: str | None
    body: str
    anchor: dict | None
    resolved: bool
    resolved_by_user_id: uuid.UUID | None
    created_at: datetime
    edited_at: datetime | None

    @classmethod
    def of(cls, c: RunComment) -> CommentOut:
        return cls(
            id=c.id,
            run_id=c.run_id,
            parent_id=c.parent_id,
            author_user_id=c.author_user_id,
            author_email=c.author_email,
            body=c.body,
            anchor=c.anchor,
            resolved=c.resolved_at is not None,
            resolved_by_user_id=c.resolved_by_user_id,
            created_at=c.created_at,
            edited_at=c.edited_at,
        )
