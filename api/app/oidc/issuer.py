from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.enums import AuditActorKind, JobPhase, OidcKeyStatus
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.oidc import OidcSigningKey
from app.models.run import Run
from app.models.stack import Stack

AUDIENCE = "sts.amazonaws.com"
# Max signing token TTL ≈ phase timeout cap (§4.2 prepare/plan/apply); kept short (§13).
PHASE_MAX_SECONDS = {JobPhase.plan: 30 * 60, JobPhase.apply: 60 * 60}
CLOCK_SKEW_SECONDS = 5 * 60  # how long past `exp` a verifier may still accept a token
KMS_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"  # RS256


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_uint(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _b64u_json(obj: dict) -> str:
    return _b64u(json.dumps(obj, separators=(",", ":")).encode())


def _kid(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()[:16]


def _rsa_jwk(public: rsa.RSAPublicKey, kid: str) -> dict:
    numbers = public.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u_uint(numbers.n),
        "e": _b64u_uint(numbers.e),
    }


def _new_local_key() -> OidcSigningKey:
    """A fresh RSA key whose private half is AES-GCM encrypted at rest (§1)."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = _kid(der)
    return OidcSigningKey(
        kid=kid,
        algorithm="RS256",
        public_jwk=_rsa_jwk(private.public_key(), kid),
        private_key_encrypted=encrypt(pem),
    )


def _kms_client(arn: str):  # type: ignore[no-untyped-def]
    # arn:aws:kms:<region>:<account>:key/<id> — the key's own region, whatever AWS_REGION says.
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != "kms":
        raise ProblemException(422, "Invalid KMS key ARN", f"not a KMS key ARN: {arn!r}")
    return boto3.client("kms", region_name=parts[3])


async def _new_kms_key(arn: str) -> OidcSigningKey:
    """Register a KMS key: only its public half and its ARN are kept (§3.11). It must be an RSA
    signing key of at least 2048 bits that can sign RS256."""

    def _fetch() -> dict:
        return _kms_client(arn).get_public_key(KeyId=arn)

    try:
        pub = await asyncio.to_thread(_fetch)
    except ProblemException:
        raise
    except Exception as exc:
        raise ProblemException(422, "KMS key unusable", str(exc)) from exc
    if pub.get("KeyUsage") != "SIGN_VERIFY" or KMS_ALGORITHM not in pub.get(
        "SigningAlgorithms", []
    ):
        raise ProblemException(
            422, "KMS key unusable", f"{arn} must be a SIGN_VERIFY key supporting {KMS_ALGORITHM}"
        )
    der = pub["PublicKey"]
    public = serialization.load_der_public_key(der)
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
        raise ProblemException(422, "KMS key unusable", f"{arn} must be an RSA key ≥ 2048 bits")
    kid = _kid(der)
    return OidcSigningKey(
        kid=kid, algorithm="RS256", public_jwk=_rsa_jwk(public, kid), kms_key_arn=arn
    )


async def _active(session: AsyncSession) -> OidcSigningKey | None:
    return (
        await session.execute(
            select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
        )
    ).scalar_one_or_none()


async def rotate_key(
    session: AsyncSession,
    *,
    kms_key_arn: str | None = None,
    actor_kind: AuditActorKind = AuditActorKind.system,
    actor_id: uuid.UUID | None = None,
    actor_email: str | None = None,
) -> OidcSigningKey:
    """Make a new key the signing one (§3.11): a KMS key when `kms_key_arn` is given, else a local
    key. The previous one moves to `retiring` — still in the JWKS, so the tokens it signed keep
    verifying — and leaves it once the longest of them has expired (`retire_keys`). Audited in the
    same transaction; the caller commits."""
    new = await _new_kms_key(kms_key_arn) if kms_key_arn else _new_local_key()
    if (
        await session.execute(select(OidcSigningKey).where(OidcSigningKey.kid == new.kid))
    ).scalar_one_or_none() is not None:
        raise ProblemException(409, "Key already registered", f"kid {new.kid} is already known")
    old = await _active(session)
    if old is not None:
        old.status = OidcKeyStatus.retiring
        old.retiring_at = datetime.now(UTC)
        await session.flush()  # one active key at a time (unique index): demote first
    session.add(new)
    await session.flush()
    await record_audit(
        session,
        action="oidc_key.rotated",
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_email=actor_email,
        target_kind="oidc_key",
        target_id=new.id,
        context={
            "kid": new.kid,
            "backend": "kms" if new.kms_key_arn else "local",
            "kms_key_arn": new.kms_key_arn,
            "previous_kid": old.kid if old else None,
        },
    )
    return new


async def ensure_active_key(session: AsyncSession) -> OidcSigningKey:
    """The signing key, created on first use. With `STACKD_OIDC_SIGNER=kms`, an issuer still
    signing with a local key (or none) moves to the configured KMS key — a rotation, audited — so
    switching the setting is the whole migration. A KMS key already active is kept: rotating to
    another one is an explicit admin action (`POST /api/v1/oidc/keys/rotate`)."""
    settings = get_settings()
    key = await _active(session)
    wants_kms = settings.stackd_oidc_signer == "kms"
    if key is not None and (key.kms_key_arn is not None or not wants_kms):
        return key
    try:
        key = await rotate_key(
            session, kms_key_arn=settings.stackd_oidc_kms_key_arn if wants_kms else None
        )
        await session.commit()
    except IntegrityError:
        # Another replica created it first (one active key): use theirs.
        await session.rollback()
        key = await _active(session)
        assert key is not None
        return key
    await session.refresh(key)
    return key


def max_token_ttl_seconds() -> int:
    """The longest a workload token can live (§10.2: min(ttl, phase cap)), plus clock skew: how
    long a retiring key must stay in the JWKS."""
    caps = (PHASE_MAX_SECONDS[JobPhase.plan], get_settings().apply_reclaim_window_seconds)
    return max(caps) + CLOCK_SKEW_SECONDS


async def retire_keys(session: AsyncSession, now: datetime) -> int:
    """Scheduler task (§7.5, advisory-locked): a `retiring` key whose tokens have all expired
    leaves the JWKS (`retired`). Idempotent."""
    cutoff = now - timedelta(seconds=max_token_ttl_seconds())
    due = (
        (
            await session.execute(
                select(OidcSigningKey).where(
                    OidcSigningKey.status == OidcKeyStatus.retiring,
                    OidcSigningKey.retiring_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for key in due:
        key.status = OidcKeyStatus.retired
        key.retired_at = now
        await record_audit(
            session,
            action="oidc_key.retired",
            actor_kind=AuditActorKind.system,
            target_kind="oidc_key",
            target_id=key.id,
            context={"kid": key.kid},
        )
    if due:
        await session.commit()
    return len(due)


async def _sign(key: OidcSigningKey, payload: dict) -> str:
    """A compact RS256 JWS. A local key signs in process; a KMS key through `kms:Sign` — the
    private key never enters this process (§13)."""
    if key.kms_key_arn is None:
        assert key.private_key_encrypted is not None
        pem = decrypt(key.private_key_encrypted)
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": key.kid})
    header = {"alg": "RS256", "kid": key.kid, "typ": "JWT"}
    signing_input = f"{_b64u_json(header)}.{_b64u_json(payload)}".encode()
    arn = key.kms_key_arn

    def _kms_sign() -> bytes:
        resp = _kms_client(arn).sign(
            KeyId=arn, Message=signing_input, MessageType="RAW", SigningAlgorithm=KMS_ALGORITHM
        )
        return resp["Signature"]

    signature = await asyncio.to_thread(_kms_sign)
    return f"{signing_input.decode()}.{_b64u(signature)}"


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
    now = datetime.now(UTC)
    # The apply cap follows the configured apply budget + grace so cloud credentials never outlive
    # the apply they were minted for (§4.2/§13); plan keeps the static cap.
    if phase == JobPhase.apply:
        phase_cap = get_settings().apply_reclaim_window_seconds
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
    return await _sign(key, payload)
