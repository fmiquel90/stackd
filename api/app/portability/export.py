"""Export a space's configuration as a `ConfigDocument` (SPECS §22.2) — by names, never a secret."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import AttachmentTarget
from app.models.dependency import EnvDependency, OutputReference
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.notification import NotificationTarget
from app.models.oidc import CloudIntegration
from app.models.secret_source import SecretSource
from app.models.space import Space
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.variable import Variable
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.models.worker import WorkerPool
from app.portability.schemas import (
    AttachmentDoc,
    CloudIntegrationDoc,
    ConfigDocument,
    DependencyDoc,
    EnvironmentDoc,
    EnvRefDoc,
    HookDoc,
    NotificationTargetDoc,
    OutputReferenceDoc,
    SecretRefDoc,
    SecretSourceDoc,
    SourceDoc,
    StackDoc,
    TierDoc,
    VariableDoc,
    VariableSetDoc,
    WorkerPoolDoc,
)


def _variable(var: Variable, source_names: dict[uuid.UUID, str]) -> VariableDoc:
    if var.secret_source_id is not None:
        return VariableDoc(
            kind=var.kind,
            name=var.name,
            hcl=var.hcl,
            sensitive=True,
            secret=SecretRefDoc(
                source=source_names[var.secret_source_id],
                ref=var.secret_ref or "",
                fallback_mode=var.secret_fallback_mode,
            ),
        )
    # Write-only (CLAUDE.md invariant 3): a sensitive value never leaves the API, not even here.
    return VariableDoc(
        kind=var.kind,
        name=var.name,
        hcl=var.hcl,
        sensitive=var.sensitive,
        value=None if var.sensitive else var.value,
    )


def _missing_for(var: Variable, where: dict[str, str]) -> list[dict[str, Any]]:
    if var.secret_source_id is not None:
        if var.secret_fallback_encrypted is not None:
            return [{**where, "item": "secret_fallback", "name": var.name}]
        return []
    if var.sensitive:
        return [{**where, "item": "variable", "kind": var.kind.value, "name": var.name}]
    return []


def _hook(h: Hook) -> HookDoc:
    return HookDoc(
        stage=h.stage, name=h.name, command=h.command, on_failure=h.on_failure, position=h.position
    )


def _notification(n: NotificationTarget) -> NotificationTargetDoc:
    return NotificationTargetDoc(
        name=n.name, kind=n.kind, url=n.url, on_states=list(n.on_states), enabled=n.enabled
    )


async def export_space(session: AsyncSession, space: Space) -> ConfigDocument:
    stacks = list(
        (
            await session.execute(
                select(Stack).where(Stack.space_id == space.id).order_by(Stack.name)
            )
        )
        .scalars()
        .all()
    )
    stack_by_id = {s.id: s for s in stacks}
    envs = list(
        (
            await session.execute(
                select(Environment)
                .where(Environment.stack_id.in_(list(stack_by_id)))
                .order_by(Environment.position, Environment.name)
            )
        )
        .scalars()
        .all()
    )
    env_by_id = {e.id: e for e in envs}
    sets = list(
        (
            await session.execute(
                select(VariableSet)
                .where(VariableSet.space_id == space.id)
                .order_by(VariableSet.name)
            )
        )
        .scalars()
        .all()
    )
    sources = list(
        (
            await session.execute(
                select(SecretSource)
                .where(SecretSource.space_id == space.id)
                .order_by(SecretSource.name)
            )
        )
        .scalars()
        .all()
    )
    source_names = {s.id: s.name for s in sources}

    variables = list(
        (
            await session.execute(
                select(Variable)
                .where(
                    Variable.stack_id.in_(list(stack_by_id))
                    | Variable.variable_set_id.in_([s.id for s in sets])
                )
                .order_by(Variable.kind, Variable.name)
            )
        )
        .scalars()
        .all()
    )
    hooks = list(
        (
            await session.execute(
                select(Hook)
                .where(Hook.target_id.in_(list(stack_by_id) + list(env_by_id)))
                .order_by(Hook.stage, Hook.position, Hook.name)
            )
        )
        .scalars()
        .all()
    )
    notifications = list(
        (
            await session.execute(
                select(NotificationTarget)
                .where(NotificationTarget.target_id.in_(list(stack_by_id) + list(env_by_id)))
                .order_by(NotificationTarget.name)
            )
        )
        .scalars()
        .all()
    )
    clouds = {
        c.environment_id: c
        for c in (
            await session.execute(
                select(CloudIntegration).where(CloudIntegration.environment_id.in_(list(env_by_id)))
            )
        ).scalars()
    }
    tiers = {t.id: t for t in (await session.execute(select(Tier))).scalars()}
    tier_by_name = {t.name: t for t in tiers.values()}

    missing: list[dict[str, Any]] = []
    # A reference to another space's source (a row predating the §15.1 check — the claim already
    # fails it closed): not exported, and the other space's source is not named.
    set_names = {vs.id: vs.name for vs in sets}
    for v in variables:
        if v.secret_source_id is not None and v.secret_source_id not in source_names:
            where: dict[str, Any] = {}
            if v.variable_set_id is not None:
                where["variable_set"] = set_names.get(v.variable_set_id)
            if v.stack_id is not None:
                where["stack"] = stack_by_id[v.stack_id].name
            if v.environment_id is not None:
                where["environment"] = env_by_id[v.environment_id].name
            missing.append({**where, "item": "secret_reference_out_of_space", "name": v.name})
    variables = [
        v for v in variables if v.secret_source_id is None or v.secret_source_id in source_names
    ]

    def scoped(kind: AttachmentTarget, target_id: uuid.UUID, rows: list[Any]) -> list[Any]:
        return [r for r in rows if r.target_kind == kind and r.target_id == target_id]

    stack_docs: list[StackDoc] = []
    for stack in stacks:
        where = {"stack": stack.name}
        env_docs: list[EnvironmentDoc] = []
        for env in (e for e in envs if e.stack_id == stack.id):
            env_where = {**where, "environment": env.name}
            env_vars = [v for v in variables if v.environment_id == env.id]
            for v in env_vars:
                missing.extend(_missing_for(v, env_where))
            ci = clouds.get(env.id)
            env_docs.append(
                EnvironmentDoc(
                    id=env.id,
                    name=env.name,
                    tier=env.tier,
                    branch=env.branch,
                    autodeploy=env.autodeploy,
                    protected=env.protected,
                    require_second_pair_of_eyes=env.require_second_pair_of_eyes,
                    managed_state=env.managed_state,
                    allow_mock_apply=env.allow_mock_apply,
                    allow_fallback_apply=env.allow_fallback_apply,
                    drift_check_enabled=env.drift_check_enabled,
                    backend_config_file=env.backend_config_file,
                    backend_config=env.backend_config,
                    labels=env.labels,
                    position=env.position,
                    variables=[_variable(v, source_names) for v in env_vars],
                    hooks=[_hook(h) for h in scoped(AttachmentTarget.environment, env.id, hooks)],
                    notification_targets=[
                        _notification(n)
                        for n in scoped(AttachmentTarget.environment, env.id, notifications)
                    ],
                    cloud_integration=(
                        CloudIntegrationDoc(
                            provider=ci.provider,
                            plan_role_arn=ci.plan_role_arn,
                            apply_role_arn=ci.apply_role_arn,
                            region=ci.region,
                            session_duration=ci.session_duration,
                        )
                        if ci is not None
                        else None
                    ),
                )
            )
        stack_vars = [v for v in variables if v.stack_id == stack.id and v.environment_id is None]
        for v in stack_vars:
            missing.extend(_missing_for(v, where))
        if stack.repo_secret_encrypted is not None:
            missing.append({**where, "item": "repo_secret"})
        if stack.webhook_secret_encrypted is not None:
            missing.append({**where, "item": "webhook_secret"})
        stack_docs.append(
            StackDoc(
                id=stack.id,
                name=stack.name,
                description=stack.description,
                repo_url=stack.repo_url,
                repo_auth_kind=stack.repo_auth_kind,
                has_repo_secret=stack.repo_secret_encrypted is not None,
                has_webhook_secret=stack.webhook_secret_encrypted is not None,
                project_root=stack.project_root,
                tool=stack.tool,
                tool_version=stack.tool_version,
                labels=stack.labels,
                variables=[_variable(v, source_names) for v in stack_vars],
                hooks=[_hook(h) for h in scoped(AttachmentTarget.stack, stack.id, hooks)],
                notification_targets=[
                    _notification(n)
                    for n in scoped(AttachmentTarget.stack, stack.id, notifications)
                ],
                environments=env_docs,
            )
        )

    attachments = list(
        (
            await session.execute(
                select(VariableSetAttachment)
                .where(VariableSetAttachment.variable_set_id.in_([s.id for s in sets]))
                .order_by(VariableSetAttachment.priority)
            )
        )
        .scalars()
        .all()
    )
    used_tiers = {e.tier for e in envs}
    set_docs: list[VariableSetDoc] = []
    for vs in sets:
        att_docs: list[AttachmentDoc] = []
        for att in (a for a in attachments if a.variable_set_id == vs.id):
            if att.target_kind == AttachmentTarget.stack and att.target_id in stack_by_id:
                att_docs.append(
                    AttachmentDoc(
                        target="stack",
                        stack=stack_by_id[att.target_id].name,
                        priority=att.priority,
                    )
                )
            elif att.target_kind == AttachmentTarget.environment and att.target_id in env_by_id:
                env = env_by_id[att.target_id]
                att_docs.append(
                    AttachmentDoc(
                        target="environment",
                        stack=stack_by_id[env.stack_id].name,
                        environment=env.name,
                        priority=att.priority,
                    )
                )
            elif att.target_kind == AttachmentTarget.tier and att.target_id in tiers:
                name = tiers[att.target_id].name
                used_tiers.add(name)
                att_docs.append(AttachmentDoc(target="tier", tier=name, priority=att.priority))
            # Any other row would target outside the space: resolution ignores it (§3.4), so does
            # the export.
        set_vars = [v for v in variables if v.variable_set_id == vs.id]
        for v in set_vars:
            missing.extend(_missing_for(v, {"variable_set": vs.name}))
        set_docs.append(
            VariableSetDoc(
                name=vs.name,
                description=vs.description,
                auto_attach=vs.auto_attach,
                selector=vs.selector,
                variables=[_variable(v, source_names) for v in set_vars],
                attachments=att_docs,
            )
        )

    # Edges belong to their downstream's space: export every edge INTO this space, naming an
    # upstream of another space with its space.
    deps = list(
        (
            await session.execute(
                select(EnvDependency).where(EnvDependency.downstream_env_id.in_(list(env_by_id)))
            )
        )
        .scalars()
        .all()
    )
    refs = list(
        (
            await session.execute(
                select(OutputReference)
                .where(OutputReference.dependency_id.in_([d.id for d in deps]))
                .order_by(OutputReference.input_name)
            )
        )
        .scalars()
        .all()
    )
    foreign_ids = {d.upstream_env_id for d in deps} - set(env_by_id)
    foreign: dict[uuid.UUID, tuple[str, str, str]] = {}
    if foreign_ids:
        for env, stack, sp in await session.execute(
            select(Environment, Stack, Space)
            .join(Stack, Stack.id == Environment.stack_id)
            .join(Space, Space.id == Stack.space_id)
            .where(Environment.id.in_(foreign_ids))
        ):
            foreign[env.id] = (sp.name, stack.name, env.name)

    def ref(env_id: uuid.UUID) -> EnvRefDoc:
        if env_id in env_by_id:
            env = env_by_id[env_id]
            return EnvRefDoc(stack=stack_by_id[env.stack_id].name, environment=env.name)
        sp_name, st_name, env_name = foreign[env_id]
        return EnvRefDoc(space=sp_name, stack=st_name, environment=env_name)

    dep_docs = sorted(
        (
            DependencyDoc(
                upstream=ref(d.upstream_env_id),
                downstream=ref(d.downstream_env_id),
                trigger_policy=d.trigger_policy,
                references=[
                    OutputReferenceDoc(
                        output_name=r.output_name, input_name=r.input_name, mock_value=r.mock_value
                    )
                    for r in refs
                    if r.dependency_id == d.id
                ],
            )
            for d in deps
        ),
        key=lambda d: (
            d.downstream.stack,
            d.downstream.environment,
            d.upstream.space or "",
            d.upstream.stack,
            d.upstream.environment,
        ),
    )

    pools = (
        (
            await session.execute(
                select(WorkerPool).where(WorkerPool.space_id == space.id).order_by(WorkerPool.name)
            )
        )
        .scalars()
        .all()
    )
    return ConfigDocument(
        exported_at=datetime.now(UTC),
        source=SourceDoc(
            space=space.name, description=space.description, url=get_settings().stackd_public_url
        ),
        tiers=[
            TierDoc(
                name=t.name,
                requires_four_eyes=t.requires_four_eyes,
                repo_hooks_at_apply=t.repo_hooks_at_apply,
                position=t.position,
            )
            for t in sorted(
                (tier_by_name[n] for n in used_tiers if n in tier_by_name),
                key=lambda t: (t.position, t.name),
            )
        ],
        secret_sources=[
            SecretSourceDoc(name=s.name, provider=s.provider, config=s.config) for s in sources
        ],
        worker_pools=[WorkerPoolDoc(name=p.name, labels=p.labels) for p in pools],
        variable_sets=set_docs,
        stacks=stack_docs,
        dependencies=dep_docs,
        missing_secrets=[
            *({"secret_source": s.name, "item": "bootstrap_secret"} for s in sources),
            *missing,
        ],
    )
