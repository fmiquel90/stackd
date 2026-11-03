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


async def _scope(
    session: AsyncSession, root_env: uuid.UUID
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[EnvDependency]]]:
    """The group's scope: every environment reachable from the root's env, in topological order,
    with each one's incoming edges from inside the scope. The graph is acyclic (§3.8)."""
    edges = (await session.execute(select(EnvDependency))).scalars().all()
    out: dict[uuid.UUID, list[EnvDependency]] = {}
    for dep in edges:
        out.setdefault(dep.upstream_env_id, []).append(dep)
    scope: set[uuid.UUID] = {root_env}
    stack = [root_env]
    while stack:
        for dep in out.get(stack.pop(), []):
            if dep.downstream_env_id not in scope:
                scope.add(dep.downstream_env_id)
                stack.append(dep.downstream_env_id)
    incoming: dict[uuid.UUID, list[EnvDependency]] = {e: [] for e in scope}
    for dep in edges:
        if dep.upstream_env_id in scope and dep.downstream_env_id in scope:
            incoming[dep.downstream_env_id].append(dep)
    indegree = {e: len(incoming[e]) for e in scope}
    order: list[uuid.UUID] = []
    ready = sorted(e for e in scope if indegree[e] == 0)
    while ready:
        node = ready.pop()
        order.append(node)
        for dep in out.get(node, []):
            if dep.downstream_env_id in indegree:
                indegree[dep.downstream_env_id] -= 1
                if indegree[dep.downstream_env_id] == 0:
                    ready.append(dep.downstream_env_id)
    return order, incoming


async def cascade(session: AsyncSession, run: Run) -> list[Run]:
    """Propagate through a run group after one of its tracked runs ended `finished` (§9.2) — after
    an apply, and after an empty plan too (it may be the parent a downstream waits for).

    The whole group is decided at once, under a lock on the group's root run (two parents
    finishing at the same moment are serialized: the second sees the first's outcome). Every
    environment reachable from the root's env (the scope) is settled in topological order:
    - it has a run in the group: `done` (finished; *applied* if it was confirmed), `broken`
      (failed, discarded, canceled) or `active`;
    - otherwise, from its upstreams in the scope: one `broken` → `broken` (stop on failure, also
      through environments that never got a run); one `active` or `waiting` → `waiting`
      (multi-parent gating, whatever the order the edges are read in); all settled → a run is
      created if an edge from an *applied* upstream passes its policy, else `skipped`.
    A downstream gets at most one run per group. The cascade never bypasses protections: a
    protected downstream still stops at `unconfirmed`, its confirm being gated independently.
    """
    if run.type != RunType.tracked or run.state != RunState.finished:
        return []
    group_id = run.run_group_id or run.id
    root = (
        await session.execute(
            select(Run)
            .where(Run.id == group_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if root is None:
        return []
    order, incoming = await _scope(session, root.environment_id)

    group_runs = (
        (
            await session.execute(
                select(Run)
                .where(_in_group(group_id), Run.type == RunType.tracked)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_env: dict[uuid.UUID, Run] = {}
    for r in sorted(group_runs, key=lambda r: r.created_at):
        by_env[r.environment_id] = r  # the latest run of an env in the group

    status: dict[uuid.UUID, str] = {}
    applied: set[uuid.UUID] = set()
    triggered: list[Run] = []
    for env_id in order:
        existing = by_env.get(env_id)
        if existing is not None:
            if existing.state == RunState.finished:
                status[env_id] = "done"
                if existing.confirmed_at is not None:
                    applied.add(env_id)
            elif existing.state in _BROKEN_STATES:
                status[env_id] = "broken"
            else:
                status[env_id] = "active"
            continue
        ups = [status.get(dep.upstream_env_id, "skipped") for dep in incoming[env_id]]
        if "broken" in ups:
            status[env_id] = "broken"
            continue
        if "active" in ups or "waiting" in ups:
            status[env_id] = "waiting"
            continue
        downstream = await session.get(Environment, env_id)
        fire = False
        if downstream is not None:
            for dep in incoming[env_id]:
                if dep.upstream_env_id in applied and await _should_trigger(
                    session, dep, downstream
                ):
                    fire = True
                    break
        if not fire:
            status[env_id] = "skipped"
            continue
        child = Run(
            environment_id=env_id,
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
        status[env_id] = "active"
        triggered.append(child)
    return triggered
