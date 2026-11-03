"""Environment clones (SPECS §21.2): one-shot creation of an environment in N stacks of a space,
modelled on the environment each stack already has under `reference_name` — its settings, its
governance and its dependency edges. All or nothing, in one transaction; every created item audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser
from app.db import get_session
from app.dependencies.router import _create_edge
from app.enums import AttachmentTarget, AuditActorKind, Role, SecretFallback
from app.errors import ProblemException
from app.models.dependency import EnvDependency, OutputReference
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.notification import NotificationTarget
from app.models.oidc import CloudIntegration
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.user import User
from app.models.variable import Variable
from app.models.variable_set import VariableSetAttachment
from app.spaces import require_space_access

router = APIRouter(prefix="/api/v1", tags=["environment-clones"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


class CloneOverrides(BaseModel):
    tier: str | None = None
    branch: str | None = None
    labels: dict[str, Any] | None = None  # merged onto the reference's labels, these win per key
    autodeploy: bool | None = None
    protected: bool | None = None
    require_second_pair_of_eyes: bool | None = None
    drift_check_enabled: bool | None = None


class CloneIn(BaseModel):
    reference_name: str
    name: str = Field(min_length=1)
    stack_ids: list[uuid.UUID] | None = None
    overrides: CloneOverrides = CloneOverrides()
    copy_cloud_integration: bool = False
    # Preview (DESIGN §5.11): everything is computed and validated, then rolled back.
    dry_run: bool = False


# Settings a clone copies from its reference (§21.2); `allow_mock_apply` / `allow_fallback_apply`
# and the unmanaged backend settings are deliberately absent.
_COPIED = (
    "tier",
    "branch",
    "autodeploy",
    "protected",
    "require_second_pair_of_eyes",
    "managed_state",
    "drift_check_enabled",
    "position",
)


def _actor(user: User) -> dict[str, Any]:
    return {"actor_kind": AuditActorKind.user, "actor_id": user.id, "actor_email": user.email}


async def _targets(
    session: AsyncSession, space_id: uuid.UUID, body: CloneIn
) -> list[tuple[Stack, Environment]]:
    """(stack, reference env) per target stack, validated: the stacks (default: every stack of the
    space with a reference env) must all have the reference and none the new name (422)."""
    stacks = (
        (
            await session.execute(
                select(Stack).where(Stack.space_id == space_id).order_by(Stack.name)
            )
        )
        .scalars()
        .all()
    )
    if body.stack_ids is not None:
        wanted = set(body.stack_ids)
        stacks = [s for s in stacks if s.id in wanted]
        unknown = wanted - {s.id for s in stacks}
        if unknown:
            raise ProblemException(
                422, "Unknown stacks", f"Not stacks of this space: {sorted(map(str, unknown))}"
            )
    envs = (
        (
            await session.execute(
                select(Environment).where(
                    Environment.stack_id.in_([s.id for s in stacks]),
                    Environment.name.in_([body.reference_name, body.name]),
                )
            )
        )
        .scalars()
        .all()
    )
    reference = {e.stack_id: e for e in envs if e.name == body.reference_name}
    taken = {e.stack_id for e in envs if e.name == body.name}
    missing = [s.name for s in stacks if s.id not in reference]
    if body.stack_ids is None:
        stacks = [s for s in stacks if s.id in reference]  # default: the stacks that have it
    elif missing:
        raise ProblemException(
            422,
            "Reference environment missing",
            f"No '{body.reference_name}' environment in: {', '.join(missing)}",
        )
    clash = [s.name for s in stacks if s.id in taken]
    if clash:
        raise ProblemException(
            422, "Environment exists", f"'{body.name}' already exists in: {', '.join(clash)}"
        )
    if not stacks:
        raise ProblemException(
            422, "Nothing to clone", f"No stack of this space has a '{body.reference_name}' env."
        )
    unmanaged = [s.name for s in stacks if not reference[s.id].managed_state]
    if unmanaged:
        # Its backend settings would point the clone at the reference's own state (§21.2).
        raise ProblemException(
            422,
            "Unmanaged state reference",
            "Cloning an env with managed_state=false would share its state; create these by "
            f"hand: {', '.join(unmanaged)}",
        )
    return [(s, reference[s.id]) for s in stacks]


@router.post("/spaces/{space_id}/environment-clones", status_code=201)
async def clone_environments(
    space_id: uuid.UUID, body: CloneIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    membership = await require_space_access(session, user, space_id, min_role=Role.writer)
    if (
        body.copy_cloud_integration
        and user.role != Role.admin
        and (membership is None or membership.role != Role.admin)
    ):
        raise ProblemException(403, "Forbidden", "Copying cloud integrations requires admin.")
    if body.name == body.reference_name:
        raise ProblemException(422, "Same name", "The new name must differ from the reference.")
    targets = await _targets(session, space_id, body)
    tier = body.overrides.tier
    if (
        tier is not None
        and (await session.execute(select(Tier).where(Tier.name == tier))).scalar_one_or_none()
        is None
    ):
        raise ProblemException(422, "Unknown tier", f"No tier named '{tier}'. Define it first.")

    ov = body.overrides.model_dump(exclude_unset=True, exclude={"labels"})
    actor = _actor(user)
    mapping: dict[uuid.UUID, Environment] = {}  # reference env id → new env
    created: list[dict[str, Any]] = []
    for stack, ref in targets:
        env = Environment(
            stack_id=stack.id,
            name=body.name,
            **{f: getattr(ref, f) for f in _COPIED},
            labels={**(ref.labels or {}), **(body.overrides.labels or {})} or None,
            allow_mock_apply=False,
            allow_fallback_apply=False,
        )
        for field, value in ov.items():
            setattr(env, field, value)
        if env.protected:
            env.autodeploy = False  # protected forces manual confirmation (§3.2)
        session.add(env)
        mapping[ref.id] = env
        created.append({"stack_id": stack.id, "stack_name": stack.name, "env": env})
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(409, "Environment exists", "Created concurrently.") from exc
    for item in created:
        env = item["env"]
        ref_name = body.reference_name
        await record_audit(
            session,
            action="environment.created",
            **actor,
            target_kind="environment",
            target_id=env.id,
            context={"name": env.name, "tier": env.tier, "cloned_from": ref_name},
        )

    missing_secrets: list[dict[str, str]] = []
    to_configure: list[dict[str, str]] = []
    stack_name = {s.id: s.name for s, _ in targets}
    for ref_id, env in mapping.items():
        where = stack_name[env.stack_id]
        # Env-level variables: plain values and external references; never a literal secret.
        for var in (
            await session.execute(select(Variable).where(Variable.environment_id == ref_id))
        ).scalars():
            if var.value_encrypted is not None:
                missing_secrets.append({"stack": where, "environment": env.name, "name": var.name})
                continue
            copy = Variable(
                stack_id=env.stack_id,
                environment_id=env.id,
                kind=var.kind,
                name=var.name,
                value=var.value,
                sensitive=var.sensitive,
                hcl=var.hcl,
                secret_source_id=var.secret_source_id,
                secret_ref=var.secret_ref,
            )
            if var.secret_source_id is not None and var.secret_fallback_encrypted is not None:
                # A static fallback is a literal secret: the reference is copied, not its fallback.
                to_configure.append(
                    {"stack": where, "environment": env.name, "item": f"fallback of {var.name}"}
                )
            copy.secret_fallback_mode = (
                var.secret_fallback_mode
                if var.secret_fallback_encrypted is None
                else SecretFallback.error
            )
            session.add(copy)
            await session.flush()
            await record_audit(
                session,
                action="variable.created",
                **actor,
                target_kind="variable",
                target_id=copy.id,
                context={
                    "name": copy.name,
                    "kind": copy.kind.value,
                    "sensitive": copy.sensitive,
                    "scope": "env",
                    "cloned_from": str(var.id),
                },
            )
        # Explicit variable-set attachments to the env (selectors and tiers apply by themselves).
        for att in (
            await session.execute(
                select(VariableSetAttachment).where(
                    VariableSetAttachment.target_kind == AttachmentTarget.environment,
                    VariableSetAttachment.target_id == ref_id,
                )
            )
        ).scalars():
            session.add(
                VariableSetAttachment(
                    variable_set_id=att.variable_set_id,
                    target_kind=AttachmentTarget.environment,
                    target_id=env.id,
                    priority=att.priority,
                )
            )
            await record_audit(
                session,
                action="variable_set.attached",
                **actor,
                target_kind="variable_set",
                target_id=att.variable_set_id,
                context={
                    "target_kind": "environment",
                    "target_id": str(env.id),
                    "priority": att.priority,
                    "cloned_from": str(ref_id),
                },
            )
        # Platform hooks: always — a clone never has less governance than its model (§8.1).
        for hook in (
            await session.execute(
                select(Hook).where(
                    Hook.target_kind == AttachmentTarget.environment, Hook.target_id == ref_id
                )
            )
        ).scalars():
            new_hook = Hook(
                target_kind=AttachmentTarget.environment,
                target_id=env.id,
                stage=hook.stage,
                name=hook.name,
                command=hook.command,
                on_failure=hook.on_failure,
                position=hook.position,
            )
            session.add(new_hook)
            await session.flush()
            await record_audit(
                session,
                action="hook.created",
                **actor,
                target_kind="hook",
                target_id=new_hook.id,
                context={"stage": hook.stage.value, "name": hook.name, "on": "environment"},
            )
        for target in (
            await session.execute(
                select(NotificationTarget).where(
                    NotificationTarget.target_kind == AttachmentTarget.environment,
                    NotificationTarget.target_id == ref_id,
                )
            )
        ).scalars():
            new_target = NotificationTarget(
                target_kind=AttachmentTarget.environment,
                target_id=env.id,
                name=target.name,
                kind=target.kind,
                url=target.url,
                on_states=list(target.on_states),
                enabled=target.enabled,
            )
            session.add(new_target)
            await session.flush()
            await record_audit(
                session,
                action="notification_target.created",
                **actor,
                target_kind="notification_target",
                target_id=new_target.id,
                context={"on": "environment", "kind": target.kind.value, "name": target.name},
            )
        ci = (
            await session.execute(
                select(CloudIntegration).where(CloudIntegration.environment_id == ref_id)
            )
        ).scalar_one_or_none()
        if ci is not None and body.copy_cloud_integration:
            session.add(
                CloudIntegration(
                    environment_id=env.id,
                    provider=ci.provider,
                    plan_role_arn=ci.plan_role_arn,
                    apply_role_arn=ci.apply_role_arn,
                    region=ci.region,
                    session_duration=ci.session_duration,
                )
            )
            await record_audit(
                session,
                action="cloud_integration.created",
                **actor,
                target_kind="environment",
                target_id=env.id,
                context={"cloned_from": str(ref_id)},
            )
        elif ci is not None:
            to_configure.append(
                {"stack": where, "environment": env.name, "item": "cloud integration"}
            )

    # Dependency edges (§21.2): an incoming edge of a reference env is re-created on its clone,
    # from the clone of its upstream when that one is cloned too, else from the same (shared)
    # upstream. Outgoing edges to non-cloned envs are not copied.
    edges: list[dict[str, str]] = []
    for ref_id, env in mapping.items():
        for dep in (
            await session.execute(
                select(EnvDependency).where(EnvDependency.downstream_env_id == ref_id)
            )
        ).scalars():
            upstream = mapping.get(dep.upstream_env_id)
            upstream_id = upstream.id if upstream is not None else dep.upstream_env_id
            refs = (
                (
                    await session.execute(
                        select(OutputReference).where(OutputReference.dependency_id == dep.id)
                    )
                )
                .scalars()
                .all()
            )
            await _create_edge(session, env.id, upstream_id, dep.trigger_policy, refs)
            await record_audit(
                session,
                action="dependency.created",
                **actor,
                target_kind="environment",
                target_id=env.id,
                context={
                    "upstream_env_id": str(upstream_id),
                    "references": len(refs),
                    "via": "clone",
                },
            )
            edges.append(
                {
                    "upstream_env_id": str(upstream_id),
                    "downstream_env_id": str(env.id),
                    "shared_upstream": str(upstream is None).lower(),
                }
            )

    await record_audit(
        session,
        action="environment.cloned",
        **actor,
        target_kind="space",
        target_id=space_id,
        context={
            "reference_name": body.reference_name,
            "name": body.name,
            "stacks": [item["stack_name"] for item in created],
            "missing_secrets": missing_secrets,
        },
    )
    if body.dry_run:
        await session.rollback()
    else:
        await session.commit()
    return {
        "dry_run": body.dry_run,
        "environments": [
            {
                "id": str(item["env"].id),
                "stack_id": str(item["stack_id"]),
                "stack_name": item["stack_name"],
                "name": item["env"].name,
                "tier": item["env"].tier,
            }
            for item in created
        ],
        "edges": edges,
        "missing_secrets": missing_secrets,
        "to_configure": to_configure,
    }
