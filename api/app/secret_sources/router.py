from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.crypto import encrypt
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.secret_source import SecretSource
from app.models.space import Space
from app.secret_sources.schemas import SecretSourceCreate, SecretSourceOut, SecretSourceUpdate
from app.spaces import require_space_access

router = APIRouter(prefix="/api/v1/spaces", tags=["secret-sources"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Depends(require_role(Role.admin))


async def _get_source(
    session: AsyncSession, space_id: uuid.UUID, src_id: uuid.UUID
) -> SecretSource:
    src = await session.get(SecretSource, src_id)
    if src is None or src.space_id != space_id:
        raise ProblemException(404, "Secret source not found", None)
    return src


@router.get("/{space_id}/secret-sources", response_model=list[SecretSourceOut])
async def list_sources(
    space_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[SecretSourceOut]:
    await require_space_access(session, user, space_id)
    rows = (
        (
            await session.execute(
                select(SecretSource)
                .where(SecretSource.space_id == space_id)
                .order_by(SecretSource.name)
            )
        )
        .scalars()
        .all()
    )
    return [SecretSourceOut.of(s) for s in rows]


@router.post(
    "/{space_id}/secret-sources",
    response_model=SecretSourceOut,
    status_code=201,
    dependencies=[Admin],
)
async def create_source(
    space_id: uuid.UUID, body: SecretSourceCreate, user: CurrentUser, session: DbSession
) -> SecretSourceOut:
    if await session.get(Space, space_id) is None:
        raise ProblemException(404, "Space not found", None)
    src = SecretSource(
        space_id=space_id,
        name=body.name,
        provider=body.provider,
        config=body.config,
        bootstrap_secret_encrypted=encrypt(body.bootstrap_secret),
        created_by_user_id=user.id,
    )
    session.add(src)
    await record_audit(
        session,
        action="secret_source.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space_id,
        context={"name": body.name, "provider": body.provider.value},
    )
    await session.commit()
    await session.refresh(src)
    return SecretSourceOut.of(src)


@router.patch(
    "/{space_id}/secret-sources/{src_id}", response_model=SecretSourceOut, dependencies=[Admin]
)
async def update_source(
    space_id: uuid.UUID,
    src_id: uuid.UUID,
    body: SecretSourceUpdate,
    user: CurrentUser,
    session: DbSession,
) -> SecretSourceOut:
    src = await _get_source(session, space_id, src_id)
    if body.name is not None:
        src.name = body.name
    if body.config is not None:
        src.config = body.config
    rotated = body.bootstrap_secret is not None
    if rotated:
        src.bootstrap_secret_encrypted = encrypt(body.bootstrap_secret or "")
    await record_audit(
        session,
        action="secret_source.token_rotated" if rotated else "secret_source.updated",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space_id,
        context={"name": src.name},
    )
    await session.commit()
    await session.refresh(src)
    return SecretSourceOut.of(src)


@router.delete("/{space_id}/secret-sources/{src_id}", status_code=204, dependencies=[Admin])
async def delete_source(
    space_id: uuid.UUID, src_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    src = await _get_source(session, space_id, src_id)
    # ON DELETE RESTRICT on variables.secret_source_id surfaces as a 409 if the source is in use.
    from sqlalchemy.exc import IntegrityError

    name = src.name  # capture before delete — the instance is expired after the flush
    await session.delete(src)
    await record_audit(
        session,
        action="secret_source.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="space",
        target_id=space_id,
        context={"name": name},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ProblemException(
            409, "Secret source in use", "Variables still reference this source."
        ) from exc
