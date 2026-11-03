"""Environment clones (SPECS §21.2): one-shot creation of an environment in N stacks of a space,
modelled on the environment each stack already has under `reference_name` — its settings, its
governance and its dependency edges. All or nothing, in one transaction; every created item audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.dependencies.router import _create_edge
from app.enums import AttachmentTarget, AuditActorKind, Role, SecretFallback, VariableKind
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
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.spaces import guard_env_id, require_space_access
from app.variables.resolution import _selector_matches, resolve_variables

router = APIRouter(prefix="/api/v1", tags=["environment-clones"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


class CloneOverrides(BaseModel):
    # Each field may be omitted (the reference's value is kept) but never `null`: these settings are
    # all required on an environment, and a null tier would also slip past the tier check.
    tier: str | None = Field(default=None, min_length=1)
    branch: str | None = Field(default=None, min_length=1)
    labels: dict[str, Any] | None = None  # merged onto the reference's labels, these win per key
    autodeploy: bool | None = None
    protected: bool | None = None
    require_second_pair_of_eyes: bool | None = None
    drift_check_enabled: bool | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _not_null(cls, v: Any) -> Any:
        # Runs on explicitly sent values only (defaults are not validated).
        if v is None:
            raise ValueError("omit the field to keep the reference's value; null is not accepted")
        return v


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

_Key = tuple[VariableKind, str]


@dataclass
class _Target:
    """One target stack: its reference env and what the clone takes from it (computed up front)."""

    stack: Stack
    ref: Environment
    env_labels: dict[str, Any] | None
    # Sets attaching to the new env by selector: (set, not attached to the reference by selector).
    label_sets: list[tuple[VariableSet, bool]] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    attachments: list[VariableSetAttachment] = field(default_factory=list)
    missing: list[Variable] = field(default_factory=list)
    to_configure: list[str] = field(default_factory=list)
    not_copied: list[dict[str, Any]] = field(default_factory=list)
    cloud: CloudIntegration | None = None
    env: Environment | None = None
    autodeploy_off_reason: str | None = None


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


async def _plan(
    session: AsyncSession,
    space_id: uuid.UUID,
    body: CloneIn,
    pairs: list[tuple[Stack, Environment]],
) -> list[_Target]:
    """What each new env takes from its reference, before anything is written.

    Shadowing (§21.2): a variable set attached by the new env's labels sits at resolution layer 1
    (§3.4), below the env's explicit attachments (layer 4) and env variables (layer 6). A copied
    item providing a key that a set newly selected by the clone's labels also provides would
    silently win over it — e.g. `region=eu-west-1` copied from the model over the `region` of the
    set selecting `region=eu-west-3`. Such items are not copied (`not_copied`)."""
    sets = (
        (
            await session.execute(
                select(VariableSet)
                .where(VariableSet.space_id == space_id)
                .order_by(VariableSet.name)
            )
        )
        .scalars()
        .all()
    )
    set_by_id = {s.id: s for s in sets}
    set_keys: dict[uuid.UUID, set[_Key]] = {s.id: set() for s in sets}
    if sets:
        for var in (
            await session.execute(
                select(Variable).where(Variable.variable_set_id.in_(list(set_by_id)))
            )
        ).scalars():
            assert var.variable_set_id is not None
            set_keys[var.variable_set_id].add((var.kind, var.name))

    targets: list[_Target] = []
    for stack, ref in pairs:
        env_labels = {**(ref.labels or {}), **(body.overrides.labels or {})} or None
        ref_effective = {**(stack.labels or {}), **(ref.labels or {})}
        new_effective = {**(stack.labels or {}), **(env_labels or {})}
        t = _Target(stack=stack, ref=ref, env_labels=env_labels)
        t.label_sets = [
            (s, not _selector_matches(s.selector, ref_effective))
            for s in sets
            if not s.auto_attach and _selector_matches(s.selector, new_effective)
        ]
        # Keys a set newly attached by the clone's labels provides → the names of those sets.
        shadowable: dict[_Key, list[str]] = {}
        for s, new in t.label_sets:
            if new:
                for key in set_keys[s.id]:
                    shadowable.setdefault(key, []).append(s.name)
        new_set_ids = {s.id for s, new in t.label_sets if new}

        # Env-level variables: plain values and external references; never a literal secret.
        for var in (
            await session.execute(
                select(Variable).where(Variable.environment_id == ref.id).order_by(Variable.name)
            )
        ).scalars():
            key = (var.kind, var.name)
            if key in shadowable:
                t.not_copied.append(
                    {
                        "item": "variable",
                        "name": var.name,
                        "reason": "shadows_label_set",
                        "keys": [var.name],
                        "label_sets": sorted(shadowable[key]),
                    }
                )
            elif var.value_encrypted is not None:
                t.missing.append(var)
            else:
                t.variables.append(var)
                if var.secret_source_id is not None and var.secret_fallback_encrypted is not None:
                    # A static fallback is a literal secret: the reference is copied, not it.
                    t.to_configure.append(f"fallback of {var.name}")

        # Explicit variable-set attachments to the env (selectors and tiers apply by themselves).
        for att in (
            await session.execute(
                select(VariableSetAttachment)
                .where(
                    VariableSetAttachment.target_kind == AttachmentTarget.environment,
                    VariableSetAttachment.target_id == ref.id,
                )
                .order_by(VariableSetAttachment.priority)
            )
        ).scalars():
            hit = (
                set()
                if att.variable_set_id in new_set_ids  # the same set: shadowing itself is moot
                else set_keys.get(att.variable_set_id, set()) & shadowable.keys()
            )
            if hit:
                attached = set_by_id[att.variable_set_id]
                t.not_copied.append(
                    {
                        "item": "variable_set",
                        "name": attached.name,
                        "reason": "shadows_label_set",
                        "keys": sorted({name for _, name in hit}),
                        "label_sets": sorted({n for key in hit for n in shadowable[key]}),
                    }
                )
            else:
                t.attachments.append(att)

        t.cloud = (
            await session.execute(
                select(CloudIntegration).where(CloudIntegration.environment_id == ref.id)
            )
        ).scalar_one_or_none()
        if t.cloud is not None and not body.copy_cloud_integration:
            t.to_configure.append("cloud integration")
        targets.append(t)
    return targets


def _integrity_problem(exc: IntegrityError, body: CloneIn) -> ProblemException:
    """Name the conflict that actually happened — never blame a concurrent create for, e.g., a tier
    deleted between the check and the insert."""
    cause = exc.orig.__cause__ if exc.orig is not None else None
    constraint = getattr(cause, "constraint_name", None) or str(exc.orig)
    if "uq_environments_stack_id_name" in constraint:
        return ProblemException(
            409,
            "Environment exists",
            f"'{body.name}' was created concurrently in one of the stacks; nothing was created.",
        )
    if "fk_environments_tier" in constraint:  # migration 0015
        return ProblemException(
            422,
            "Unknown tier",
            f"Tier '{body.overrides.tier or 'of a reference'}' no longer exists (deleted "
            "concurrently); nothing was created.",
        )
    return ProblemException(
        409,
        "Conflicting change",
        "Something the clone copies changed concurrently (a stack, a variable set, a secret "
        "source…); nothing was created. Retry.",
    )


@router.post(
    "/spaces/{space_id}/environment-clones",
    status_code=201,
    # Global role, like every route that creates environments, hooks or edges.
    dependencies=[Depends(require_role(Role.writer))],
)
async def clone_environments(
    space_id: uuid.UUID, body: CloneIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    await require_space_access(session, user, space_id, min_role=Role.writer)
    if body.copy_cloud_integration and user.role != Role.admin:
        # Attaching role ARNs is an instance admin's call, as on PUT …/cloud-integration (§10).
        raise ProblemException(
            403, "Forbidden", "Copying cloud integrations requires the instance admin role."
        )
    if body.name == body.reference_name:
        raise ProblemException(422, "Same name", "The new name must differ from the reference.")
    pairs = await _targets(session, space_id, body)
    tier = body.overrides.tier
    if (
        tier is not None
        and (await session.execute(select(Tier).where(Tier.name == tier))).scalar_one_or_none()
        is None
    ):
        raise ProblemException(422, "Unknown tier", f"No tier named '{tier}'. Define it first.")
    try:
        result = await _clone(
            session, user, space_id, body, await _plan(session, space_id, body, pairs)
        )
    except IntegrityError as exc:
        raise _integrity_problem(exc, body) from exc
    if body.dry_run:
        await session.rollback()
    else:
        await session.commit()
    return result


async def _clone(
    session: AsyncSession, user: User, space_id: uuid.UUID, body: CloneIn, targets: list[_Target]
) -> dict[str, Any]:
    ov = body.overrides.model_dump(exclude_unset=True, exclude={"labels"})
    actor = _actor(user)
    mapping: dict[uuid.UUID, Environment] = {}  # reference env id → new env
    for t in targets:
        env = Environment(
            stack_id=t.stack.id,
            name=body.name,
            **{f: getattr(t.ref, f) for f in _COPIED},
            labels=t.env_labels,
            allow_mock_apply=False,
            allow_fallback_apply=False,
        )
        for name, value in ov.items():
            setattr(env, name, value)
        if env.autodeploy and env.protected:
            t.autodeploy_off_reason = "protected"  # protected forces manual confirmation (§3.2)
        elif env.autodeploy and (t.missing or t.to_configure):
            # A push must not plan and apply before the secrets are filled: an unset input would
            # fall back to a lower-scope value with the same name (§3.4), e.g. the dev password.
            t.autodeploy_off_reason = "left_to_fill"
        if t.autodeploy_off_reason:
            env.autodeploy = False
        session.add(env)
        t.env = env
        mapping[t.ref.id] = env
    await session.flush()
    for t in targets:
        assert t.env is not None
        await record_audit(
            session,
            action="environment.created",
            **actor,
            target_kind="environment",
            target_id=t.env.id,
            context={
                "name": t.env.name,
                "tier": t.env.tier,
                "cloned_from": body.reference_name,
                "autodeploy": t.env.autodeploy,
                **(
                    {"autodeploy_off_reason": t.autodeploy_off_reason}
                    if t.autodeploy_off_reason
                    else {}
                ),
            },
        )

    for t in targets:
        assert t.env is not None
        env = t.env
        for var in t.variables:
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
                # A static fallback is not copied, so its mode falls back to `error` (§15.2).
                secret_fallback_mode=(
                    var.secret_fallback_mode
                    if var.secret_fallback_encrypted is None
                    else SecretFallback.error
                ),
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
        for att in t.attachments:
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
                    "cloned_from": str(t.ref.id),
                },
            )
        # Platform hooks: always — a clone never has less governance than its model (§8.1).
        for hook in (
            await session.execute(
                select(Hook).where(
                    Hook.target_kind == AttachmentTarget.environment, Hook.target_id == t.ref.id
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
                    NotificationTarget.target_id == t.ref.id,
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
        ci = t.cloud
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
                context={"cloned_from": str(t.ref.id)},
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
            if upstream is None:
                # A shared upstream: wiring a new consumer of its outputs needs writer access to its
                # space too, as creating the edge by hand does (§3.8).
                await guard_env_id(session, user, upstream_id, min_role=Role.writer)
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

    # What a missing secret resolves to until it is filled: an unset env-level input falls back to
    # the next layer down (§3.4) — flag it, the value is the model's region/tier's, not this one's.
    await session.flush()
    missing_secrets: list[dict[str, Any]] = []
    to_configure: list[dict[str, str]] = []
    not_copied: list[dict[str, Any]] = []
    for t in targets:
        assert t.env is not None
        env = t.env
        where = {"stack": t.stack.name, "environment": env.name}
        if t.missing:
            provenance = {
                (rv.kind, rv.name): rv.provenance for rv in await resolve_variables(session, env)
            }
            missing_secrets.extend(
                {**where, "name": var.name, "falls_back_to": provenance.get((var.kind, var.name))}
                for var in t.missing
            )
        to_configure.extend({**where, "item": item} for item in t.to_configure)
        not_copied.extend({**where, **item} for item in t.not_copied)

    await record_audit(
        session,
        action="environment.cloned",
        **actor,
        target_kind="space",
        target_id=space_id,
        context={
            "reference_name": body.reference_name,
            "name": body.name,
            "stacks": [t.stack.name for t in targets],
            "missing_secrets": missing_secrets,
            "not_copied": not_copied,
        },
    )
    return {
        "dry_run": body.dry_run,
        "environments": [
            {
                "id": str(t.env.id),
                "stack_id": str(t.stack.id),
                "stack_name": t.stack.name,
                "name": t.env.name,
                "tier": t.env.tier,
                "autodeploy": t.env.autodeploy,
                "autodeploy_off_reason": t.autodeploy_off_reason,
                "label_sets": [{"name": s.name, "new": new} for s, new in t.label_sets],
            }
            for t in targets
            if t.env is not None
        ],
        "edges": edges,
        "missing_secrets": missing_secrets,
        "to_configure": to_configure,
        "not_copied": not_copied,
    }
