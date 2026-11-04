from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import botocore.exceptions as boto_exc  # type: ignore[import-untyped]
import jwt
import sqlalchemy.exc as sa_exc
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
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
# Transaction-level advisory lock serializing key management (rotation, first-use provisioning)
# across sessions and replicas. Next to the scheduler's task keys (74001…74006, §7.5).
_LOCK_KEY_MANAGEMENT = 74007


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_uint(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _b64u_json(obj: dict[str, Any]) -> str:
    return _b64u(json.dumps(obj, separators=(",", ":")).encode())


class WorkloadSigningError(RuntimeError):
    """A workload token that cannot be signed for good (key disabled or deleted, access denied, an
    unusable key, a misconfiguration): the job is refused and its run failed closed — never handed
    out without its credentials (§10). `code` is a stable, generic marker (what `run.error` and the
    audit get); the provider's own message — ARNs, account ids — stays in the server log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"oidc_signing:{code}")


class WorkloadSigningUnavailable(RuntimeError):
    """A workload token that cannot be signed *right now* (KMS throttling or unreachable, a KMS 5xx,
    no database connection for key provisioning): the claim is rolled back — the run keeps its
    state — and the worker told to retry (§3.11)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"oidc_signing:{code}")


# AWS error codes, by what a retry can change.
_THROTTLED = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "LimitExceededException",
    }
)
_KMS_DOWN = frozenset(
    {
        "KMSInternalException",
        "DependencyTimeoutException",
        "KeyUnavailableException",
        "InternalFailure",
        "InternalError",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "ExpiredTokenException",  # the API's own (auto-refreshed) credentials, mid-rotation
    }
)
_ACCESS_DENIED = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "IncompleteSignature",
        "InvalidSignatureException",
    }
)
_KEY_DISABLED = frozenset({"DisabledException", "KMSInvalidStateException"})
_KEY_NOT_FOUND = frozenset({"NotFoundException", "InvalidArnException"})


def classify_signing_failure(exc: BaseException) -> tuple[bool, str]:
    """(transient, code) for a failure to sign a workload token (§3.11). Transient: what a later
    attempt can get past (throttling, KMS or the network down, a KMS 5xx, the database pool
    exhausted or a connection lost). Everything else — and anything unrecognized — is permanent:
    the run fails closed."""
    if isinstance(exc, ProblemException):
        code = str(exc.extra.get("code") or "signing_failed")
        # 409: another replica activated the key first; 503: no active key yet — both settle.
        return exc.status in (409, 503), code
    if isinstance(exc, boto_exc.ClientError):
        error = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        if error in _THROTTLED:
            return True, "kms_throttled"
        if error in _KMS_DOWN or status >= 500:
            return True, "kms_unavailable"
        if error in _ACCESS_DENIED:
            return False, "kms_access_denied"
        if error in _KEY_DISABLED:
            return False, "kms_key_disabled"
        if error in _KEY_NOT_FOUND:
            return False, "kms_key_not_found"
        return False, "kms_rejected"
    if isinstance(exc, (boto_exc.ConnectionError, boto_exc.HTTPClientError)):
        return True, "kms_unreachable"
    if isinstance(exc, boto_exc.NoCredentialsError):
        return False, "kms_no_credentials"
    if isinstance(exc, (sa_exc.TimeoutError, sa_exc.OperationalError, sa_exc.InterfaceError)):
        # sa_exc.TimeoutError: no connection free in the pool — key provisioning needs a second
        # one while the claim holds its own (`ensure_active_key`).
        return True, "db_unavailable"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True, "unreachable"
    return False, "signing_failed"


def _kid(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()[:16]


def _rsa_jwk(public: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
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


@functools.lru_cache(maxsize=16)
def _kms_client_for(region: str) -> Any:
    # One client per region: building one loads the service model and the credential chain, which
    # does not belong on the claim path (a signature per claimed job).
    return boto3.client("kms", region_name=region)


def _kms_client(arn: str) -> Any:
    # arn:aws:kms:<region>:<account>:key/<id> — the key's own region, whatever AWS_REGION says.
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != "kms":
        raise ProblemException(
            422, "Invalid KMS key ARN", f"not a KMS key ARN: {arn!r}", code="kms_invalid_arn"
        )
    return _kms_client_for(parts[3])


async def _new_kms_key(arn: str) -> OidcSigningKey:
    """Register a KMS key: only its public half and its ARN are kept (§3.11). It must be an RSA
    signing key of at least 2048 bits that can sign RS256."""

    def _fetch() -> dict[str, Any]:
        return dict(_kms_client(arn).get_public_key(KeyId=arn))

    try:
        pub = await asyncio.to_thread(_fetch)
    except ProblemException:
        raise
    except Exception as exc:
        transient, code = classify_signing_failure(exc)
        if transient:
            raise  # throttled, unreachable: not the key's fault — the caller retries
        raise ProblemException(422, "KMS key unusable", str(exc), code=code) from exc
    if pub.get("KeyUsage") != "SIGN_VERIFY" or KMS_ALGORITHM not in pub.get(
        "SigningAlgorithms", []
    ):
        raise ProblemException(
            422,
            "KMS key unusable",
            f"{arn} must be a SIGN_VERIFY key supporting {KMS_ALGORITHM}",
            code="kms_key_unusable",
        )
    der = pub["PublicKey"]
    public = serialization.load_der_public_key(der)
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
        raise ProblemException(
            422,
            "KMS key unusable",
            f"{arn} must be an RSA key ≥ 2048 bits",
            code="kms_key_unusable",
        )
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


async def _lock_key_management(session: AsyncSession) -> None:
    """Serialize key management until the session's transaction ends (re-entrant within it)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_LOCK_KEY_MANAGEMENT)
    )


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
    same transaction; the caller commits. Serialized with every other rotation (advisory lock held
    until the caller's commit)."""
    await _lock_key_management(session)
    new = await _new_kms_key(kms_key_arn) if kms_key_arn else _new_local_key()
    known = (
        await session.execute(select(OidcSigningKey).where(OidcSigningKey.kid == new.kid))
    ).scalar_one_or_none()
    if known is not None and known.status == OidcKeyStatus.active:
        raise ProblemException(
            409,
            "Key already active",
            f"kid {new.kid} already signs",
            code="oidc_key_already_active",
        )
    old = await _active(session)
    if old is not None:
        old.status = OidcKeyStatus.retiring
        old.retiring_at = datetime.now(UTC)
        await session.flush()  # one active key at a time (unique index): demote first
    context: dict[str, Any] = {"reactivated": known is not None}
    if known is not None:
        # A KMS key used before (switching back to it): it signs again — same kid, same JWKS entry.
        # Signed through the ARN asked for now: the same key material can sit behind another ARN
        # (a multi-region replica), and the one configured is the one the API may call.
        context["previous_status"] = known.status.value
        if new.kms_key_arn is not None and known.kms_key_arn != new.kms_key_arn:
            context["replaced_kms_key_arn"] = known.kms_key_arn
            known.kms_key_arn = new.kms_key_arn
        known.status = OidcKeyStatus.active
        known.retiring_at = None
        known.retired_at = None
        new = known
    else:
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
            **context,
        },
    )
    return new


def _needs_key(key: OidcSigningKey | None, wants_kms: bool) -> bool:
    return key is None or (wants_kms and key.kms_key_arn is None)


async def ensure_active_key(session: AsyncSession) -> OidcSigningKey:
    """The signing key, created on first use. With `STACKD_OIDC_SIGNER=kms`, an issuer still
    signing with a local key (or none) moves to the configured KMS key — a rotation, audited — so
    switching the setting is the whole migration. A KMS key already active is kept: rotating to
    another one is an explicit admin action (`POST /api/v1/oidc/keys/rotate`).

    Idempotent under concurrency: concurrent first uses (claims, JWKS fetches, replicas) provision
    once and all sign with the result."""
    settings = get_settings()
    wants_kms = settings.stackd_oidc_signer == "kms"
    key = await _active(session)
    if not _needs_key(key, wants_kms):
        assert key is not None
        return key
    # Provisioned in a session of its own, committed there: the caller's transaction (a job claim,
    # holding the run's transition) is never committed nor rolled back by key management. That
    # session needs a second pool connection while the caller holds one: with the pool exhausted,
    # acquiring it times out (sqlalchemy TimeoutError) — a transient failure, the claim is rolled
    # back and retried (`classify_signing_failure` → db_unavailable), never a failed run.
    from app.db import SessionLocal

    async with SessionLocal() as own:
        try:
            # Under the key-management lock, re-check: a concurrent first use (another claim, a
            # JWKS fetch, another replica) may have provisioned the key while we waited.
            await _lock_key_management(own)
            if _needs_key(await _active(own), wants_kms):
                await rotate_key(
                    own, kms_key_arn=settings.stackd_oidc_kms_key_arn if wants_kms else None
                )
            await own.commit()
        except IntegrityError:
            # One active key / one row per kid: someone made it first without the lock (a replica
            # of an older version during a rolling upgrade). Theirs signs.
            await own.rollback()
        except ProblemException as exc:
            if exc.extra.get("code") != "oidc_key_already_active":
                raise
            await own.rollback()  # the same: the configured key already signs
    key = (
        await session.execute(
            select(OidcSigningKey)
            .where(OidcSigningKey.status == OidcKeyStatus.active)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if key is None:
        raise ProblemException(
            503, "No signing key", "The OIDC issuer has no active key.", code="oidc_no_active_key"
        )
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


async def _sign(key: OidcSigningKey, payload: dict[str, Any]) -> str:
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
        return bytes(resp["Signature"])

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
