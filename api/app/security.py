from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings
from app.models.user import User

ALGO = "HS256"


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(user: User) -> str:
    settings = get_settings()
    now = _now()
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.stackd_jwt_secret, algorithm=ALGO)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    claims = jwt.decode(token, settings.stackd_jwt_secret, algorithms=[ALGO])
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return claims


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(*, jti: uuid.UUID, family_id: uuid.UUID, user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = _now()
    payload = {
        "jti": str(jti),
        "fam": str(family_id),
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.refresh_token_ttl_seconds)).timestamp()),
        "typ": "refresh",
    }
    return jwt.encode(payload, settings.stackd_jwt_secret, algorithm=ALGO)


def decode_refresh_token(token: str) -> dict:
    settings = get_settings()
    claims = jwt.decode(token, settings.stackd_jwt_secret, algorithms=[ALGO])
    if claims.get("typ") != "refresh":
        raise jwt.InvalidTokenError("not a refresh token")
    return claims


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)
