from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.space import Space
from app.models.space_membership import SpaceMembership
from app.models.stack import Stack
from app.models.user import User

_ROLE_RANK = {Role.reader: 0, Role.writer: 1, Role.approver: 2, Role.admin: 3}


async def get_default_space(session: AsyncSession) -> Space:
    """The bootstrap space (SPECS §3.0). Used as the fallback target when a create request omits an
    explicit space_id; access is still membership-gated (§2/§6, Phase F)."""
    space = (
        await session.execute(select(Space).where(Space.name == "default"))
    ).scalar_one_or_none()
    if space is None:
        raise ProblemException(500, "Not bootstrapped", "Default space missing — run the seed.")
    return space


async def get_membership(
    session: AsyncSession, user_id: uuid.UUID, space_id: uuid.UUID
) -> SpaceMembership | None:
    return (
        await session.execute(
            select(SpaceMembership).where(
                SpaceMembership.user_id == user_id, SpaceMembership.space_id == space_id
            )
        )
    ).scalar_one_or_none()


async def accessible_space_ids(session: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """The set of space ids the user may see. None means "all spaces" (instance admin) — callers
    skip the filter in that case (§2/§6, Phase F)."""
    if user.role == Role.admin:
        return None
    rows = (
        await session.execute(
            select(SpaceMembership.space_id).where(SpaceMembership.user_id == user.id)
        )
    ).scalars()
    return set(rows)


async def require_space_access(
    session: AsyncSession, user: User, space_id: uuid.UUID, *, min_role: Role = Role.reader
) -> SpaceMembership | None:
    """Gate a request on space membership (§2/§6, Phase F). An instance admin reaches every space;
    everyone else must hold a membership with at least `min_role`. Returns the membership (None for
    an instance admin with no explicit one), so the caller can derive effective permissions."""
    membership = await get_membership(session, user.id, space_id)
    if user.role == Role.admin:
        return membership
    if membership is None:
        raise ProblemException(403, "Forbidden", "You are not a member of this space.")
    if _ROLE_RANK[membership.role] < _ROLE_RANK[min_role]:
        raise ProblemException(403, "Forbidden", f"Requires space role {min_role.value} or higher.")
    return membership


async def guard_stack(
    session: AsyncSession, user: User, stack: Stack, *, min_role: Role = Role.reader
) -> SpaceMembership | None:
    return await require_space_access(session, user, stack.space_id, min_role=min_role)


async def guard_env(
    session: AsyncSession, user: User, env: Environment, *, min_role: Role = Role.reader
) -> SpaceMembership | None:
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    return await require_space_access(session, user, stack.space_id, min_role=min_role)


async def guard_run(
    session: AsyncSession, user: User, run: Run, *, min_role: Role = Role.reader
) -> SpaceMembership | None:
    env = await session.get(Environment, run.environment_id)
    assert env is not None
    return await guard_env(session, user, env, min_role=min_role)
