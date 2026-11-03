from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.enums import (
    ACTIVE_STATES,
    TERMINAL_STATES,
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


# A run ending in one of these stops its branch of the group (§9.2 step 5).
_BROKEN_STATES = frozenset({RunState.failed, RunState.discarded, RunState.canceled})


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


def _in_group(group_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """A run group = its root run (id = group id) and every run carrying the group id (§9.4)."""
    return or_(Run.run_group_id == group_id, Run.id == group_id)


async def group_status(session: AsyncSession, group_id: uuid.UUID) -> str:
    """Computed run-group status (§9.2): `running` while a run of the group is active (or queued),
    else `partial_failure` if one failed, was discarded or canceled, else `succeeded`."""
    states = set((await session.execute(select(Run.state).where(_in_group(group_id)))).scalars())
    if states & (ACTIVE_STATES | {RunState.queued}):
        return "running"
    if states & _BROKEN_STATES:
        return "partial_failure"
    return "succeeded"


async def _decide_downstream(
    session: AsyncSession, group_id: uuid.UUID, downstream: Environment
) -> bool:
    """Should `downstream` get its run in the group now (§9.2 steps 4-5)?

    - it already has one in the group → no (a downstream runs once per group);
    - one of its upstreams has a run in the group that is not terminal → not yet (multi-parent
      gating: the last parent to finish triggers it);
    - one of those runs ended failed / discarded / canceled → never (stop on failure: the branch
      stops there, and nothing reachable from it gets a run in the group);
    - otherwise → yes if an edge from an upstream whose group run *applied* passes its trigger
      policy. An upstream with no run in the group does not hold it back (its current outputs are
      used) and does not trigger it either.
    """
    group_runs = (
        (await session.execute(select(Run).where(_in_group(group_id), Run.type == RunType.tracked)))
        .scalars()
        .all()
    )
    if any(r.environment_id == downstream.id for r in group_runs):
        return False
    by_env: dict[uuid.UUID, list[Run]] = {}
    for r in group_runs:
        by_env.setdefault(r.environment_id, []).append(r)
    incoming = (
        (
            await session.execute(
                select(EnvDependency).where(EnvDependency.downstream_env_id == downstream.id)
            )
        )
        .scalars()
        .all()
    )
    applied_edges: list[EnvDependency] = []
    for dep in incoming:
        runs = by_env.get(dep.upstream_env_id, [])
        if any(r.state not in TERMINAL_STATES for r in runs):
            return False
        if any(r.state in _BROKEN_STATES for r in runs):
            return False
        if any(r.state == RunState.finished and r.confirmed_at is not None for r in runs):
            applied_edges.append(dep)
    for dep in applied_edges:
        if await _should_trigger(session, dep, downstream):
            return True
    return False


async def cascade(session: AsyncSession, run: Run) -> list[Run]:
    """Propagate downstream after a run of the group ended `finished` (§9.2): after an apply, and
    also after an empty plan — it may be the last parent a downstream was waiting for.

    Every downstream environment of the run's env is decided by `_decide_downstream` (multi-parent
    gating, stop on failure, trigger policy). The cascade never bypasses protections: a protected
    downstream still stops at `unconfirmed` because its confirm is gated independently.
    """
    if run.type != RunType.tracked or run.state != RunState.finished:
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
    for downstream_id in dict.fromkeys(dep.downstream_env_id for dep in outgoing):
        downstream = await session.get(Environment, downstream_id)
        if downstream is None or not await _decide_downstream(session, group_id, downstream):
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
