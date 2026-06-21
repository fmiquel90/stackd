from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

import boto3
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.enums import AuditActorKind, CloudProvider, JobPhase, Role, TriggeredBy
from app.errors import ProblemException
from app.ids import uuid7
from app.models.environment import Environment
from app.models.oidc import CloudIntegration
from app.models.run import Run
from app.models.stack import Stack
from app.oidc.issuer import ensure_active_key, jwks, openid_configuration, sign_workload_token
from app.spaces import guard_env

issuer_router = APIRouter(tags=["oidc"])
cloud_router = APIRouter(prefix="/api/v1/environments", tags=["cloud-integration"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Admin = Depends(require_role(Role.admin))


@issuer_router.get("/.well-known/openid-configuration")
async def openid_config() -> dict:
    return openid_configuration()


@issuer_router.get("/oidc/jwks")
async def get_jwks(session: DbSession) -> dict:
    await ensure_active_key(session)  # the issuer always publishes a key (AWS fetches it)
    return await jwks(session)


class CloudIntegrationIn(BaseModel):
    provider: CloudProvider = CloudProvider.aws
    plan_role_arn: str
    apply_role_arn: str
    region: str | None = None
    session_duration: int = 3600


def _out(ci: CloudIntegration) -> dict:
    return {
        "id": str(ci.id),
        "provider": ci.provider.value,
        "plan_role_arn": ci.plan_role_arn,
        "apply_role_arn": ci.apply_role_arn,
        "region": ci.region,
        "session_duration": ci.session_duration,
    }


@cloud_router.get("/{env_id}/cloud-integration")
async def get_integration(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    from sqlalchemy import select

    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env)
    ci = (
        await session.execute(
            select(CloudIntegration).where(CloudIntegration.environment_id == env_id)
        )
    ).scalar_one_or_none()
    if ci is None:
        raise ProblemException(404, "No cloud integration", None)
    return _out(ci)


@cloud_router.put("/{env_id}/cloud-integration", dependencies=[Admin])
async def put_integration(
    env_id: uuid.UUID, body: CloudIntegrationIn, user: CurrentUser, session: DbSession
) -> dict:
    from sqlalchemy import select

    if await session.get(Environment, env_id) is None:
        raise ProblemException(404, "Environment not found", None)
    ci = (
        await session.execute(
            select(CloudIntegration).where(CloudIntegration.environment_id == env_id)
        )
    ).scalar_one_or_none()
    action = "cloud_integration.updated" if ci else "cloud_integration.created"
    if ci is None:
        ci = CloudIntegration(environment_id=env_id)
        session.add(ci)
    ci.provider = body.provider
    ci.plan_role_arn = body.plan_role_arn
    ci.apply_role_arn = body.apply_role_arn
    ci.region = body.region
    ci.session_duration = body.session_duration
    await record_audit(
        session,
        action=action,
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
    )
    await session.commit()
    await session.refresh(ci)
    return _out(ci)


@cloud_router.delete("/{env_id}/cloud-integration", status_code=204, dependencies=[Admin])
async def delete_integration(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    from sqlalchemy import select

    ci = (
        await session.execute(
            select(CloudIntegration).where(CloudIntegration.environment_id == env_id)
        )
    ).scalar_one_or_none()
    if ci is not None:
        await session.delete(ci)
    await record_audit(
        session,
        action="cloud_integration.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
    )
    await session.commit()


@cloud_router.post("/{env_id}/cloud-integration/test", dependencies=[Admin])
async def test_assume_role(env_id: uuid.UUID, _: CurrentUser, session: DbSession) -> dict:
    """Verify the plan role is assumable with a freshly signed workload token (§10.4)."""
    from sqlalchemy import select

    env = await session.get(Environment, env_id)
    ci = (
        await session.execute(
            select(CloudIntegration).where(CloudIntegration.environment_id == env_id)
        )
    ).scalar_one_or_none()
    if env is None or ci is None:
        raise ProblemException(404, "No cloud integration", None)
    stack = await session.get(Stack, env.stack_id)

    probe = Run(environment_id=env.id, triggered_by=TriggeredBy.manual)
    probe.id = uuid7()
    token = await sign_workload_token(session, env, stack, probe, JobPhase.plan, ttl=900)

    def _assume() -> dict:
        sts = boto3.client("sts", region_name=ci.region or "us-east-1")
        resp = sts.assume_role_with_web_identity(
            RoleArn=ci.plan_role_arn,
            RoleSessionName=f"stackd-test-{probe.id}",
            WebIdentityToken=token,
        )
        return {"assumed_role": resp["AssumedRoleUser"]["Arn"]}

    try:
        return await asyncio.to_thread(_assume)
    except Exception as exc:
        raise ProblemException(422, "AssumeRole failed", str(exc)) from exc
