from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import SecretProvider
from app.models.secret_source import SecretSource


class SecretSourceCreate(BaseModel):
    name: str
    provider: SecretProvider
    config: dict = {}
    bootstrap_secret: str  # write-only: the provider machine credential (PAT / token)


class SecretSourceUpdate(BaseModel):
    # bootstrap_secret omitted → credential unchanged (write-only, like sensitive variables).
    name: str | None = None
    config: dict | None = None
    bootstrap_secret: str | None = None


class SecretSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    name: str
    provider: SecretProvider
    config: dict
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: SecretSource) -> SecretSourceOut:
        # The bootstrap credential is never serialized.
        return cls(
            id=s.id,
            space_id=s.space_id,
            name=s.name,
            provider=s.provider,
            config=s.config,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
