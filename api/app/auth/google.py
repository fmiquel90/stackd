from __future__ import annotations

import base64
import hashlib
import secrets

import httpx
import jwt
from jwt import PyJWKClient

from app.config import get_settings
from app.errors import ProblemException

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
SCOPES = "openid email profile"

_jwks_client = PyJWKClient(JWKS_URI)


def _redirect_uri() -> str:
    return f"{get_settings().stackd_public_url}/api/v1/auth/google/callback"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_authorization_url() -> tuple[str, str, str, str]:
    """Returns (authorization_url, state, nonce, code_verifier)."""
    settings = get_settings()
    if not settings.google_client_id:
        raise ProblemException(503, "Google auth not configured", "GOOGLE_CLIENT_ID is unset.")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    if settings.allowed_domains:
        params["hd"] = next(iter(settings.allowed_domains))
    query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return f"{AUTH_ENDPOINT}?{query}", state, nonce, verifier


async def exchange_code(code: str, verifier: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    if resp.status_code != 200:
        raise ProblemException(401, "Google token exchange failed", resp.text)
    return resp.json()


def validate_id_token(id_token: str, expected_nonce: str) -> dict:
    settings = get_settings()
    signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.google_client_id,
        issuer=list(ISSUERS),
        options={"require": ["exp", "iat", "aud", "iss"]},
    )
    if claims.get("nonce") != expected_nonce:
        raise ProblemException(401, "Invalid id_token", "Nonce mismatch.")
    return claims


def admit(claims: dict) -> None:
    """Admission policy (SPECS §2.1 step 5): verified email + allowed `hd` domain."""
    settings = get_settings()
    if not claims.get("email_verified"):
        raise ProblemException(403, "Access denied", "Email not verified.")
    hd = claims.get("hd")
    if settings.allowed_domains and hd not in settings.allowed_domains:
        raise ProblemException(403, "Access denied", "Domain not allowed.")
