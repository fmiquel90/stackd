from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ProblemException
from app.models.space import Space


async def get_default_space(session: AsyncSession) -> Space:
    """The MVP single space (SPECS §3.0). Multi-space CRUD is deferred to Phase 7."""
    space = (
        await session.execute(select(Space).where(Space.name == "default"))
    ).scalar_one_or_none()
    if space is None:
        raise ProblemException(500, "Not bootstrapped", "Default space missing — run the seed.")
    return space
