from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import ProblemException
from app.models.worker import Worker, WorkerPool
from app.security import hash_token


def mint_worker_token(worker_id: uuid.UUID, pool_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(worker_id),
            "pool": str(pool_id),
            "typ": "worker",
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(seconds=get_settings().worker_token_ttl_seconds)).timestamp()
            ),
        },
        get_settings().stackd_jwt_secret,
        algorithm="HS256",
    )


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise ProblemException(401, "Not authenticated", "Missing bearer token.")
    return auth.removeprefix("Bearer ")


async def pool_from_token(request: Request, session: AsyncSession) -> WorkerPool:
    token = _bearer(request)
    pool = (
        await session.execute(select(WorkerPool).where(WorkerPool.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if pool is None:
        raise ProblemException(401, "Invalid pool token", None)
    return pool


async def get_current_worker(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Worker:
    try:
        claims = jwt.decode(
            _bearer(request),
            get_settings().stackd_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "typ"]},
        )
        if claims.get("typ") != "worker":
            raise jwt.InvalidTokenError("not a worker token")
    except jwt.PyJWTError as exc:
        raise ProblemException(401, "Not authenticated", "Invalid worker token.") from exc
    worker = await session.get(Worker, uuid.UUID(claims["sub"]))
    if worker is None:
        raise ProblemException(401, "Not authenticated", "Worker unregistered.")
    return worker


CurrentWorker = Annotated[Worker, Depends(get_current_worker)]
