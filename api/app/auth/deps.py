from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.enums import Role
from app.errors import ProblemException
from app.models.user import User
from app.security import decode_access_token

_ROLE_RANK = {Role.reader: 0, Role.writer: 1, Role.approver: 2, Role.admin: 3}


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise ProblemException(401, "Not authenticated", "Missing bearer token.")
    try:
        claims = decode_access_token(auth.removeprefix("Bearer "))
    except jwt.PyJWTError as exc:
        raise ProblemException(401, "Not authenticated", "Invalid or expired token.") from exc

    user = await session.get(User, uuid.UUID(claims["sub"]))
    if user is None or user.disabled:
        raise ProblemException(401, "Not authenticated", "Account unavailable.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role) -> Callable[[User], User]:
    def _dep(user: CurrentUser) -> User:
        if _ROLE_RANK[user.role] < _ROLE_RANK[minimum]:
            raise ProblemException(403, "Forbidden", f"Requires role {minimum.value} or higher.")
        return user

    return _dep
