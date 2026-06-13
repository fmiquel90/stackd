from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role, Tier
from app.models.user import User


async def upsert_user(
    session: AsyncSession,
    *,
    google_sub: str,
    email: str,
    display_name: str | None = None,
    avatar_url: str | None = None,
    role: Role | None = None,
    max_apply_tier: Tier | None = None,
    can_destroy: bool | None = None,
) -> User:
    """Upsert on the stable `google_sub` (SPECS §2.1 step 6).

    Bootstrap (§2.1): the very first user is `admin`, subsequent ones `reader`,
    unless an explicit role is provided (dev personas, §2.4).
    """
    user = (
        await session.execute(select(User).where(User.google_sub == google_sub))
    ).scalar_one_or_none()

    if user is None:
        if role is None:
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            role = Role.admin if count == 0 else Role.reader
        user = User(
            google_sub=google_sub,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            role=role,
            max_apply_tier=max_apply_tier,
            can_destroy=can_destroy if can_destroy is not None else False,
        )
        session.add(user)
        await session.flush()
        return user

    # Existing user: refresh profile fields; never silently downgrade role/permissions.
    user.email = email
    if display_name is not None:
        user.display_name = display_name
    if avatar_url is not None:
        user.avatar_url = avatar_url
    return user
