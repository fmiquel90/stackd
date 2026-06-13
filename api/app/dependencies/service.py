from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.enums import (
    AuditActorKind,
    RunState,
    RunType,
    TriggeredBy,
    TriggerPolicy,
)
from app.models.dependency import EnvDependency, EnvOutput, OutputReference
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack


class DependencyError(Exception):
    """Unresolvable dependency at claim time → the run fails immediately (§9.3)."""


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class DependencyResolution:
    resolved_inputs: dict[str, object] = field(default_factory=dict)  # input_name → real value
    mock_inputs: dict[str, object] = field(default_factory=dict)  # input_name → mock value
    provenance: dict[str, str] = field(
        default_factory=dict
    )  # TF_VAR_name → "dependency:.." | "mock"
    consumed_mocks: list[str] = field(default_factory=list)

    @property
    def used_mocks(self) -> bool:
        return bool(self.mock_inputs)


async def resolve_dependency_inputs(
    session: AsyncSession, env: Environment
) -> DependencyResolution:
    """Resolve incoming dependency inputs (§9.3): real value > mock > explicit error."""
    res = DependencyResolution()
    deps = (
        (
            await session.execute(
                select(EnvDependency).where(EnvDependency.downstream_env_id == env.id)
            )
        )
        .scalars()
        .all()
    )

    for dep in deps:
        upstream = await session.get(Environment, dep.upstream_env_id)
        stack = await session.get(Stack, upstream.stack_id) if upstream else None
        label = f"{stack.name}/{upstream.name}" if stack and upstream else "upstream"
        refs = (
            (
                await session.execute(
                    select(OutputReference).where(OutputReference.dependency_id == dep.id)
                )
            )
            .scalars()
            .all()
        )

        for ref in refs:
            output = (
                await session.execute(
                    select(EnvOutput).where(
                        EnvOutput.environment_id == dep.upstream_env_id,
                        EnvOutput.name == ref.output_name,
                    )
                )
            ).scalar_one_or_none()
            injected = f"TF_VAR_{ref.input_name}"

            if output is not None and output.sensitive:
                raise DependencyError(
                    f"output_reference points to sensitive output '{ref.output_name}'"
                )
            if output is not None:
                res.resolved_inputs[ref.input_name] = output.value
                res.provenance[injected] = f"dependency:{label}"
            elif ref.mock_value is not None:
                res.mock_inputs[ref.input_name] = ref.mock_value
                res.provenance[injected] = "mock"
                res.consumed_mocks.append(ref.input_name)
            else:
                raise DependencyError(f"missing_upstream_output:{label}:{ref.output_name}")
    return res


async def capture_outputs(session: AsyncSession, run: Run, outputs: dict) -> None:
    """Persist outputs after apply (§9.1). Sensitive → value NULL, never propagated."""
    for name, meta in outputs.items():
        sensitive = bool(meta.get("sensitive", False)) if isinstance(meta, dict) else False
        value = None if sensitive else (meta.get("value") if isinstance(meta, dict) else meta)
        stmt = (
            pg_insert(EnvOutput)
            .values(
                environment_id=run.environment_id,
                run_id=run.id,
                name=name,
                value=value,
                value_hash=None if sensitive else canonical_hash(value),
                sensitive=sensitive,
            )
            .on_conflict_do_update(
                index_elements=["environment_id", "name"],
                set_={
                    "value": value,
                    "value_hash": None if sensitive else canonical_hash(value),
                    "sensitive": sensitive,
                    "run_id": run.id,
                },
            )
        )
        await session.execute(stmt)


async def _should_trigger(
    session: AsyncSession, dep: EnvDependency, downstream: Environment
) -> bool:
    if dep.trigger_policy == TriggerPolicy.never:
        return False
    if dep.trigger_policy == TriggerPolicy.always:
        return True
    # on_output_change: trigger unless the downstream's last finished run already consumed the
    # current upstream values for every referenced output.
    last = (
        await session.execute(
            select(Run)
            .where(Run.environment_id == downstream.id, Run.state == RunState.finished)
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return True
    consumed = last.resolved_inputs or {}
    refs = (
        (
            await session.execute(
                select(OutputReference).where(OutputReference.dependency_id == dep.id)
            )
        )
        .scalars()
        .all()
    )
    for ref in refs:
        out = (
            await session.execute(
                select(EnvOutput).where(
                    EnvOutput.environment_id == dep.upstream_env_id,
                    EnvOutput.name == ref.output_name,
                )
            )
        ).scalar_one_or_none()
        if out is not None and consumed.get(ref.input_name) != out.value:
            return True
    return False


async def cascade(session: AsyncSession, run: Run) -> list[Run]:
    """Propagate to downstream environments after a tracked apply finishes (§9.2).

    The cascade never bypasses protections: a protected downstream still stops at `unconfirmed`
    because its confirm is gated independently. Multi-parent gating is a documented follow-up.
    """
    if run.type != RunType.tracked:
        return []
    outgoing = (
        (
            await session.execute(
                select(EnvDependency).where(EnvDependency.upstream_env_id == run.environment_id)
            )
        )
        .scalars()
        .all()
    )

    group_id = run.run_group_id or run.id
    triggered: list[Run] = []
    for dep in outgoing:
        downstream = await session.get(Environment, dep.downstream_env_id)
        if downstream is None or not await _should_trigger(session, dep, downstream):
            continue
        child = Run(
            environment_id=downstream.id,
            type=RunType.tracked,
            state=RunState.queued,
            triggered_by=TriggeredBy.dependency,
            parent_run_id=run.id,
            run_group_id=group_id,
        )
        session.add(child)
        await session.flush()
        await record_audit(
            session,
            action="run.triggered",
            actor_kind=AuditActorKind.system,
            target_kind="run",
            target_id=child.id,
            context={"via": "cascade", "parent_run_id": str(run.id)},
        )
        triggered.append(child)
    return triggered
