from __future__ import annotations

import base64
import json
import uuid
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.config import get_settings
from app.db import get_session
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.ids import uuid7
from app.models.environment import Environment
from app.models.state import StateLock, StateVersion
from app.spaces import guard_env
from app.statebackend.store import get_object, put_object, state_key
from app.statebackend.tokens import decode_state_token, mint_state_token

# Terraform HTTP backend protocol (SPECS §11.2). Auth: HTTP Basic, password = scoped state JWT.
tf_router = APIRouter(prefix="/state/v1", tags=["state-backend"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


def _token_claims(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        raise ProblemException(401, "Unauthorized", "Basic auth required.")
    try:
        _user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        return decode_state_token(password)
    except (ValueError, jwt.PyJWTError) as exc:
        raise ProblemException(401, "Unauthorized", "Invalid state token.") from exc


def _require_env(claims: dict, env_id: uuid.UUID) -> None:
    if claims.get("env_id") != str(env_id):
        raise ProblemException(403, "Forbidden", "Token not scoped to this environment.")


def _require_rw(claims: dict) -> None:
    if claims.get("scope") != "rw":
        raise ProblemException(403, "Read-only", "This run holds a read-only state token.")


async def _latest(session: AsyncSession, env_id: uuid.UUID) -> StateVersion | None:
    return (
        await session.execute(
            select(StateVersion)
            .where(StateVersion.environment_id == env_id, StateVersion.deleted_at.is_(None))
            .order_by(StateVersion.serial.desc(), StateVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@tf_router.get("/{env_id}")
async def get_state(env_id: uuid.UUID, request: Request, session: DbSession) -> Response:
    _require_env(_token_claims(request), env_id)
    latest = await _latest(session, env_id)
    if latest is None:
        return Response(status_code=404)
    data = await get_object(latest.s3_key)
    if data is None:
        return Response(status_code=404)
    return Response(content=data, media_type="application/json")


@tf_router.post("/{env_id}")
async def post_state(
    env_id: uuid.UUID, request: Request, session: DbSession, ID: str | None = None
) -> Response:
    claims = _token_claims(request)
    _require_env(claims, env_id)
    _require_rw(claims)

    lock = await session.get(StateLock, env_id)
    if lock is not None and ID and lock.lock_id != ID:
        raise ProblemException(423, "Locked", "Lock ID mismatch.")

    body = await request.body()
    doc = json.loads(body)
    new_serial = int(doc.get("serial", 0))
    latest = await _latest(session, env_id)
    # Only a *regressive* serial is refused — Terraform legitimately re-POSTs the same serial, so
    # there is intentionally NO unique (environment_id, serial) constraint. The state lock (one
    # holder per env) is what serializes writers.
    if latest is not None and new_serial < latest.serial:
        raise ProblemException(409, "Serial conflict", "Refusing a regressive state serial.")

    version_id = uuid7()
    key = state_key(str(env_id), str(version_id))
    # Insert the DB row first (flush, not commit), then upload: if S3 fails the row rolls back, so
    # we never leave a state_version pointing at a missing object.
    session.add(
        StateVersion(
            id=version_id,
            environment_id=env_id,
            serial=new_serial,
            lineage=doc.get("lineage"),
            size_bytes=len(body),
            s3_key=key,
            created_by_run_id=uuid.UUID(claims["run_id"]) if claims.get("run_id") else None,
        )
    )
    await session.flush()
    await put_object(key, body)
    await session.commit()
    return Response(status_code=200)


@tf_router.api_route("/{env_id}/lock", methods=["LOCK"])
async def lock_state(env_id: uuid.UUID, request: Request, session: DbSession) -> Response:
    claims = _token_claims(request)
    _require_env(claims, env_id)
    _require_rw(claims)
    info = json.loads(await request.body() or b"{}")

    existing = await session.get(StateLock, env_id)
    if existing is not None:
        return Response(
            content=json.dumps(existing.info or {}), status_code=423, media_type="application/json"
        )
    session.add(StateLock(environment_id=env_id, lock_id=info.get("ID", ""), info=info))
    await session.commit()
    return Response(status_code=200)


@tf_router.api_route("/{env_id}/lock", methods=["UNLOCK"])
async def unlock_state(env_id: uuid.UUID, request: Request, session: DbSession) -> Response:
    claims = _token_claims(request)
    _require_env(claims, env_id)
    _require_rw(claims)  # a read-only token (proposed runs) never locks, so it must not unlock
    info = json.loads(await request.body() or b"{}")
    existing = await session.get(StateLock, env_id)
    if existing is None:
        return Response(status_code=200)
    if info.get("ID") and existing.lock_id and info["ID"] != existing.lock_id:
        raise ProblemException(423, "Locked", "Lock ID mismatch.")
    await session.delete(existing)
    await session.commit()
    return Response(status_code=200)


# --- human-facing state management (SPECS §12) ---
human_router = APIRouter(prefix="/api/v1/environments", tags=["state"])


@human_router.get("/{env_id}/state/versions")
async def list_versions(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env)
    rows = (
        (
            await session.execute(
                select(StateVersion)
                .where(StateVersion.environment_id == env_id, StateVersion.deleted_at.is_(None))
                .order_by(StateVersion.serial.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(v.id),
            "serial": v.serial,
            "lineage": v.lineage,
            "size_bytes": v.size_bytes,
            "created_by_run_id": str(v.created_by_run_id) if v.created_by_run_id else None,
            "created_at": v.created_at.isoformat(),
        }
        for v in rows
    ]


_IMPORT_TTL_SECONDS = 30 * 60


@human_router.post(
    "/{env_id}/state/import-session", dependencies=[Depends(require_role(Role.admin))]
)
async def create_import_session(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Adopt an existing stack into Stackd-managed state (SPECS §11.4).

    Mints a short-lived, run-less `rw` state token and returns a ready-to-use `http` backend config
    so an operator can migrate their current remote state with one standard command:
    `tofu init -migrate-state -backend-config=...`. Terraform LOCKs, uploads the state through the
    backend (stored as a `state_version` with no originating run), then UNLOCKs. Admin-only.
    """
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    if not env.managed_state:
        raise ProblemException(
            409,
            "Managed state is off",
            "Enable managed_state on this environment before importing state.",
        )

    token = mint_state_token(environment_id=env_id, scope="rw", ttl_seconds=_IMPORT_TTL_SECONDS)
    # Public URL: the operator runs `tofu` from their machine/CI, not from inside the cluster.
    addr = f"{get_settings().stackd_public_url.rstrip('/')}/state/v1/{env_id}"
    latest = await _latest(session, env_id)

    await record_audit(
        session,
        action="state.import_session_created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
        context={"ttl_seconds": _IMPORT_TTL_SECONDS},
    )
    await session.commit()

    backend = {
        "address": addr,
        "lock_address": f"{addr}/lock",
        "unlock_address": f"{addr}/lock",
        "lock_method": "LOCK",
        "unlock_method": "UNLOCK",
        "username": "env",
        "password": token,
    }
    init_args = " ".join(f'-backend-config="{k}={v}"' for k, v in backend.items())
    return {
        "expires_in": _IMPORT_TTL_SECONDS,
        "current_serial": latest.serial if latest else None,
        "backend": {"type": "http", **backend},
        "instructions": [
            'Replace your stack\'s backend block with: terraform { backend "http" {} }',
            f"Run once to migrate your current state into Stackd:\n"
            f"  tofu init -migrate-state {init_args}",
            "After it succeeds, future runs use the managed backend automatically. The token "
            f"expires in {_IMPORT_TTL_SECONDS // 60} minutes.",
        ],
    }


@human_router.delete(
    "/{env_id}/state/lock", status_code=204, dependencies=[Depends(require_role(Role.admin))]
)
async def force_unlock(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    lock = await session.get(StateLock, env_id)
    if lock is not None:
        await session.delete(lock)
    await record_audit(
        session,
        action="state.force_unlocked",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
    )
    await session.commit()
    return Response(status_code=204)
