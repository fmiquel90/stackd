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


def _failures(task: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("stackd_scheduler_task_failures_total", {"task": task}) or 0.0


async def test_tick_isolates_failing_and_slow_tasks(
    _database: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§7.5: a task that raises, or overruns its budget, is logged and counted — and the tick's
    other tasks still run."""
    import asyncio

    from app.scheduler import tasks

    ran: list[str] = []

    def record(name: str):  # type: ignore[no-untyped-def]
        async def task(session, now):  # type: ignore[no-untyped-def]
            ran.append(name)

        return task

    async def broken(session, now):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    async def stuck(session, now):  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)

    monkeypatch.setattr(tasks, "detect_worker_lost", broken)
    monkeypatch.setattr(tasks, "detect_drift", stuck)
    monkeypatch.setattr(tasks, "retire_keys", record("oidc_keys"))
    monkeypatch.setattr(tasks, "expire_approvals", record("rollouts"))
    monkeypatch.setattr(tasks, "archive_run_logs", record("log_archive"))
    monkeypatch.setitem(tasks._TASK_BUDGETS, "drift", 0.2)
    before = {t: _failures(t) for t in ("worker_lost", "drift")}

    await tasks._tick()

    assert ran == ["oidc_keys", "rollouts", "log_archive"]
    assert {t: _failures(t) - n for t, n in before.items()} == {"worker_lost": 1, "drift": 1}
    failed = [r for r in caplog.records if getattr(r, "event", None) == "scheduler.task_failed"]
    assert [(r.levelname, r.task) for r in failed] == [
        ("ERROR", "worker_lost"),
        ("ERROR", "drift"),
    ]
    assert failed[0].exc_info is not None  # the traceback is kept


async def test_outbox_dispatchers_never_delay_the_tick(
    _database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbound dispatchers run in loops of their own: a hung delivery doesn't hold up
    worker_lost detection, and stopping the scheduler cancels and awaits them."""
    import asyncio

    from app.scheduler import tasks

    ticks: list[datetime] = []
    three_ticks = asyncio.Event()
    cancelled: list[str] = []

    async def worker_lost(session, now):  # type: ignore[no-untyped-def]
        ticks.append(now)
        if len(ticks) == 3:
            three_ticks.set()

    async def noop(session, now):  # type: ignore[no-untyped-def]
        return 0

    def hung(name: str):  # type: ignore[no-untyped-def]
        async def drain(session, now, *, limit):  # type: ignore[no-untyped-def]
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return drain

    monkeypatch.setattr(tasks, "detect_worker_lost", worker_lost)
    for name in (
        "detect_drift",
        "retire_keys",
        "expire_approvals",
        "archive_run_logs",
        "purge_retention",
        "poll_branch_heads",
    ):
        monkeypatch.setattr(tasks, name, noop)
    monkeypatch.setattr(tasks, "dispatch_pending", hung("notifications"))
    monkeypatch.setattr(tasks, "dispatch_vcs", hung("vcs"))
    monkeypatch.setattr(tasks, "_TICK_SECONDS", 0.05)

    loop = asyncio.create_task(tasks.scheduler_loop())
    async with asyncio.timeout(10):
        await three_ticks.wait()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    assert sorted(cancelled) == ["notifications", "vcs"]


async def test_lifespan_shutdown_awaits_scheduler_then_purges_then_disposes(
    _database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from fastapi import FastAPI
    from sqlalchemy import text

    from app import main
    from app.config import get_settings
    from app.db import SessionLocal, get_engine
    from app.runs import artifacts
    from app.scheduler import tasks

    steps: list[str] = []

    async def fake_loop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # winding down its children takes a moment
            steps.append("scheduler stopped")
            raise

    async def fake_wait_for_purges() -> None:
        steps.append("purges awaited")

    monkeypatch.setattr(tasks, "scheduler_loop", fake_loop)
    monkeypatch.setattr(artifacts, "wait_for_purges", fake_wait_for_purges)
    monkeypatch.setenv("STACKD_RUN_SCHEDULER", "true")
    get_settings.cache_clear()
    try:
        async with main.lifespan(FastAPI()):
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            assert get_engine().pool.checkedin() >= 1  # type: ignore[attr-defined]
            steps.append("serving")
    finally:
        monkeypatch.delenv("STACKD_RUN_SCHEDULER")
        get_settings.cache_clear()
    assert steps == ["serving", "scheduler stopped", "purges awaited"]
    assert get_engine().pool.checkedin() == 0  # type: ignore[attr-defined]
