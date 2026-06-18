from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.space import Space
from app.models.space_membership import SpaceMembership
from app.models.user import User
from app.spaces.service import accessible_space_ids, get_membership, require_space_access

router = APIRouter(prefix="/api/v1/spaces", tags=["spaces"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
InstanceAdmin = Depends(require_role(Role.admin))


class SpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class SpaceCreate(BaseModel):
    name: str
    description: str | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: Role
    allowed_tiers: list[str]
    can_destroy: bool


class MemberUpsert(BaseModel):
    user_id: uuid.UUID
    role: Role = Role.reader
    allowed_tiers: list[str] = []
    can_destroy: bool = False


@router.get("", response_model=list[SpaceOut])
async def list_spaces(user: CurrentUser, session: DbSession) -> list[Space]:
    """Spaces the caller can reach (member of, or all for an instance admin)."""
    ids = await accessible_space_ids(session, user)
    q = select(Space).order_by(Space.name)
    if ids is not None:
        q = q.where(Space.id.in_(ids))
    return list((await session.execute(q)).scalars().all())


@router.post("", response_model=SpaceOut, status_code=201, dependencies=[InstanceAdmin])
async def create_space(body: SpaceCreate, user: CurrentUser, session: DbSession) -> Space:
    space = Space(name=body.name, description=body.description)
    session.add(space)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ProblemException(409, "Space exists", f"A space named '{body.name}' exists.") from exc
    # The creator becomes a space admin so the space isn't immediately unreachable.
    session.add(
        SpaceMembership(
            space_id=space.id,
            user_id=user.id,
            role=Role.admin,
            allowed_tiers=list(user.allowed_tiers or []),
            can_destroy=user.can_destroy,
        )
    )
    await record_audit(
        session,
        action="space.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space.id,
        context={"name": space.name},
    )
    await session.commit()
    await session.refresh(space)
    return space


@router.get("/{space_id}/members", response_model=list[MemberOut])
async def list_members(
    space_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[MemberOut]:
    await require_space_access(session, user, space_id, min_role=Role.admin)
    rows = (
        await session.execute(
            select(SpaceMembership, User)
            .join(User, User.id == SpaceMembership.user_id)
            .where(SpaceMembership.space_id == space_id)
            .order_by(User.email)
        )
    ).all()
    return [
        MemberOut(
            user_id=m.user_id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            allowed_tiers=m.allowed_tiers,
            can_destroy=m.can_destroy,
        )
        for m, u in rows
    ]


@router.put("/{space_id}/members", response_model=MemberOut)
async def upsert_member(
    space_id: uuid.UUID, body: MemberUpsert, user: CurrentUser, session: DbSession
) -> MemberOut:
    """Grant or update a member's space role/tier ceiling (space admin or instance admin)."""
    await require_space_access(session, user, space_id, min_role=Role.admin)
    target = await session.get(User, body.user_id)
    if target is None:
        raise ProblemException(404, "User not found", None)
    membership = await get_membership(session, body.user_id, space_id)
    if membership is None:
        membership = SpaceMembership(space_id=space_id, user_id=body.user_id)
        session.add(membership)
    membership.role = body.role
    membership.allowed_tiers = body.allowed_tiers
    membership.can_destroy = body.can_destroy
    await record_audit(
        session,
        action="space.member_set",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space_id,
        context={"user_id": str(body.user_id), "role": body.role.value},
    )
    await session.commit()
    return MemberOut(
        user_id=body.user_id,
        email=target.email,
        display_name=target.display_name,
        role=membership.role,
        allowed_tiers=membership.allowed_tiers,
        can_destroy=membership.can_destroy,
    )


@router.delete("/{space_id}/members/{user_id}", status_code=204)
async def remove_member(
    space_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    await require_space_access(session, user, space_id, min_role=Role.admin)
    membership = await get_membership(session, user_id, space_id)
    if membership is not None:
        await session.delete(membership)
        await record_audit(
            session,
            action="space.member_removed",
            actor_kind=AuditActorKind.user,
            actor_id=user.id,
            actor_email=user.email,
            target_kind="space",
            target_id=space_id,
            context={"user_id": str(user_id)},
        )
        await session.commit()
