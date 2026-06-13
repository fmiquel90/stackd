from __future__ import annotations

import asyncio
import os
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.enums import AttachmentTarget, Tier, TriggerPolicy, VariableKind
from app.models.dependency import EnvDependency, OutputReference
from app.models.environment import Environment
from app.models.space import Space
from app.models.stack import Stack
from app.models.variable import Variable
from app.models.variable_set import VariableSet, VariableSetAttachment
from app.models.worker import WorkerPool
from app.security import hash_token

# Path the seed writes the pool token to; shared with the worker via the .dev mount (DEV §7).
TOKEN_PATH = os.environ.get("STACKD_SEED_TOKEN_PATH", "/stackd-dev/pool.token")
# Worker mount view of the fixture repos created by scripts/seed-fixtures.sh.
REPO_BASE = os.environ.get("STACKD_SEED_REPO_BASE", "file:///stackd-dev/repos")


async def seed() -> None:
    """Idempotent base data the app (and tests) require: the spaces (SPECS §3.0)."""
    async with SessionLocal() as session:
        for name, desc in (("default", "Default space"), ("demo", "Demo space")):
            exists = (
                await session.execute(select(Space).where(Space.name == name))
            ).scalar_one_or_none()
            if exists is None:
                session.add(Space(name=name, description=desc))
        await session.commit()
    print("seed: spaces ensured (default, demo)")


async def _get(session: AsyncSession, model, **filters):  # type: ignore[no-untyped-def]
    stmt = select(model)
    for k, v in filters.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _ensure(session: AsyncSession, model, *, match: dict, defaults: dict):  # type: ignore[no-untyped-def]
    obj = await _get(session, model, **match)
    if obj is None:
        obj = model(**match, **defaults)
        session.add(obj)
        await session.flush()
    return obj


async def _ensure_dependency(
    session: AsyncSession, upstream: Environment, downstream: Environment, *, mock: str
) -> None:
    dep = await _get(
        session,
        EnvDependency,
        upstream_env_id=upstream.id,
        downstream_env_id=downstream.id,
    )
    if dep is None:
        dep = EnvDependency(
            upstream_env_id=upstream.id,
            downstream_env_id=downstream.id,
            trigger_policy=TriggerPolicy.on_output_change,
        )
        session.add(dep)
        await session.flush()
    ref = await _get(session, OutputReference, dependency_id=dep.id, output_name="network_name")
    if ref is None:
        session.add(
            OutputReference(
                dependency_id=dep.id,
                output_name="network_name",
                input_name="network_name",
                mock_value=mock,
            )
        )


def _token_fs_state() -> tuple[bool, bool]:
    """(can_write, have_file) for the shared token path — absent without the mount (e.g. tests)."""
    return os.path.isdir(os.path.dirname(TOKEN_PATH)), os.path.exists(TOKEN_PATH)


def _write_token_file(token: str) -> None:
    with open(TOKEN_PATH, "w") as f:
        f.write(token)
    os.chmod(TOKEN_PATH, 0o600)
    print(f"seed: wrote worker pool token to {TOKEN_PATH}")


async def _ensure_pool_token(session: AsyncSession, space: Space) -> None:
    """Ensure worker pool 'local' exists and its plaintext token is on disk for the worker.

    Only the hash is stored, so if the token file is missing (e.g. .dev was wiped) we must rotate:
    drop the pool (cascades its workers) and recreate it with a fresh token."""
    pool = await _get(session, WorkerPool, name="local")
    can_write, have_file = _token_fs_state()

    # Token already provisioned, or we're in an env without the shared mount (e.g. tests).
    if pool is not None and (have_file or not can_write):
        return

    if pool is not None:
        await session.delete(pool)
        await session.flush()

    token = secrets.token_urlsafe(32)
    session.add(
        WorkerPool(space_id=space.id, name="local", labels=None, token_hash=hash_token(token))
    )
    if can_write:
        _write_token_file(token)


async def seed_demo() -> None:
    """Idempotent demo graph (DEV §7): variable sets, demo-network + demo-app stacks with dev/prod
    environments, the inter-env dependency with a mock output, and the 'local' worker pool token.
    The fixture git repos themselves are created by scripts/seed-fixtures.sh."""
    async with SessionLocal() as session:
        demo = await _get(session, Space, name="demo")
        assert demo is not None, "run seed() first"

        # --- variable sets ---
        common = await _ensure(
            session,
            VariableSet,
            match={"space_id": demo.id, "name": "common"},
            defaults={"description": "Org-wide defaults", "auto_attach": True},
        )
        region = await _ensure(
            session,
            VariableSet,
            match={"space_id": demo.id, "name": "region-local"},
            defaults={"description": "Local region", "auto_attach": False},
        )
        if await _get(session, Variable, variable_set_id=common.id, name="org") is None:
            session.add(
                Variable(
                    variable_set_id=common.id, kind=VariableKind.terraform, name="org", value="demo"
                )
            )
        if await _get(session, Variable, variable_set_id=region.id, name="region") is None:
            session.add(
                Variable(
                    variable_set_id=region.id,
                    kind=VariableKind.terraform,
                    name="region",
                    value="local-1",
                )
            )

        # --- stacks + environments ---
        stacks: dict[str, Stack] = {}
        envs: dict[str, Environment] = {}
        for sname in ("demo-network", "demo-app"):
            stack = await _ensure(
                session,
                Stack,
                match={"space_id": demo.id, "name": sname},
                defaults={
                    "repo_url": f"{REPO_BASE}/{sname}",
                    "tool_version": "1.12.0",
                    "project_root": ".",
                },
            )
            stacks[sname] = stack
            # region-local set is not auto-attach → attach it explicitly to each stack.
            if (
                await _get(
                    session,
                    VariableSetAttachment,
                    variable_set_id=region.id,
                    target_kind=AttachmentTarget.stack,
                    target_id=stack.id,
                )
                is None
            ):
                session.add(
                    VariableSetAttachment(
                        variable_set_id=region.id,
                        target_kind=AttachmentTarget.stack,
                        target_id=stack.id,
                        priority=0,
                    )
                )
            # dev (open) + prod (protected, 4-eyes). Local state in dev (no S3/Garage coupling).
            envs[f"{sname}/dev"] = await _ensure(
                session,
                Environment,
                match={"stack_id": stack.id, "name": "dev"},
                defaults={"tier": Tier.dev, "branch": "main", "managed_state": False},
            )
            envs[f"{sname}/prod"] = await _ensure(
                session,
                Environment,
                match={"stack_id": stack.id, "name": "prod"},
                defaults={
                    "tier": Tier.prod,
                    "branch": "main",
                    "protected": True,
                    "require_second_pair_of_eyes": True,
                    "managed_state": False,
                },
            )

        # --- dependencies: network → app, per environment, with a mock for bootstrap ---
        await _ensure_dependency(
            session, envs["demo-network/dev"], envs["demo-app/dev"], mock="mock-network"
        )
        await _ensure_dependency(
            session, envs["demo-network/prod"], envs["demo-app/prod"], mock="mock-network"
        )

        await _ensure_pool_token(session, demo)
        await session.commit()
    print("seed: demo graph ensured (stacks, envs, deps, worker pool)")


if __name__ == "__main__":

    async def _main() -> None:
        await seed()
        await seed_demo()

    asyncio.run(_main())
