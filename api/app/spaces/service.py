from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ROLE_RANK, Role
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.space import Space
from app.models.space_membership import SpaceMembership
from app.models.stack import Stack
from app.models.user import User


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
    if ROLE_RANK[membership.role] < ROLE_RANK[min_role]:
        raise ProblemException(403, "Forbidden", f"Requires space role {min_role.value} or higher.")
    return membership


async def users_with_space_access(
    session: AsyncSession, space_id: uuid.UUID
) -> list[tuple[User, SpaceMembership | None]]:
    """Bulk form of `require_space_access` (reader floor): every enabled user who reaches the
    space — a member of it, or an instance admin — paired with their membership (None for an
    admin without one), so a caller can evaluate effective permissions (`can_apply`) for the whole
    audience without a query per user. Keep the predicate in step with `require_space_access`."""
    rows = (
        await session.execute(
            select(User, SpaceMembership)
            .outerjoin(
                SpaceMembership,
                (SpaceMembership.user_id == User.id) & (SpaceMembership.space_id == space_id),
            )
            .where(
                User.disabled.is_(False),
                SpaceMembership.id.is_not(None) | (User.role == Role.admin),
            )
        )
    ).all()
    return [(u, m) for u, m in rows]


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


# Load-by-id variants of the guards above: fetch, 404 if missing, gate on space membership, return
# the entity. Routers use these instead of re-implementing the fetch-404-guard shell locally.


async def guard_stack_id(
    session: AsyncSession, user: User, stack_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Stack:
    stack = await session.get(Stack, stack_id)
    if stack is None:
        raise ProblemException(404, "Stack not found", None)
    await guard_stack(session, user, stack, min_role=min_role)
    return stack


async def guard_env_id(
    session: AsyncSession,
    user: User,
    env_id: uuid.UUID,
    *,
    min_role: Role = Role.reader,
    not_found: str = "Environment not found",
) -> Environment:
    """`not_found` titles the 404 so a route taking two env ids (e.g. promote's source) can tell
    the client which one is missing; the membership gate is identical either way."""
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, not_found, None)
    await guard_env(session, user, env, min_role=min_role)
    return env


async def guard_run_id(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise ProblemException(404, "Run not found", None)
    await guard_run(session, user, run, min_role=min_role)
    return run


async def resolve_target_space(
    session: AsyncSession, user: User, space_id: uuid.UUID | None, *, min_role: Role = Role.reader
) -> uuid.UUID:
    """Resolve the target space of a create request (explicit space_id or the bootstrap default,
    SPECS §3.0) and gate the caller's membership on it in one step. An explicit id that names no
    space is a 404 — checked after the gate, so a non-member still gets 403 and can't probe ids
    (an instance admin passes the gate for any id, and would otherwise hit the FK on INSERT)."""
    resolved = space_id or (await get_default_space(session)).id
    await require_space_access(session, user, resolved, min_role=min_role)
    if space_id is not None and await session.get(Space, space_id) is None:
        raise ProblemException(404, "Space not found", None)
    return resolved


def membership_from_defaults(
    space_id: uuid.UUID, user: User, *, role: Role | None = None
) -> SpaceMembership:
    """A membership mirroring the user's instance-level defaults (Phase F seeding rule): same tiers
    and destroy grant, role overridable (e.g. admin for a space's creator)."""
    return SpaceMembership(
        space_id=space_id,
        user_id=user.id,
        role=role or user.role,
        allowed_tiers=list(user.allowed_tiers or []),
        can_destroy=user.can_destroy,
    )
