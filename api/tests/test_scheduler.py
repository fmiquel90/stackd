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
