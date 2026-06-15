from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.provisioning import upsert_user
from app.auth.schemas import DevLoginIn, SessionOut, UserOut
from app.auth.sessions import start_session
from app.config import get_settings
from app.db import get_session
from app.enums import Role
from app.errors import ProblemException

# This module is physically removed from the production image build (DEV §3) — never
# reachable when STACKD_ENV=production. The runtime guard below is the second line of defense.
router = APIRouter(prefix="/api/v1/auth/dev", tags=["auth-dev"])

PERSONAS = {
    "admin": {
        "google_sub": "dev:admin",
        "email": "admin@dev.local",
        "display_name": "Admin (dev)",
        "role": Role.admin,
        "allowed_tiers": ["dev", "staging", "prod"],
        "can_destroy": True,
    },
    "alice": {
        "google_sub": "dev:alice",
        "email": "alice@dev.local",
        "display_name": "Alice (dev)",
        "role": Role.approver,
        "allowed_tiers": ["dev", "staging", "prod"],
        "can_destroy": False,
    },
    "bob": {
        "google_sub": "dev:bob",
        "email": "bob@dev.local",
        "display_name": "Bob (dev)",
        "role": Role.writer,
        "allowed_tiers": ["dev", "staging"],
        "can_destroy": False,
    },
}


@router.get("/personas")
async def list_personas() -> dict:
    return {
        "personas": [
            {"key": k, "email": v["email"], "role": v["role"]} for k, v in PERSONAS.items()
        ]
    }


@router.post("/login", response_model=SessionOut)
async def dev_login(
    body: DevLoginIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionOut:
    settings = get_settings()
    if not settings.stackd_dev_auth or settings.is_production:
        raise ProblemException(404, "Not found", None)
    persona = PERSONAS.get(body.persona)
    if persona is None:
        raise ProblemException(400, "Unknown persona", f"Valid: {', '.join(PERSONAS)}.")

    user = await upsert_user(
        session,
        google_sub=persona["google_sub"],
        email=persona["email"],
        display_name=persona["display_name"],
        role=persona["role"],
        allowed_tiers=persona["allowed_tiers"],
        can_destroy=persona["can_destroy"],
    )
    access_token = await start_session(session, user, response, request)
    await session.commit()
    return SessionOut(access_token=access_token, user=UserOut.model_validate(user))
