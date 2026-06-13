from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AttachmentTarget
from app.models.hook import Hook


async def platform_hooks(
    session: AsyncSession, *, stack_id: uuid.UUID, env_id: uuid.UUID
) -> dict[str, list[dict]]:
    """Platform hooks for a run, grouped by stage (SPECS §8.1).

    Order within a stage: stack hooks, then env hooks, each by position. The agent appends repo
    hooks (.stackd.yml) AFTER these — platform hooks are non-bypassable by a PR.
    """
    rows = (
        (
            await session.execute(
                select(Hook).where(
                    or_(
                        (Hook.target_kind == AttachmentTarget.stack) & (Hook.target_id == stack_id),
                        (Hook.target_kind == AttachmentTarget.environment)
                        & (Hook.target_id == env_id),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    # stack hooks before env hooks; stable by (source rank, position).
    def sort_key(h: Hook) -> tuple[int, int]:
        return (0 if h.target_kind == AttachmentTarget.stack else 1, h.position)

    grouped: dict[str, list[dict]] = {}
    for h in sorted(rows, key=sort_key):
        grouped.setdefault(h.stage.value, []).append(
            {
                "name": h.name,
                "command": h.command,
                "on_failure": h.on_failure.value,
                "source": "platform",
            }
        )
    return grouped
