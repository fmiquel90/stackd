"""API tokens of service accounts (SPECS §23): `stkd_<random>`, stored as a SHA-256 only."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.errors import ProblemException
from app.models.api_token import ApiToken
from app.models.user import User
from app.security import hash_token

TOKEN_PREFIX = "stkd_"
PREFIX_SHOWN = 12  # `stkd_` + 7 characters: enough to recognise a token, useless to replay it
# last_used_at is a hint for the operator (which token is idle, which one leaked), not an audit
# record: written at most once a minute per token, outside the request's transaction.
_LAST_USED_RESOLUTION = timedelta(minutes=1)


def is_api_token(raw: str) -> bool:
    return raw.startswith(TOKEN_PREFIX)


def new_token() -> tuple[str, str, str]:
    """(raw token — shown once, its hash, its display prefix)."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw), raw[:PREFIX_SHOWN]


async def authenticate(session: AsyncSession, raw: str) -> User:
    """The service account a token belongs to — or 401, the same answer whatever is wrong with it
    (unknown, revoked, expired, account disabled), so a probe learns nothing."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(ApiToken, User)
            .join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_hash == hash_token(raw))
        )
    ).first()
    if row is None:
        raise ProblemException(401, "Not authenticated", "Invalid or expired token.")
    token: ApiToken = row[0]
    user: User = row[1]
    if (
        token.revoked_at is not None
        or token.expires_at <= now
        or user.disabled
        or not user.is_service_account
    ):
        raise ProblemException(401, "Not authenticated", "Invalid or expired token.")
    if token.last_used_at is None or now - token.last_used_at >= _LAST_USED_RESOLUTION:
        async with SessionLocal() as side:
            await side.execute(
                update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=now)
            )
            await side.commit()
    return user
