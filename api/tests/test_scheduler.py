from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def test_scheduler_lock_survives_task_commit(client: httpx.AsyncClient) -> None:
    """§7.5: the tasks commit mid-way, which hands the session's connection back to the pool. The
    advisory lock must still be released on the connection that took it — otherwise the unlock
    misses, the lock leaks on a pooled connection and later ticks silently skip the task."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import SessionLocal
    from app.scheduler.tasks import _with_lock

    key = 74999  # not one of the real task keys
    ran: list[datetime] = []

    async def task(session: AsyncSession, now: datetime) -> None:
        await session.execute(text("SELECT 1"))
        await session.commit()  # the session's connection goes back to the pool
        await session.execute(text("SELECT 1"))
        await session.commit()
        ran.append(now)

    async def held() -> int:
        async with SessionLocal() as session:
            return (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                        " AND classid = 0 AND objid = :k AND objsubid = 1"
                    ).bindparams(k=key)
                )
            ).scalar_one()

    # Several idle pooled connections, so a checkout after the task's commit gets another one
    # than the connection the session started on (QueuePool hands them out FIFO).
    warm = [SessionLocal() for _ in range(3)]
    for session in warm:
        await session.execute(text("SELECT 1"))
    for session in warm:
        await session.close()

    for _ in range(2):
        async with SessionLocal() as session:
            await _with_lock(session, key, task)
        assert await held() == 0
    assert len(ran) == 2

    # Another replica holding the key still makes this tick skip the task (runs once, §7.5).
    async with SessionLocal() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)").bindparams(k=key))
        async with SessionLocal() as session:
            await _with_lock(session, key, task)
        await other.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))
    assert len(ran) == 2

    # A task that fails its session: the task's own error surfaces and the lock is still released.
    async def failing(session: AsyncSession, now: datetime) -> None:
        await session.execute(text("SELECT 1 / 0"))

    async with SessionLocal() as session:
        with pytest.raises(DBAPIError, match="division by zero"):
            await _with_lock(session, key, failing)
    assert await held() == 0


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


async def test_worker_lost_spares_runs_awaiting_human_or_apply(client: httpx.AsyncClient) -> None:
    """Workers are disposable (§3.7): once its plan job is over, a run waits on a human
    (`unconfirmed`) or on the apply claim (`confirmed`) and no longer depends on the plan worker —
    the apply re-clones the run's commit on whichever worker claims it. Losing the plan worker must
    not fail those runs; a run whose job is still executing on it is reclaimed as before."""
    from sqlalchemy import update

    from app.config import get_settings
    from app.db import SessionLocal
    from app.models.run import Run
    from app.scheduler.tasks import detect_worker_lost

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-lost-await")
    stack = await make_stack(client, admin, "lost-await-stack")
    changes = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}

    async def _plan(env_name: str, *, finish: bool) -> str:
        env_id = await make_env(client, admin, stack, env_name, "dev")
        run = (
            await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
        ).json()
        assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
        await event(client, wh, run["id"], "phase_started", phase="planning")
        if finish:
            await event(client, wh, run["id"], "phase_finished", result=changes)
        return run["id"]

    unconfirmed = await _plan("awaiting-human", finish=True)
    confirmed = await _plan("awaiting-apply", finish=True)
    executing = await _plan("executing", finish=False)  # still planning on the worker
    # Confirm only now: a confirmed run is claimed first by its plan worker (affinity, §7.2).
    assert (
        await client.post(f"/api/v1/runs/{confirmed}/confirm", headers=admin)
    ).status_code == 200

    await _backdate([unconfirmed, confirmed, executing], claimed_ago=timedelta(hours=1))
    async with SessionLocal() as session:
        assert await detect_worker_lost(session, datetime.now(UTC)) == 1

    async def _state(run_id: str) -> str:
        return (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()["state"]

    assert await _state(executing) == "failed"
    assert await _state(unconfirmed) == "unconfirmed"
    assert await _state(confirmed) == "confirmed"

    # Past the affinity window, another worker takes the apply of the run the lost worker planned.
    affinity = get_settings().stackd_apply_affinity_seconds
    async with SessionLocal() as session:
        await session.execute(
            update(Run)
            .where(Run.id == uuid.UUID(confirmed))
            .values(confirmed_at=datetime.now(UTC) - timedelta(seconds=affinity + 60))
        )
        await session.commit()
    other = await register_worker(client, admin, "pool-lost-await-2", "w2")
    claim = await client.post("/worker/v1/jobs/claim", headers=other)
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == confirmed
    assert claim.json()["phase"] == "apply"
