from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.tier import Tier

router = APIRouter(prefix="/api/v1/tiers", tags=["tiers"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Depends(require_role(Role.admin))


class TierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    requires_four_eyes: bool
    position: int
    created_at: datetime


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class TierCreate(BaseModel):
    name: str
    requires_four_eyes: bool = False
    position: int = 0

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        # The tier name lands verbatim in the OIDC workload `sub` (run:<tier>:<stack>:<phase>,
        # §10.3), so it must not contain the ':' delimiter or whitespace that could alias a policy.
        if not _NAME_RE.match(v):
            raise ValueError("tier name must match ^[a-z0-9][a-z0-9-]*$ (no ':' or whitespace)")
        return v


class TierUpdate(BaseModel):
    requires_four_eyes: bool | None = None
    position: int | None = None


# Reading the catalog is open (env-create forms + users admin need it); mutations are admin-only.
@router.get("", response_model=list[TierOut])
async def list_tiers(_: CurrentUser, session: DbSession) -> list[Tier]:
    rows = (await session.execute(select(Tier).order_by(Tier.position, Tier.name))).scalars().all()
    return list(rows)


@router.post("", response_model=TierOut, status_code=201, dependencies=[Admin])
async def create_tier(body: TierCreate, user: CurrentUser, session: DbSession) -> Tier:
    tier = Tier(name=body.name, requires_four_eyes=body.requires_four_eyes, position=body.position)
    session.add(tier)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(409, "Tier exists", f"'{body.name}' already exists.") from exc
    await record_audit(
        session,
        action="tier.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="tier",
        target_id=tier.id,
        context={"name": tier.name, "requires_four_eyes": tier.requires_four_eyes},
    )
    await session.commit()
    await session.refresh(tier)
    return tier


@router.patch("/{tier_id}", response_model=TierOut, dependencies=[Admin])
async def update_tier(
    tier_id: uuid.UUID, body: TierUpdate, user: CurrentUser, session: DbSession
) -> Tier:
    tier = await session.get(Tier, tier_id)
    if tier is None:
        raise ProblemException(404, "Tier not found", None)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(tier, field, value)
    await record_audit(
        session,
        action="tier.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="tier",
        target_id=tier.id,
        context={"name": tier.name, "fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(tier)
    return tier


@router.delete("/{tier_id}", status_code=204, dependencies=[Admin])
async def delete_tier(tier_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    tier = await session.get(Tier, tier_id)
    if tier is None:
        raise ProblemException(404, "Tier not found", None)
    in_use = (
        await session.execute(select(Environment.id).where(Environment.tier == tier.name).limit(1))
    ).scalar_one_or_none()
    if in_use is not None:
        raise ProblemException(
            409, "Tier in use", "Environments still reference this tier; reassign them first."
        )
    name = tier.name
    # Strip the name from every user's allowed_tiers so re-creating a tier with the same name later
    # can't silently resurrect a stale grant (the array carries no FK).
    await session.execute(
        text("UPDATE users SET allowed_tiers = array_remove(allowed_tiers, :name)").bindparams(
            name=name
        )
    )
    await session.delete(tier)
    await record_audit(
        session,
        action="tier.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="tier",
        target_id=tier_id,
        context={"name": name},
    )
    await session.commit()
