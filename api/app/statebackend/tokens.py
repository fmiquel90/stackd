from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from app.config import get_settings

# Scoped state-backend token (SPECS §11.2): Basic-auth password. The scope is bound to the job at
# mint time (app.workers.claim) and enforced by the backend routes (app.statebackend.router):
#   "ro"   — GET only: a proposed run (drift included) or a read-only command. Never locks.
#   "plan" — GET + LOCK/UNLOCK, never POST: the plan job of a tracked/destroy run (`tofu plan` takes
#            the lock but never persists state). LOCK only during that run's plan phase.
#   "rw"   — GET + LOCK/UNLOCK + POST: a job holding apply credentials (an apply, a mutating
#            command) or a run-less import session (§11.4).
StateScope = Literal["ro", "plan", "rw"]


def mint_state_token(
    *,
    environment_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    scope: StateScope,
    ttl_seconds: int,
) -> str:
    # run_id is None for an out-of-run import session (admin-minted, §11.4).
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "env_id": str(environment_id),
            "run_id": str(run_id) if run_id else None,
            "scope": scope,
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
