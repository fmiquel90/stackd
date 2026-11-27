"""Reaping runs whose job no worker executes (SPECS §3.7, §7.1, §7.5): a live worker that does not
list the job, a stop request answered `running_here: false`, an API outage that must not pass for
dead workers, and downward commands delivered again until they get a result."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def _plan(client: httpx.AsyncClient, admin: dict, wh: dict, env_id: str) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    return str(run["id"])


async def _claimed_ago(run_id: str, ago: timedelta) -> None:
    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as session:
        await session.execute(
            update(Run)
            .where(Run.id == uuid.UUID(run_id))
            .values(claimed_at=datetime.now(UTC) - ago)
        )
        await session.commit()


async def _beat(client: httpx.AsyncClient, wh: dict, job_ids: list[str] | None) -> list[dict]:
    body: dict = {"in_flight": len(job_ids or []), "capacity": 2}
    if job_ids is not None:
        body["job_ids"] = job_ids
    resp = await client.post("/worker/v1/heartbeat", headers=wh, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["commands"]


async def _reap() -> int:
    from app.db import SessionLocal
    from app.scheduler.tasks import detect_worker_lost

    async with SessionLocal() as session:
        return await detect_worker_lost(session, datetime.now(UTC))


async def _run(client: httpx.AsyncClient, h: dict, run_id: str) -> dict:
    return (await client.get(f"/api/v1/runs/{run_id}", headers=h)).json()


async def _audit_reasons(run_id: str) -> list[str | None]:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT context->>'reason' FROM audit_events"
                " WHERE target_id = CAST(:r AS uuid) AND action = 'run.apply_failed'"
            ).bindparams(r=run_id)
        )
        return [r[0] for r in rows]


def _lost_seconds() -> int:
    from app.config import get_settings

    return get_settings().stackd_worker_lost_seconds


async def test_job_left_out_by_a_live_worker_is_failed(client: httpx.AsyncClient) -> None:
    """The claim response was lost: the worker heartbeats fine but never runs the job. Before,
    the run stayed `planning` forever and blocked its env."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-unlisted")
    stack = await make_stack(client, admin, "reap-unlisted")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _plan(client, admin, wh, env_id)

    # A fresh claim is never counted: its response may still be on its way to the worker.
    await _beat(client, wh, [])
    await _beat(client, wh, [])
    assert await _reap() == 0

    await _claimed_ago(run_id, timedelta(seconds=_lost_seconds() + 60))
    await _beat(client, wh, [])
    assert await _reap() == 0  # one beat could have raced the job's start
    await _beat(client, wh, [])
    assert await _reap() == 1

    run = await _run(client, admin, run_id)
    assert run["state"] == "failed"
    assert run["error"] == "job_not_running"
    assert await _audit_reasons(run_id) == ["job_not_running"]
    workers = (await client.get("/api/v1/workers", headers=admin)).json()
    assert {w["status"] for w in workers if w["id"] == run["worker_id"]} == {"idle"}
    # The env is free again.
    nxt = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    assert nxt.status_code in (200, 201), nxt.text


async def test_listed_job_and_old_agent_are_spared(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-listed")
    stack = await make_stack(client, admin, "reap-listed")
    listed = await _plan(client, admin, wh, await make_env(client, admin, stack, "a", "dev"))
    await _claimed_ago(listed, timedelta(hours=1))

    # Left out once, then listed again: the count starts over.
    await _beat(client, wh, [])
    await _beat(client, wh, [listed])
    await _beat(client, wh, [])
    assert await _reap() == 0
    await _beat(client, wh, [listed])
    await _beat(client, wh, [listed])
    assert await _reap() == 0
    assert (await _run(client, admin, listed))["state"] == "planning"

    # An agent that predates the listing sends no job ids: nothing is counted.
    old = await register_worker(client, admin, "pool-reap-old")
    legacy = await _plan(client, admin, old, await make_env(client, admin, stack, "b", "dev"))
    await _claimed_ago(legacy, timedelta(hours=1))
    for _ in range(3):
        await _beat(client, old, None)
    assert await _reap() == 0
    assert (await _run(client, admin, legacy))["state"] == "planning"


async def test_unlisted_job_holding_apply_credentials_waits_for_the_apply_window(
    client: httpx.AsyncClient,
) -> None:
    """A mutating command holds apply credentials: even left out by its worker, it is only failed
    once its state/OIDC tokens have expired (§4.2) — two workers sharing a name, say, must not
    let a second run write the env's state while the first still can."""
    from app.config import get_settings

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-apply")
    stack = await make_stack(client, admin, "reap-apply")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/commands",
            headers=admin,
            json={"command": "state rm", "args": ["aws_s3_bucket.old"]},
        )
    ).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="running")

    await _claimed_ago(run["id"], timedelta(seconds=_lost_seconds() + 60))
    await _beat(client, wh, [])
    await _beat(client, wh, [])
    assert await _reap() == 0
    assert (await _run(client, admin, run["id"]))["state"] == "running"

    window = get_settings().apply_reclaim_window_seconds
    await _claimed_ago(run["id"], timedelta(seconds=window + 60))
    assert await _reap() == 1
    assert (await _run(client, admin, run["id"]))["error"] == "job_not_running"


async def test_stop_answered_not_running_here_ends_the_run(client: httpx.AsyncClient) -> None:
    """Before, the worker's `running_here: false` was just stored: the run stayed executing."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-cancel")
    stack = await make_stack(client, admin, "reap-cancel")

    async def _stop(run_id: str) -> None:
        assert (
            await client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)
        ).status_code == 200
        cmds = [c for c in await _beat(client, wh, None) if c["type"] == "cancel_job"]
        assert [c["payload"] for c in cmds] == [{"job_id": run_id}]
        res = await client.post(
            f"/worker/v1/commands/{cmds[0]['id']}/result",
            headers=wh,
            json={"result": {"job_id": run_id, "running_here": False}},
        )
        assert res.status_code == 200, res.text

    # Claimed just now: the claim response may still be in flight (the cancel beat it) — kept.
    fresh = await _plan(client, admin, wh, await make_env(client, admin, stack, "a", "dev"))
    await _stop(fresh)
    assert (await _run(client, admin, fresh))["state"] == "planning"

    old = await _plan(client, admin, wh, await make_env(client, admin, stack, "b", "dev"))
    await _claimed_ago(old, timedelta(seconds=_lost_seconds() + 60))
    await _stop(old)
    run = await _run(client, admin, old)
    assert (run["state"], run["error"]) == ("failed", "job_not_running")
    assert await _audit_reasons(old) == ["job_not_running"]


async def test_one_run_that_cannot_be_failed_does_not_hold_back_the_others(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before, every lost run was failed in one transaction: one error (a 409 concurrent
    modification) rolled them all back, tick after tick."""
    from app.errors import ProblemException
    from app.workers import reaping

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-batch")
    stack = await make_stack(client, admin, "reap-batch")
    runs = [
        await _plan(client, admin, wh, await make_env(client, admin, stack, name, "dev"))
        for name in ("a", "b", "c")
    ]
    for run_id in runs:
        await _claimed_ago(run_id, timedelta(hours=1))
    from app.db import SessionLocal
    from app.models.run import Run
    from app.models.worker import Worker

    async with SessionLocal() as session:
        wid = select(Run.worker_id).where(Run.id == uuid.UUID(runs[0])).scalar_subquery()
        await session.execute(
            update(Worker)
            .where(Worker.id == wid)
            .values(last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    real = reaping.transition
    stuck = uuid.UUID(runs[1])

    async def flaky(session, run, *a, **k):  # type: ignore[no-untyped-def]
        if run.id == stuck:
            raise ProblemException(409, "Concurrent modification", "Run state changed underneath.")
        await real(session, run, *a, **k)

    monkeypatch.setattr(reaping, "transition", flaky)
    assert await _reap() == 2
    states = [(await _run(client, admin, r))["state"] for r in runs]
    assert states == ["failed", "planning", "failed"]

    monkeypatch.setattr(reaping, "transition", real)
    assert await _reap() == 1  # retried on the next tick
    assert (await _run(client, admin, runs[1]))["error"] == "worker_lost"


async def test_api_outage_is_not_counted_as_worker_silence(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before, the first tick after an API/DB outage longer than the offline threshold marked every
    worker offline and failed their plans, although the workers were healthy: their heartbeats
    could not reach the API either."""
    from app.models.worker import Worker
    from app.scheduler import tasks

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reap-outage")
    stack = await make_stack(client, admin, "reap-outage")
    run_id = await _plan(client, admin, wh, await make_env(client, admin, stack, "dev", "dev"))
    await _claimed_ago(run_id, timedelta(hours=1))

    from app.db import SessionLocal
    from app.models.run import Run

    async def _last_beat(at: datetime) -> None:
        async with SessionLocal() as session:
            wid = select(Run.worker_id).where(Run.id == uuid.UUID(run_id)).scalar_subquery()
            await session.execute(
                update(Worker).where(Worker.id == wid).values(last_heartbeat_at=at)
            )
            await session.commit()

    async def _status() -> str:
        async with SessionLocal() as session:
            wid = select(Run.worker_id).where(Run.id == uuid.UUID(run_id)).scalar_subquery()
            return (
                await session.execute(select(Worker.status).where(Worker.id == wid))
            ).scalar_one()

    async def _tick(now: datetime) -> int:
        async with SessionLocal() as session:
            return await tasks.detect_worker_lost(session, now)

    # The API process starts after a 10-minute outage: the worker's last beat is that old.
    clock = tasks._ApiClock()
    monkeypatch.setattr(tasks, "_api_clock", clock)
    start = datetime.now(UTC)
    await _last_beat(start - timedelta(minutes=10))
    assert await _tick(start) == 0
    assert await _tick(start + timedelta(seconds=10)) == 0
    assert await _status() != "offline"

    # The worker beats again: nothing happens. It then dies: once a full offline period has gone
    # by with the API up, it is marked offline and its run failed.
    await _last_beat(start + timedelta(seconds=15))
    t = start + timedelta(seconds=20)
    while t < start + timedelta(seconds=70):
        assert await _tick(t) == 0
        t += timedelta(seconds=10)
    assert await _tick(start + timedelta(seconds=78)) == 1
    assert (await _run(client, admin, run_id))["error"] == "worker_lost"

    # A DB outage in a running process (ticks failing for 5 min): a worker that beat shortly before
    # it is not offline when the ticks resume.
    run2 = await _plan(client, admin, wh, await make_env(client, admin, stack, "b", "dev"))
    await _claimed_ago(run2, timedelta(hours=1))
    resumed = start + timedelta(seconds=78 + 300)
    await _last_beat(start + timedelta(seconds=75))
    assert await _tick(resumed) == 0
    assert await _status() != "offline"


def test_api_clock_discounts_only_the_last_outage() -> None:
    """Slow ticks must never postpone reaping for good: only the last stretch is discounted."""
    from app.scheduler.tasks import _ApiClock

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    clock = _ApiClock()
    clock.observe(t0)  # process start: everything before is unknown
    assert clock.silence(t0 - timedelta(hours=1), t0 + timedelta(seconds=30)) == timedelta(
        seconds=30
    )
    clock.observe(t0 + timedelta(seconds=10))
    clock.observe(t0 + timedelta(seconds=40))  # a 30 s gap
    beat = t0 + timedelta(seconds=5)
    assert clock.silence(beat, t0 + timedelta(seconds=40)) == timedelta(seconds=5)
    clock.observe(t0 + timedelta(seconds=50))
    clock.observe(t0 + timedelta(seconds=75))  # another one: the previous is now counted
    assert clock.silence(beat, t0 + timedelta(seconds=75)) == timedelta(seconds=45)


async def test_command_without_result_is_delivered_again_then_failed(
    client: httpx.AsyncClient,
) -> None:
    """Before, a command was marked `sent` when returned — lost with its response, or with a
    result post that failed, for good (at most once)."""
    from app.db import SessionLocal
    from app.models.worker_command import WorkerCommand

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-redeliver", "agent-redeliver")
    workers = (await client.get("/api/v1/workers", headers=admin)).json()
    worker_id = next(w["id"] for w in workers if w["name"] == "agent-redeliver")
    cid = (await client.post(f"/api/v1/workers/{worker_id}/diagnostics", headers=admin)).json()[
        "command_id"
    ]

    async def _age() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(WorkerCommand)
                .where(WorkerCommand.id == uuid.UUID(cid))
                .values(sent_at=datetime.now(UTC) - timedelta(minutes=5))
            )
            await session.commit()

    def _ids(cmds: list[dict]) -> list[str]:
        return [c["id"] for c in cmds]

    assert _ids(await _beat(client, wh, None)) == [cid]
    assert _ids(await _beat(client, wh, None)) == []  # not before the redelivery delay
    for _ in range(2):
        await _age()
        assert _ids(await _beat(client, wh, None)) == [cid]
    await _age()
    assert _ids(await _beat(client, wh, None)) == []
    latest = (await client.get(f"/api/v1/workers/{worker_id}/diagnostics", headers=admin)).json()
    assert latest["status"] == "failed"

    # A command that got its result is never delivered again.
    cid = (await client.post(f"/api/v1/workers/{worker_id}/diagnostics", headers=admin)).json()[
        "command_id"
    ]
    assert _ids(await _beat(client, wh, None)) == [cid]
    await client.post(f"/worker/v1/commands/{cid}/result", headers=wh, json={"result": {}})
    await _age()
    assert _ids(await _beat(client, wh, None)) == []
