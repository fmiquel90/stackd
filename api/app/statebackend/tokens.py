from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

# Scoped state-backend token (SPECS §11.2): Basic-auth password, scope ro for proposed runs.


def mint_state_token(
    *, environment_id: uuid.UUID, run_id: uuid.UUID, scope: str, ttl_seconds: int
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "env_id": str(environment_id),
            "run_id": str(run_id),
            "scope": scope,  # "rw" | "ro"
            "typ": "state",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        },
        get_settings().stackd_jwt_secret,
        algorithm="HS256",
    )


def decode_state_token(token: str) -> dict:
    claims = jwt.decode(token, get_settings().stackd_jwt_secret, algorithms=["HS256"])
    if claims.get("typ") != "state":
        raise jwt.InvalidTokenError("not a state token")
    return claims
