"""Service accounts and their API tokens (SPECS §23) — instance admin."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import require_role
from app.db import get_session
from app.enums import AuditActorKind, Role, UserKind
from app.errors import ProblemException
from app.models.api_token import ApiToken
from app.models.user import User
from app.service_accounts.tokens import new_token

router = APIRouter(prefix="/api/v1/service-accounts", tags=["service-accounts"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Annotated[User, Depends(require_role(Role.admin))]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EMAIL_DOMAIN = "service-accounts.stackd.invalid"  # RFC 2606: never a deliverable address
# The instance role a service account may hold: it plans and reads; space-level rights (up to a
# space's admin, e.g. for config export/import, §22) come from its memberships (§2.4).
SERVICE_ROLES = {Role.reader, Role.writer}
MAX_TOKEN_DAYS = 365


class ServiceAccountIn(BaseModel):
    name: str
    role: Role = Role.reader

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must match ^[a-z0-9][a-z0-9-]{0,62}$")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: Role) -> Role:
        if v not in SERVICE_ROLES:
            raise ValueError("a service account's instance role is reader or writer")
        return v


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int = Field(default=90, ge=1, le=MAX_TOKEN_DAYS)


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, t: ApiToken) -> TokenOut:
        return cls(
            id=t.id,
            name=t.name,
            prefix=t.prefix,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            revoked_at=t.revoked_at,
            created_at=t.created_at,
        )


class TokenCreated(TokenOut):
    token: str  # shown once — only its hash is stored


class ServiceAccountOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: Role
    disabled: bool
    created_at: datetime
    active_tokens: int


async def _account(session: AsyncSession, account_id: uuid.UUID) -> User:
    user = await session.get(User, account_id)
    if user is None or not user.is_service_account:
        raise ProblemException(404, "Service account not found", None)
    return user


async def _active_tokens(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Active (unrevoked, unexpired) token count per account — one grouped query for a list."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(ApiToken.user_id, func.count())
        .where(
            ApiToken.user_id.in_(user_ids),
            ApiToken.revoked_at.is_(None),
            ApiToken.expires_at > datetime.now(UTC),
        )
        .group_by(ApiToken.user_id)
    )
    return {uid: n for uid, n in rows.all()}


def _out(u: User, active_tokens: int) -> ServiceAccountOut:
    return ServiceAccountOut(
        id=u.id,
        name=u.display_name or u.email.split("@")[0],
        email=u.email,
        role=u.role,
        disabled=u.disabled,
        created_at=u.created_at,
        active_tokens=active_tokens,
    )


def _actor(admin: User) -> dict:  # type: ignore[type-arg]
    return {"actor_kind": AuditActorKind.user, "actor_id": admin.id, "actor_email": admin.email}


@router.get("", response_model=list[ServiceAccountOut])
async def list_accounts(_: Admin, session: DbSession) -> list[ServiceAccountOut]:
    rows = (
        (
            await session.execute(
                select(User).where(User.kind == UserKind.service).order_by(User.email)
            )
        )
        .scalars()
        .all()
    )
    counts = await _active_tokens(session, [u.id for u in rows])
    return [_out(u, counts.get(u.id, 0)) for u in rows]


@router.post("", response_model=ServiceAccountOut, status_code=201)
async def create_account(
    body: ServiceAccountIn, admin: Admin, session: DbSession
) -> ServiceAccountOut:
    email = f"{body.name}@{EMAIL_DOMAIN}"
    exists = (await session.execute(select(User.id).where(User.email == email))).first()
    if exists is not None:
        raise ProblemException(409, "Service account exists", f"'{body.name}' already exists.")
    # No membership: unlike a new Google user (§2.4), a service account reaches no space until an
    # admin grants it one (PUT /spaces/{id}/members) — least privilege for a credential that lives
    # in a CI system.
    user = User(
        kind=UserKind.service,
        google_sub=None,
        email=email,
        display_name=body.name,
        role=body.role,
        allowed_tiers=[],
        can_destroy=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # a concurrent create of the same name (unique index, 0036)
        raise ProblemException(
            409, "Service account exists", f"'{body.name}' already exists."
        ) from exc
    await record_audit(
        session,
        action="service_account.created",
        **_actor(admin),
        target_kind="user",
        target_id=user.id,
        context={"name": body.name, "role": body.role.value},
    )
    await session.commit()
    await session.refresh(user)
    return _out(user, 0)


@router.get("/{account_id}/tokens", response_model=list[TokenOut])
async def list_tokens(account_id: uuid.UUID, _: Admin, session: DbSession) -> list[TokenOut]:
    await _account(session, account_id)
    rows = (
        (
            await session.execute(
                select(ApiToken)
                .where(ApiToken.user_id == account_id)
                .order_by(ApiToken.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [TokenOut.of(t) for t in rows]


@router.post("/{account_id}/tokens", response_model=TokenCreated, status_code=201)
async def create_token(
    account_id: uuid.UUID, body: TokenIn, admin: Admin, session: DbSession
) -> TokenCreated:
    account = await _account(session, account_id)
    if account.disabled:
        raise ProblemException(409, "Service account disabled", "Enable it first.")
    raw, digest, prefix = new_token()
    token = ApiToken(
        user_id=account.id,
        name=body.name,
        token_hash=digest,
        prefix=prefix,
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_in_days),
        created_by_user_id=admin.id,
    )
    session.add(token)
    await session.flush()
    await record_audit(
        session,
        action="api_token.created",
        **_actor(admin),
        target_kind="user",
        target_id=account.id,
        # Never the token: its id and display prefix identify it.
        context={
            "token_id": str(token.id),
            "name": body.name,
            "prefix": prefix,
            "expires_at": token.expires_at.isoformat(),
        },
    )
    await session.commit()
    await session.refresh(token)
    return TokenCreated(**TokenOut.of(token).model_dump(), token=raw)


@router.delete("/{account_id}/tokens/{token_id}", status_code=204)
async def revoke_token(
    account_id: uuid.UUID, token_id: uuid.UUID, admin: Admin, session: DbSession
) -> None:
    await _account(session, account_id)
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != account_id:
        raise ProblemException(404, "Token not found", None)
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await record_audit(
            session,
            action="api_token.revoked",
            **_actor(admin),
            target_kind="user",
            target_id=account_id,
            context={"token_id": str(token.id), "name": token.name, "prefix": token.prefix},
        )
        await session.commit()
