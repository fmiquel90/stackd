from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.enums import JobPhase, OidcKeyStatus
from app.models.environment import Environment
from app.models.oidc import OidcSigningKey
from app.models.run import Run
from app.models.stack import Stack

AUDIENCE = "sts.amazonaws.com"
# Max signing token TTL ≈ phase timeout cap (§4.2 prepare/plan/apply); kept short (§13).
PHASE_MAX_SECONDS = {JobPhase.plan: 30 * 60, JobPhase.apply: 60 * 60}


def _b64u_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def ensure_active_key(session: AsyncSession) -> OidcSigningKey:
    key = (
        await session.execute(
            select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
        )
    ).scalar_one_or_none()
    if key is not None:
        return key

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    numbers = private.public_key().public_numbers()
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = hashlib.sha256(der).hexdigest()[:16]
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u_uint(numbers.n),
        "e": _b64u_uint(numbers.e),
    }
    key = OidcSigningKey(
        kid=kid, algorithm="RS256", public_jwk=jwk, private_key_encrypted=encrypt(pem)
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


async def jwks(session: AsyncSession) -> dict:
    keys = (
        (
            await session.execute(
                select(OidcSigningKey).where(
                    OidcSigningKey.status.in_([OidcKeyStatus.active, OidcKeyStatus.retiring])
                )
            )
        )
        .scalars()
        .all()
    )
    return {"keys": [k.public_jwk for k in keys]}


def openid_configuration() -> dict:
    issuer = get_settings().stackd_public_url.rstrip("/")
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/oidc/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "claims_supported": [
            "sub",
            "aud",
            "exp",
            "iat",
            "iss",
            "environment",
            "tier",
            "stack",
            "phase",
        ],
    }


async def sign_workload_token(
    session: AsyncSession, env: Environment, stack: Stack, run: Run, phase: JobPhase, *, ttl: int
) -> str:
    """Sign a per-run/per-phase workload token (SPECS §10.2). sub = run:<tier>:<stack>:<phase>."""
    key = await ensure_active_key(session)
    private_pem = decrypt(key.private_key_encrypted)
    now = datetime.now(UTC)
    # The apply cap follows the configured apply budget + grace so cloud credentials never outlive
    # the apply they were minted for (§4.2/§13); plan keeps the static cap.
    if phase == JobPhase.apply:
        s = get_settings()
        phase_cap = s.stackd_apply_timeout_seconds + s.stackd_apply_lost_grace_seconds
    else:
        phase_cap = PHASE_MAX_SECONDS[phase]
    exp = now + timedelta(seconds=min(ttl, phase_cap))
    payload = {
        "iss": get_settings().stackd_public_url.rstrip("/"),
        "sub": f"run:{env.tier}:{stack.name}:{phase.value}",
        "aud": AUDIENCE,
        "environment": env.name,
        "tier": env.tier,
        "stack": stack.name,
        "environment_id": str(env.id),
        "run_id": str(run.id),
        "phase": phase.value,
        "triggered_by": run.triggered_by.value,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": key.kid})
