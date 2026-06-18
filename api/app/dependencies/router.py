from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
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
from app.models.user import User
from app.spaces import accessible_space_ids, guard_env, guard_stack

router = APIRouter(prefix="/api/v1", tags=["dependencies"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
Writer = Depends(require_role(Role.writer))


async def _guard_env_id(
    session: AsyncSession, user: User, env_id: uuid.UUID, *, min_role: Role = Role.reader
) -> Environment:
    env = await session.get(Environment, env_id)
    if env is None:
        raise ProblemException(404, "Environment not found", None)
    await guard_env(session, user, env, min_role=min_role)
    return env


async def _creates_cycle(session: AsyncSession, upstream: uuid.UUID, downstream: uuid.UUID) -> bool:
    """A new upstream→downstream edge is a cycle if downstream can already reach upstream (§3.8)."""
    if upstream == downstream:
        return True
    edges = (await session.execute(select(EnvDependency))).scalars().all()
    adj: dict[uuid.UUID, list[uuid.UUID]] = {}
    for e in edges:
        adj.setdefault(e.upstream_env_id, []).append(e.downstream_env_id)
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
    if await _creates_cycle(session, upstream_id, downstream_id):
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
    await _guard_env_id(session, user, env_id, min_role=Role.writer)
    await _guard_env_id(session, user, body.upstream_env_id, min_role=Role.writer)
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
    await _guard_env_id(session, user, env_id)
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
    await _guard_env_id(session, user, dep.downstream_env_id, min_role=Role.writer)
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
        stack = await session.get(Stack, sid)
        if stack is None:
            raise ProblemException(404, "Stack not found", None)
        await guard_stack(session, user, stack, min_role=Role.writer)
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
    created = 0
    for name, downstream in downstream_envs.items():
        upstream = upstream_envs.get(name)
        if upstream is None:
            continue
        try:
            await _create_edge(session, downstream.id, upstream.id, body.trigger_policy, [])
        except ProblemException:
            continue  # skip existing/cyclic pairs
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
        created += 1
    await session.commit()
    return {"created": created}


@router.get("/environments/{env_id}/outputs")
async def list_outputs(env_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    await _guard_env_id(session, user, env_id)
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
    envs = list((await session.execute(select(Environment))).scalars().all())
    # Scope to the caller's spaces (None = instance admin → all). An edge survives only if both of
    # its envs are visible, so the graph never reveals a cross-space dependency the user can't see.
    ids = await accessible_space_ids(session, user)
    if ids is not None:
        space_of = {
            s.id: s.space_id for s in (await session.execute(select(Stack))).scalars().all()
        }
        visible = {e.id for e in envs if space_of.get(e.stack_id) in ids}
        envs = [e for e in envs if e.id in visible]
    else:
        visible = {e.id for e in envs}
    edges = [
        e
        for e in (await session.execute(select(EnvDependency))).scalars().all()
        if e.upstream_env_id in visible and e.downstream_env_id in visible
    ]
    refs = (await session.execute(select(OutputReference))).scalars().all()
    # Aggregate references per dependency so the UI can show count + dash mocked edges (§5.4).
    by_dep: dict[uuid.UUID, dict] = {}
    for r in refs:
        agg = by_dep.setdefault(r.dependency_id, {"count": 0, "has_mock": False})
        agg["count"] += 1
        agg["has_mock"] = agg["has_mock"] or r.mock_value is not None
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
