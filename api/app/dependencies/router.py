from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth.deps import CurrentUser, require_role
from app.db import get_session
from app.dependencies.schemas import DependencyCreate, LinkByNameIn
from app.enums import AuditActorKind, Role
from app.errors import ProblemException
from app.models.dependency import EnvDependency, EnvOutput, OutputReference
from app.models.environment import Environment
from app.models.stack import Stack
from app.spaces import accessible_space_ids, guard_env_id, guard_stack_id

router = APIRouter(prefix="/api/v1", tags=["dependencies"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Writer = Depends(require_role(Role.writer))


Adjacency = dict[uuid.UUID, set[uuid.UUID]]


async def _load_adjacency(session: AsyncSession) -> Adjacency:
    """upstream → downstreams over the whole graph (ids only): a cycle can cross any space."""
    adj: Adjacency = {}
    rows = await session.execute(
        select(EnvDependency.upstream_env_id, EnvDependency.downstream_env_id)
    )
    for up, down in rows:
        adj.setdefault(up, set()).add(down)
    return adj


def _creates_cycle(adj: Adjacency, upstream: uuid.UUID, downstream: uuid.UUID) -> bool:
    """A new upstream→downstream edge is a cycle if downstream can already reach upstream (§3.8)."""
    if upstream == downstream:
        return True
    seen: set[uuid.UUID] = set()
    stack = [downstream]
    while stack:
        node = stack.pop()
        if node == upstream:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, []))
    return False


async def _create_edge(
    session: AsyncSession, downstream_id: uuid.UUID, upstream_id: uuid.UUID, policy, refs
) -> EnvDependency:  # type: ignore[no-untyped-def]
    if _creates_cycle(await _load_adjacency(session), upstream_id, downstream_id):
        raise ProblemException(422, "Dependency cycle", "This edge would create a cycle.")
    dep = EnvDependency(
        upstream_env_id=upstream_id, downstream_env_id=downstream_id, trigger_policy=policy
    )
    session.add(dep)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(409, "Dependency exists", "This edge already exists.") from exc
    for r in refs:
        session.add(
            OutputReference(
                dependency_id=dep.id,
                output_name=r.output_name,
                input_name=r.input_name,
                mock_value=r.mock_value,
            )
        )
    return dep


@router.post("/environments/{env_id}/dependencies", status_code=201, dependencies=[Writer])
async def create_dependency(
    env_id: uuid.UUID, body: DependencyCreate, user: CurrentUser, session: DbSession
) -> dict:
    # Both ends must be in spaces the user can write — prevents wiring a cross-space dependency.
    await guard_env_id(session, user, env_id, min_role=Role.writer)
    await guard_env_id(session, user, body.upstream_env_id, min_role=Role.writer)
    dep = await _create_edge(
        session, env_id, body.upstream_env_id, body.trigger_policy, body.references
    )
    await record_audit(
        session,
        action="dependency.created",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="environment",
        target_id=env_id,
        context={"upstream_env_id": str(body.upstream_env_id), "references": len(body.references)},
    )
    await session.commit()
    return {"id": str(dep.id)}


@router.get("/environments/{env_id}/dependencies")
async def list_dependencies(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    await guard_env_id(session, user, env_id)
    deps = (
        (
            await session.execute(
                select(EnvDependency).where(EnvDependency.downstream_env_id == env_id)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for d in deps:
        refs = (
            (
                await session.execute(
                    select(OutputReference).where(OutputReference.dependency_id == d.id)
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "id": str(d.id),
                "upstream_env_id": str(d.upstream_env_id),
                "trigger_policy": d.trigger_policy.value,
                "references": [
                    {
                        "output_name": r.output_name,
                        "input_name": r.input_name,
                        "has_mock": r.mock_value is not None,
                    }
                    for r in refs
                ],
            }
        )
    return out


@router.delete("/dependencies/{dep_id}", status_code=204, dependencies=[Writer])
async def delete_dependency(dep_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    dep = await session.get(EnvDependency, dep_id)
    if dep is None:
        raise ProblemException(404, "Dependency not found", None)
    await guard_env_id(session, user, dep.downstream_env_id, min_role=Role.writer)
    await session.delete(dep)
    await record_audit(
        session,
        action="dependency.deleted",
        actor_kind=AuditActorKind.user,
        actor_id=user.id,
        actor_email=user.email,
        target_kind="dependency",
        target_id=dep_id,
    )
    await session.commit()


@router.post("/stacks/{stack_id}/dependencies/link-by-name", dependencies=[Writer])
async def link_by_name(
    stack_id: uuid.UUID, body: LinkByNameIn, user: CurrentUser, session: DbSession
) -> dict:
    """Create edges between homonymous environments of two stacks (SPECS §3.8)."""
    for sid in (stack_id, body.upstream_stack_id):
        await guard_stack_id(session, user, sid, min_role=Role.writer)
    downstream_envs = {
        e.name: e
        for e in (
            await session.execute(select(Environment).where(Environment.stack_id == stack_id))
        )
        .scalars()
        .all()
    }
    upstream_envs = {
        e.name: e
        for e in (
            await session.execute(
                select(Environment).where(Environment.stack_id == body.upstream_stack_id)
            )
        )
        .scalars()
        .all()
    }
    # Loaded once and kept current as edges are added, so cycle detection also sees this request's
    # own edges without re-reading the whole table per pair.
    adj = await _load_adjacency(session)
    created = 0
    for name, downstream in downstream_envs.items():
        upstream = upstream_envs.get(name)
        if upstream is None or downstream.id in adj.get(upstream.id, ()):
            continue  # no homonym, or already linked
        if _creates_cycle(adj, upstream.id, downstream.id):
            continue
        # SAVEPOINT per edge: a concurrent insert of the same pair only drops this edge (and its
        # audit row, staged in the same savepoint), not the ones already created in the request.
        try:
            async with session.begin_nested():
                session.add(
                    EnvDependency(
                        upstream_env_id=upstream.id,
                        downstream_env_id=downstream.id,
                        trigger_policy=body.trigger_policy,
                    )
                )
                await record_audit(
                    session,
                    action="dependency.created",
                    actor_kind=AuditActorKind.user,
                    actor_id=user.id,
                    actor_email=user.email,
                    target_kind="environment",
                    target_id=downstream.id,
                    context={"upstream_env_id": str(upstream.id), "via": "link-by-name"},
                )
                await session.flush()
        except IntegrityError:
            continue
        adj.setdefault(upstream.id, set()).add(downstream.id)
        created += 1
    await session.commit()
    return {"created": created}


@router.get("/environments/{env_id}/outputs")
async def list_outputs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    await guard_env_id(session, user, env_id)
    rows = (
        (await session.execute(select(EnvOutput).where(EnvOutput.environment_id == env_id)))
        .scalars()
        .all()
    )
    return [
        {"name": o.name, "value": None if o.sensitive else o.value, "sensitive": o.sensitive}
        for o in rows
    ]


@router.get("/graph")
async def graph(user: CurrentUser, session: DbSession) -> dict:
    # Scope to the caller's spaces (None = instance admin → all). An edge survives only if both of
    # its envs are visible, so the graph never reveals a cross-space dependency the user can't see.
    ids = await accessible_space_ids(session, user)
    q = select(Environment)
    edge_q = select(EnvDependency)
    if ids is not None:
        q = q.join(Stack, Stack.id == Environment.stack_id).where(Stack.space_id.in_(ids))
        # Filtered in SQL (subquery, not an id list) so a small-space reader never pulls the
        # instance-wide edge set.
        visible = q.with_only_columns(Environment.id)
        edge_q = edge_q.where(
            EnvDependency.upstream_env_id.in_(visible),
            EnvDependency.downstream_env_id.in_(visible),
        )
    envs = list((await session.execute(q)).scalars().all())
    edges = list((await session.execute(edge_q)).scalars().all())
    # Aggregate references per kept dependency so the UI can show count + dash mocked edges (§5.4).
    by_dep: dict[uuid.UUID, dict] = {
        dep_id: {"count": count, "has_mock": has_mock}
        for dep_id, count, has_mock in await session.execute(
            select(
                OutputReference.dependency_id,
                func.count(),
                func.bool_or(OutputReference.mock_value.is_not(None)),
            )
            .where(OutputReference.dependency_id.in_(edge_q.with_only_columns(EnvDependency.id)))
            .group_by(OutputReference.dependency_id)
        )
    }
    return {
        "nodes": [
            {"id": str(e.id), "name": e.name, "stack_id": str(e.stack_id), "tier": e.tier}
            for e in envs
        ],
        "edges": [
            {
                "id": str(e.id),
                "upstream": str(e.upstream_env_id),
                "downstream": str(e.downstream_env_id),
                "policy": e.trigger_policy.value,
                "references": by_dep.get(e.id, {}).get("count", 0),
                "has_mock": by_dep.get(e.id, {}).get("has_mock", False),
            }
            for e in edges
        ],
    }
