from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.enums import AuditActorKind
from app.errors import ProblemException
from app.ids import uuid7
from app.logging import get_logger
from app.models.user import RefreshToken, User
from app.security import (
    create_access_token,
    decode_refresh_token,
    hash_token,
    issue_refresh_token,
    new_csrf_token,
)

_log = get_logger("stackd.auth")

REFRESH_COOKIE = "stackd_refresh"
CSRF_COOKIE = "stackd_csrf"
COOKIE_PATH = "/api/v1/auth"


def _cookie_secure() -> bool:
    return get_settings().is_production


def set_auth_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.refresh_token_ttl_seconds
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path=COOKIE_PATH,
    )
    # Double-submit CSRF token: readable by JS so the SPA can echo it in a header (§2.1).
    # Path "/" (not the auth path) so the SPA running at "/" can read it on boot to call /refresh.
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE, path="/")


async def start_session(
    session: AsyncSession, user: User, response: Response, request: Request
) -> str:
    """Open a fresh refresh-token family and set cookies. Returns the access token."""
    settings = get_settings()
    jti = uuid7()
    family_id = uuid7()
    refresh = issue_refresh_token(jti=jti, family_id=family_id, user_id=user.id)
    session.add(
        RefreshToken(
            id=jti,
            user_id=user.id,
            family_id=family_id,
            parent_id=None,
            token_hash=hash_token(refresh),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    user.last_login_at = datetime.now(UTC)
    await record_audit(
        session,
        action="auth.login",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    csrf = new_csrf_token()
    set_auth_cookies(response, refresh, csrf)
    return create_access_token(user)


async def rotate_session(
    session: AsyncSession, presented: str, response: Response, request: Request
) -> str:
    """Rotate a refresh token with reuse detection (SPECS §2.5). Returns a new access token."""
    settings = get_settings()
    invalid = ProblemException(401, "Invalid refresh token", "Please sign in again.")

    try:
        claims = decode_refresh_token(presented)
    except jwt.PyJWTError as exc:
        raise invalid from exc

    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(presented))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise invalid
    if row.expires_at <= datetime.now(UTC):
        raise invalid

    if row.used_at is not None:
        # Reuse of an already-rotated token → revoke the whole family.
        await session.execute(
            RefreshToken.__table__.update()
            .where(RefreshToken.family_id == row.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
        await record_audit(
            session,
            action="auth.refresh_reuse_detected",
            actor_kind=AuditActorKind.user,
            actor_id=row.user_id,
            context={"family_id": str(row.family_id)},
            ip=request.client.host if request.client else None,
        )
        await session.commit()
        clear_auth_cookies(response)
        _log.warning(
            "refresh token reuse detected",
            extra={
                "event": "auth.refresh_reuse",
                "user_id": str(row.user_id),
                "family_id": str(row.family_id),
            },
        )
        raise invalid

    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or user.disabled:
        raise invalid

    row.used_at = datetime.now(UTC)
    new_jti = uuid7()
    new_refresh = issue_refresh_token(jti=new_jti, family_id=row.family_id, user_id=user.id)
    session.add(
        RefreshToken(
            id=new_jti,
            user_id=user.id,
            family_id=row.family_id,
            parent_id=row.id,
            token_hash=hash_token(new_refresh),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    csrf = new_csrf_token()
    set_auth_cookies(response, new_refresh, csrf)
    _ = claims  # decoded purely to validate signature/typ
    return create_access_token(user)


async def revoke_family_of(session: AsyncSession, presented: str) -> None:
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(presented))
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.execute(
            RefreshToken.__table__.update()
            .where(RefreshToken.family_id == row.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
