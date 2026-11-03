from __future__ import annotations

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.dependency import EnvDependency
from app.models.environment import Environment
from app.models.space import Space
from app.models.stack import Stack
from app.models.variable_set import VariableSet
from app.models.worker import WorkerPool
from app.seed import seed_demo


async def test_seed_demo_is_idempotent(client) -> None:
    try:
        await seed_demo()
        await seed_demo()  # second run must not duplicate or raise

        async with SessionLocal() as s:
            demo = (await s.execute(select(Space).where(Space.name == "demo"))).scalar_one()
            stacks = (
                (await s.execute(select(Stack).where(Stack.space_id == demo.id))).scalars().all()
            )
            assert sorted(st.name for st in stacks) == ["demo-app", "demo-network"]

            net = next(st for st in stacks if st.name == "demo-network")
            assert net.repo_url.endswith("/demo-network")

            envs = (
                (
                    await s.execute(
                        select(Environment).where(
                            Environment.stack_id.in_([st.id for st in stacks])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(envs) == 4  # dev + prod per stack, not duplicated on the second run
            prod = next(e for e in envs if e.stack_id == net.id and e.name == "prod")
            assert prod.protected and prod.require_second_pair_of_eyes

            deps = (await s.execute(select(EnvDependency))).scalars().all()
            net_env_ids = {e.id for e in envs if e.stack_id == net.id}
            assert sum(1 for d in deps if d.upstream_env_id in net_env_ids) == 2

            pool = (
                await s.execute(select(WorkerPool).where(WorkerPool.name == "local"))
            ).scalar_one()
            assert pool.space_id == demo.id
    finally:
        # Keep the global graph clean for other tests (/graph is not space-scoped).
        async with SessionLocal() as s:
            demo = (await s.execute(select(Space).where(Space.name == "demo"))).scalar_one()
            await s.execute(delete(Stack).where(Stack.space_id == demo.id))
            await s.execute(delete(VariableSet).where(VariableSet.space_id == demo.id))
            await s.execute(delete(WorkerPool).where(WorkerPool.name == "local"))
            await s.commit()


async def test_base_seed_creates_default_space_and_tiers_only(client) -> None:
    # Production bootstrap (`python -m app.seed --base`): idempotent, never creates the demo space.
    # The suite's demo space is renamed aside (not deleted: other tests hang rows off it).
    from sqlalchemy import update

    from app.models.tier import Tier
    from app.seed import seed

    aside = "demo-aside-for-base-seed"
    async with SessionLocal() as s:
        await s.execute(update(Space).where(Space.name == "demo").values(name=aside))
        await s.commit()
    try:
        await seed(include_demo_space=False)
        await seed(include_demo_space=False)
        async with SessionLocal() as s:
            names = set((await s.execute(select(Space.name))).scalars().all())
            assert "default" in names
            assert "demo" not in names
            tiers = set((await s.execute(select(Tier.name))).scalars().all())
            assert {"dev", "staging", "prod"} <= tiers
    finally:
        async with SessionLocal() as s:
            await s.execute(update(Space).where(Space.name == aside).values(name="demo"))
            await s.commit()
