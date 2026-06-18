from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def _applied_commit(env_id: str, sha: str = "applied1") -> None:
    """Seed a finished tracked run so the env has an applied commit to refresh against."""
    from app.db import SessionLocal
    from app.enums import RunState, RunType, TriggeredBy
    from app.models.run import Run

    async with SessionLocal() as s:
        s.add(
            Run(
                environment_id=uuid.UUID(env_id),
                type=RunType.tracked,
                state=RunState.finished,
                triggered_by=TriggeredBy.manual,
                commit_sha=sha,
                finished_at=datetime.now(UTC),
            )
        )
        await s.commit()


async def _drift_run(env_id: str) -> dict:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as s:
        run = (
            await s.execute(
                select(Run).where(Run.environment_id == uuid.UUID(env_id), Run.is_drift.is_(True))
            )
        ).scalar_one()
        return {"id": str(run.id), "commit_sha": run.commit_sha, "type": run.type.value}


async def _user_id(email: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.user import User

    async with SessionLocal() as s:
        return (await s.execute(select(User.id).where(User.email == email))).scalar_one()


async def _drift_notifs(user_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models.user_notification import UserNotification

    async with SessionLocal() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(UserNotification)
                .where(
                    UserNotification.user_id == user_id,
                    UserNotification.kind == "drift_detected",
                )
            )
        ).scalar_one()


async def test_detect_drift_enqueues_refresh_run(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.drift.service import detect_drift

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "drift-detect")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _applied_commit(env)

    async with SessionLocal() as s:
        assert await detect_drift(s, datetime.now(UTC)) == 1
    run = await _drift_run(env)
    assert run["type"] == "proposed" and run["commit_sha"] == "applied1"

    # Idempotent within the interval: last_drift_checked_at was just bumped → no-op.
    async with SessionLocal() as s:
        assert await detect_drift(s, datetime.now(UTC)) == 0


async def test_no_drift_without_applied_commit(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.drift.service import detect_drift

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "drift-noapply")
    await make_env(client, admin, stack, "dev", "dev")

    # No env in the DB has an applied commit (runs are cleared per test) → nothing enqueued.
    async with SessionLocal() as s:
        assert await detect_drift(s, datetime.now(UTC)) == 0


async def test_drift_run_flips_env_and_notifies_once(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.drift.service import detect_drift, record_drift_result
    from app.enums import DriftStatus
    from app.models.environment import Environment
    from app.models.run import Run

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "drift-pool")
    stack = await make_stack(client, admin, "drift-flip")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _applied_commit(env)

    async with SessionLocal() as s:
        await detect_drift(s, datetime.now(UTC))
    run = await _drift_run(env)

    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(
        client,
        wh,
        run["id"],
        "phase_finished",
        result={"has_changes": True, "summary": {"add": 0, "change": 1, "destroy": 0}},
    )

    admin_id = await _user_id("admin@dev.local")
    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        finished = await s.get(Run, uuid.UUID(run["id"]))
    assert env_obj.drift_status == DriftStatus.drifted.value
    assert str(env_obj.drift_run_id) == run["id"]
    assert finished.state.value == "finished"  # the drift run is still terminal
    assert await _drift_notifs(admin_id) == 1

    # Debounce: a second drifted result while already drifted emits no new notification.
    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        again = await s.get(Run, uuid.UUID(run["id"]))
        await record_drift_result(s, again, env_obj, DriftStatus.drifted)
        await s.commit()
    assert await _drift_notifs(admin_id) == 1


async def test_clear_drift_on_apply(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.drift.service import clear_drift
    from app.enums import DriftStatus
    from app.models.environment import Environment

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "drift-clear")
    env = await make_env(client, admin, stack, "dev", "dev")

    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        env_obj.drift_status = DriftStatus.drifted.value
        env_obj.drift_run_id = uuid.uuid4()
        await s.commit()

    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        await clear_drift(s, env_obj, datetime.now(UTC))
        await s.commit()

    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
    assert env_obj.drift_status == DriftStatus.in_sync.value
    assert env_obj.drift_run_id is None


async def test_claim_prefers_user_run_over_drift(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.enums import RunState, RunType, TriggeredBy
    from app.models.run import Run

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "drift-prio")
    stack = await make_stack(client, admin, "drift-prio")
    env = await make_env(client, admin, stack, "dev", "dev")

    async with SessionLocal() as s:
        drift = Run(
            environment_id=uuid.UUID(env),
            type=RunType.proposed,
            state=RunState.queued,
            triggered_by=TriggeredBy.schedule,
            commit_sha="c1",
            is_drift=True,
        )
        user = Run(
            environment_id=uuid.UUID(env),
            type=RunType.tracked,
            state=RunState.queued,
            triggered_by=TriggeredBy.manual,
            commit_sha="c2",
        )
        s.add_all([drift, user])
        await s.commit()
        user_id = str(user.id)

    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == user_id  # the user run wins despite the drift run being older
