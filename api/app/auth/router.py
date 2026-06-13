from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import google
from app.auth.deps import CurrentUser
from app.auth.provisioning import upsert_user
from app.auth.schemas import SessionOut, UserOut
from app.auth.sessions import (
    CSRF_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    revoke_family_of,
    rotate_session,
    start_session,
)
from app.config import get_settings
from app.db import get_session
from app.enums import AuditActorKind
from app.errors import ProblemException
from app.models.user import User
from app.security import csrf_matches

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

OAUTH_COOKIE = "stackd_oauth"
OAUTH_PATH = "/api/v1/auth/google"
CSRF_HEADER = "x-csrf-token"


def _encode_oauth_state(state: str, nonce: str, verifier: str) -> str:
    settings = get_settings()
    return jwt.encode(
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
            "typ": "oauth",
        },
        settings.stackd_jwt_secret,
        algorithm="HS256",
    )


def _copy_set_cookies(src: Response, dst: Response) -> None:
    dst.raw_headers.extend((k, v) for k, v in src.raw_headers if k.lower() == b"set-cookie")


@router.get("/google/start")
async def google_start() -> RedirectResponse:
    url, state, nonce, verifier = google.build_authorization_url()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        OAUTH_COOKIE,
        _encode_oauth_state(state, nonce, verifier),
        max_age=600,
        httponly=True,
        secure=get_settings().is_production,
        samesite="lax",  # must survive the top-level redirect back from Google
        path=OAUTH_PATH,
    )
    return response


@router.get("/google/callback")
async def google_callback(
    state: str,
    code: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    settings = get_settings()
    raw = request.cookies.get(OAUTH_COOKIE)
    if not raw:
        raise ProblemException(400, "Invalid OAuth state", "Missing state cookie.")
    try:
        oauth = jwt.decode(raw, settings.stackd_jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ProblemException(400, "Invalid OAuth state", "Expired or tampered.") from exc
    if oauth.get("state") != state:
        raise ProblemException(400, "Invalid OAuth state", "State mismatch.")

    tokens = await google.exchange_code(code, oauth["verifier"])
    id_token = tokens.get("id_token")
    if not id_token:
        raise ProblemException(401, "Google auth failed", "No id_token returned.")
    claims = google.validate_id_token(id_token, oauth["nonce"])
    try:
        google.admit(claims)
    except ProblemException:
        await record_audit(
            session,
            action="auth.domain_denied",
            actor_kind=AuditActorKind.user,
            actor_email=claims.get("email"),
            context={"hd": claims.get("hd")},
            ip=request.client.host if request.client else None,
        )
        await session.commit()
        raise

    user = await upsert_user(
        session,
        google_sub=claims["sub"],
        email=claims["email"],
        display_name=claims.get("name"),
        avatar_url=claims.get("picture"),
    )
    # Session cookies are minted onto a throwaway response, then copied onto the redirect.
    # The access token is intentionally discarded here: the SPA fetches it via /refresh on boot.
    cookie_sink = Response()
    await start_session(session, user, cookie_sink, request)
    redirect = RedirectResponse(settings.stackd_public_url + "/", status_code=302)
    _copy_set_cookies(cookie_sink, redirect)
    redirect.delete_cookie(OAUTH_COOKIE, path=OAUTH_PATH)
    await session.commit()
    return redirect


def _require_csrf(request: Request) -> None:
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise ProblemException(403, "CSRF check failed", "Missing or mismatched CSRF token.")


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionOut:
    _require_csrf(request)
    presented = request.cookies.get(REFRESH_COOKIE)
    if not presented:
        raise ProblemException(401, "Not authenticated", "No refresh token.")
    access_token = await rotate_session(session, presented, response, request)
    await session.commit()
    claims = jwt.decode(access_token, get_settings().stackd_jwt_secret, algorithms=["HS256"])
    user = await session.get(User, uuid.UUID(claims["sub"]))
    return SessionOut(access_token=access_token, user=UserOut.model_validate(user))


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    _require_csrf(request)
    presented = request.cookies.get(REFRESH_COOKIE)
    if presented:
        await revoke_family_of(session, presented)
        await record_audit(session, action="auth.logout", actor_kind=AuditActorKind.user)
        await session.commit()
    clear_auth_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/me/onboarded", response_model=UserOut)
async def mark_onboarded(
    user: CurrentUser, session: Annotated[AsyncSession, Depends(get_session)]
) -> UserOut:
    """Mark the first-login walkthrough as seen (persisted server-side — no browser storage)."""
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(user)
    return UserOut.model_validate(user)
