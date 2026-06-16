from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AttachmentTarget, SecretFallback, VariableKind
from app.models.environment import Environment
from app.models.stack import Stack
from app.models.tier import Tier
from app.models.variable import Variable
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.variables.values import reveal_value


@dataclass
class ResolvedVariable:
    name: str
    kind: VariableKind
    sensitive: bool
    hcl: bool
    provenance: str  # "set:<name>" | "stack" | "env"; dependency/mock are merged in at claim (§9)
    value: str | None  # masked (None) unless reveal_sensitive and sensitive
    # External secret reference (§15) — value stays None after layered resolution; it is fetched
    # from the provider in a dedicated pass at claim time (app.secret_sources.service).
    secret_source_id: uuid.UUID | None = None
    secret_ref: str | None = None
    secret_fallback_mode: SecretFallback = SecretFallback.error
    secret_fallback_encrypted: bytes | None = None

    @property
    def injected_name(self) -> str:
        return f"TF_VAR_{self.name}" if self.kind == VariableKind.terraform else self.name

    @property
    def is_reference(self) -> bool:
        return self.secret_source_id is not None


def _key(var: Variable) -> tuple[VariableKind, str]:
    return (var.kind, var.name)


def _selector_matches(selector: dict | None, labels: dict) -> bool:
    """A selector matches when every key=value it lists is present in `labels` (AND-equality).
    Empty/absent selector never matches via this path (use auto_attach for space-wide)."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


async def _sets_for_env(
    session: AsyncSession, env: Environment, stack: Stack
) -> list[tuple[VariableSet, str]]:
    """Variable sets applicable to `env`, ordered weakest→strongest (SPECS §3.4 steps 1-3)."""
    ordered: list[tuple[VariableSet, str]] = []

    # 1. Space-wide sets that apply by rule: auto_attach (all), or a selector matching the env's
    # effective labels (stack + env, env wins on conflict). Ordered by name for determinism.
    effective_labels = {**(stack.labels or {}), **(env.labels or {})}
    candidates = (
        (
            await session.execute(
                select(VariableSet)
                .where(VariableSet.space_id == stack.space_id)
                .order_by(VariableSet.name)
            )
        )
        .scalars()
        .all()
    )
    ordered.extend(
        (s, "auto")
        for s in candidates
        if s.auto_attach or _selector_matches(s.selector, effective_labels)
    )

    # 2. explicit attachments, weakest→strongest: tier (all envs of the tier) < stack (all envs of
    # the stack) < this env. Each ordered by priority asc.
    tier_row = (
        await session.execute(select(Tier).where(Tier.name == env.tier))
    ).scalar_one_or_none()
    targets: list[tuple[AttachmentTarget, uuid.UUID]] = []
    if tier_row is not None:
        targets.append((AttachmentTarget.tier, tier_row.id))
    targets.append((AttachmentTarget.stack, env.stack_id))
    targets.append((AttachmentTarget.environment, env.id))
    for kind, target_id in targets:
        rows = (
            await session.execute(
                select(VariableSet, VariableSetAttachment.priority)
                .join(
                    VariableSetAttachment, VariableSetAttachment.variable_set_id == VariableSet.id
                )
                .where(
                    VariableSetAttachment.target_kind == kind,
                    VariableSetAttachment.target_id == target_id,
                )
                .order_by(VariableSetAttachment.priority, VariableSet.name)
            )
        ).all()
        ordered.extend((s, "attach") for s, _ in rows)

    return ordered


async def resolve_variables(
    session: AsyncSession, env: Environment, *, reveal_sensitive: bool = False
) -> list[ResolvedVariable]:
    """Resolve the effective variables for an environment.

    Order (weakest→strongest, §3.4): auto_attach sets < stack-attached sets < env-attached sets
    < stack variables < env variables. At equal (kind, name) the stronger layer wins.
    Sensitive values are masked (None) unless `reveal_sensitive` (claim time only, §7.2).
    """
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None
    merged: dict[tuple[VariableKind, str], ResolvedVariable] = {}

    def apply(var: Variable, provenance: str) -> None:
        merged[_key(var)] = ResolvedVariable(
            name=var.name,
            kind=var.kind,
            sensitive=var.sensitive,
            hcl=var.hcl,
            provenance=provenance,
            value=(reveal_value(var) if (reveal_sensitive or not var.sensitive) else None),
            secret_source_id=var.secret_source_id,
            secret_ref=var.secret_ref,
            secret_fallback_mode=var.secret_fallback_mode,
            secret_fallback_encrypted=var.secret_fallback_encrypted,
        )

    # Layers 1-3: variable sets.
    for vset, _ in await _sets_for_env(session, env, stack):
        set_vars = (
            (await session.execute(select(Variable).where(Variable.variable_set_id == vset.id)))
            .scalars()
            .all()
        )
        for var in set_vars:
            apply(var, f"set:{vset.name}")

    # Layer 4: stack variables (env override slot empty).
    stack_vars = (
        (
            await session.execute(
                select(Variable).where(
                    Variable.stack_id == stack.id, Variable.environment_id.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for var in stack_vars:
        apply(var, "stack")

    # Layer 5: env variables — always win.
    env_vars = (
        (await session.execute(select(Variable).where(Variable.environment_id == env.id)))
        .scalars()
        .all()
    )
    for var in env_vars:
        apply(var, "env")

    return list(merged.values())


def provenance_snapshot(resolved: list[ResolvedVariable]) -> dict[str, str]:
    """Frozen provenance map keyed by injected name (SPECS §3.4 / runs.variable_provenance)."""
    return {rv.injected_name: rv.provenance for rv in resolved}
