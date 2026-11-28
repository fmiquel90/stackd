from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.state import StateLock


def _text(info: dict | None, key: str) -> str | None:
    value = (info or {}).get(key)
    return value if isinstance(value, str) and value else None


class StateLockOut(BaseModel):
    """The held state lock (SPECS §11.2): its holder run and the fields of tofu's lock info."""

    lock_id: str
    run_id: uuid.UUID | None
    locked_at: datetime
    operation: str | None
    who: str | None
    version: str | None
    created: str | None
    info: str | None

    @classmethod
    def of(cls, lock: StateLock) -> StateLockOut:
        return cls(
            lock_id=lock.lock_id,
            run_id=lock.run_id,
            locked_at=lock.locked_at,
            operation=_text(lock.info, "Operation"),
            who=_text(lock.info, "Who"),
            version=_text(lock.info, "Version"),
            created=_text(lock.info, "Created"),
            info=_text(lock.info, "Info"),
        )
