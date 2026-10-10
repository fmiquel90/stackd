from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def test_worker_lost_fails_active_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-lost")
    stack = await make_stack(client, admin, "lost-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")  # now in 'planning'

    # Backdate the worker heartbeat and the run claim so the detector considers it lost.
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.run import Run
    from app.models.worker import Worker
    from app.scheduler.tasks import detect_worker_lost

    old = datetime.now(UTC) - timedelta(hours=1)
    async with SessionLocal() as session:
        await session.execute(update(Worker).values(last_heartbeat_at=old))
        await session.execute(
            update(Run).where(Run.id == uuid.UUID(run["id"])).values(claimed_at=old)
        )
        await session.commit()

    async with SessionLocal() as session:
        failed = await detect_worker_lost(session, datetime.now(UTC))
    assert failed == 1

    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert detail["state"] == "failed"
    assert detail["error"] == "worker_lost"


async def _backdate(run_ids: list[str], *, claimed_ago: timedelta) -> None:
    """Mark the runs' worker long offline and set their claim `claimed_ago` in the past."""
    from sqlalchemy import select, update

    from app.db import SessionLocal
    from app.models.run import Run
    from app.models.worker import Worker

    ids = [uuid.UUID(r) for r in run_ids]
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        worker_ids = select(Run.worker_id).where(Run.id.in_(ids)).scalar_subquery()
        await session.execute(
            update(Worker)
            .where(Worker.id.in_(worker_ids))
            .values(last_heartbeat_at=now - timedelta(hours=1))
        )
        await session.execute(
            update(Run).where(Run.id.in_(ids)).values(claimed_at=now - claimed_ago)
        )
        await session.commit()


async def test_worker_lost_mutating_command_waits_apply_window(client: httpx.AsyncClient) -> None:
    """A mutating command holds apply credentials (apply OIDC role, apply-window state token) from
    its claim, so — like an applying run — it is only reclaimed after the apply budget + grace,
    when those tokens have expired. Reclaiming it at the short cutoff would hand the env to the
    next run while the lost worker can still write state (split-brain). A read-only command keeps
    the short cutoff."""
    from app.config import get_settings
    from app.db import SessionLocal
    from app.scheduler.tasks import detect_worker_lost

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-lost-cmd")
    stack = await make_stack(client, admin, "lost-cmd-stack")
    mut_env = await make_env(client, admin, stack, "mut", "dev")
    ro_env = await make_env(client, admin, stack, "ro", "dev")

    async def _start(env_id: str, command: str, args: list[str]) -> str:
        run = (
            await client.post(
                f"/api/v1/environments/{env_id}/commands",
                headers=admin,
                json={"command": command, "args": args},
            )
        ).json()
        assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
        await event(client, wh, run["id"], "phase_started", phase="running")
        return run["id"]

    mutating = await _start(mut_env, "state rm", ["aws_s3_bucket.old"])
    readonly = await _start(ro_env, "state list", [])

    async def _state(run_id: str) -> str:
        return (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()["state"]

    settings = get_settings()
    # Past the short worker-lost cutoff, still inside the apply window.
    await _backdate(
        [mutating, readonly],
        claimed_ago=timedelta(seconds=settings.stackd_worker_lost_seconds + 60),
    )
    async with SessionLocal() as session:
        assert await detect_worker_lost(session, datetime.now(UTC)) == 1
    assert await _state(readonly) == "failed"
    assert await _state(mutating) == "running"

    # Past the apply window: its state/OIDC tokens have expired → now it's reclaimed.
    await _backdate(
        [mutating], claimed_ago=timedelta(seconds=settings.apply_reclaim_window_seconds + 60)
    )
    async with SessionLocal() as session:
        assert await detect_worker_lost(session, datetime.now(UTC)) == 1
    assert await _state(mutating) == "failed"
