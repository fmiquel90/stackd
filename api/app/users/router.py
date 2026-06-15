from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.auth.schemas import MentionableUser, UserOut, UserUpdate
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.user import User

router = APIRouter(prefix="/api/v1/users", tags=["users"])

# Each mutable field maps to its dedicated audit action (§6.2).
_AUDIT_ACTIONS = {
    "role": "user.role_changed",
    "allowed_tiers": "user.apply_tier_changed",
    "can_destroy": "user.destroy_permission_changed",
    "disabled": "user.disabled",
}


@router.get("", response_model=list[UserOut], dependencies=[Depends(require_role(Role.admin))])
async def list_users(session: Annotated[AsyncSession, Depends(get_session)]) -> list[User]:
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    return list(rows)


@router.get("/mentionable", response_model=list[MentionableUser])
async def list_mentionable(
    _: CurrentUser, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[User]:
    """A minimal directory (id/email/display_name) for @mention autocomplete in comments — readable
    by any authenticated user (collaboration), unlike the admin-only full user list."""
    rows = (
        (await session.execute(select(User).where(User.disabled.is_(False)).order_by(User.email)))
        .scalars()
        .all()
    )
    return list(rows)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    admin: Annotated[User, Depends(require_role(Role.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise ProblemException(404, "User not found", None)

    changes = body.model_dump(exclude_unset=True)
    # allowed_tiers carries no FK; validate every entry against the catalog so a typo (or a stale
    # name) can't be stored — symmetry with environments.tier validation (§2.4).
    if "allowed_tiers" in changes:
        from app.models.tier import Tier

        known = set((await session.execute(select(Tier.name))).scalars().all())
        unknown = sorted(set(changes["allowed_tiers"]) - known)
        if unknown:
            raise ProblemException(422, "Unknown tier", f"No such tier(s): {', '.join(unknown)}.")
        changes["allowed_tiers"] = sorted(set(changes["allowed_tiers"]))
    for field, value in changes.items():
        before = getattr(target, field)
        if before == value:
            continue
        setattr(target, field, value)
        await record_audit(
            session,
            action=_AUDIT_ACTIONS[field],
            actor_kind=AuditActorKind.user,
            actor_id=admin.id,
            actor_email=admin.email,
            target_kind="user",
            target_id=target.id,
            context={
                "field": field,
                "before": before.value if hasattr(before, "value") else before,
                "after": value.value if hasattr(value, "value") else value,
            },
        )
    await session.commit()
    await session.refresh(target)
    return target
