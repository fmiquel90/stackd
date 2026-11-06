from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def _applied_commit(
    env_id: str, sha: str = "applied1", plan_options: dict | None = None
) -> None:
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
                plan_options=plan_options,
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


async def test_drift_alert_follows_space_membership(client: httpx.AsyncClient) -> None:
    # Same audience as an approval request: who can apply in the env's space (effective
    # membership), not instance role/tiers. An instance approver with no membership in the space
    # must not learn about its envs; a membership-only approver (instance writer) must be told.
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.drift.service import record_drift_result
    from app.enums import DriftStatus
    from app.models.environment import Environment
    from app.models.run import Run

    admin = await login(client, "admin")
    await login(client, "alice")  # instance approver {dev, staging, prod}
    await login(client, "bob")  # instance writer
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": "drift-space"})
    assert space.status_code == 201, space.text
    space_id = space.json()["id"]
    alice_id = await _user_id("alice@dev.local")
    bob_id = await _user_id("bob@dev.local")
    await client.delete(f"/api/v1/spaces/{space_id}/members/{alice_id}", headers=admin)
    grant = await client.put(
        f"/api/v1/spaces/{space_id}/members",
        headers=admin,
        json={"user_id": str(bob_id), "role": "approver", "allowed_tiers": ["dev"]},
    )
    assert grant.status_code == 200, grant.text
    stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space_id,
            "name": "drift-space-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    assert stack.status_code == 201, stack.text
    env = await make_env(client, admin, stack.json()["id"], "dev", "dev")
    await _applied_commit(env)

    alice_before, bob_before = await _drift_notifs(alice_id), await _drift_notifs(bob_id)
    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        run = (await s.execute(select(Run).where(Run.environment_id == env_obj.id))).scalar_one()
        await record_drift_result(s, run, env_obj, DriftStatus.drifted)
        await s.commit()
    assert await _drift_notifs(bob_id) == bob_before + 1
    assert await _drift_notifs(alice_id) == alice_before


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


async def _set_drifted(env_id: str) -> None:
    from app.db import SessionLocal
    from app.enums import DriftStatus
    from app.models.environment import Environment

    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env_id))
        env_obj.drift_status = DriftStatus.drifted.value
        env_obj.drift_run_id = uuid.uuid4()
        await s.commit()


async def _drift_status(env_id: str) -> str:
    from app.db import SessionLocal
    from app.models.environment import Environment

    async with SessionLocal() as s:
        return (await s.get(Environment, uuid.UUID(env_id))).drift_status


async def _apply(client, wh, admin, env_id: str, **opts) -> None:
    changes = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
    run = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json=opts)
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=changes)
    assert (await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)).status_code == (
        200
    )
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished", result={"outputs": {}})
    final = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert final["state"] == "finished", final


async def test_targeted_apply_does_not_clear_drift(client: httpx.AsyncClient) -> None:
    # §19.4: a `-target` apply reconciled only its targets — the rest may still drift. A replace
    # plan applies the whole configuration: it clears drift like any apply.
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "drift-targeted-pool")
    stack = await make_stack(client, admin, "drift-targeted")
    env = await make_env(client, admin, stack, "dev", "dev")

    await _set_drifted(env)
    await _apply(client, wh, admin, env, targets=["aws_s3_bucket.a"])
    assert await _drift_status(env) == "drifted"

    await _apply(client, wh, admin, env, replace=["aws_instance.web"])
    assert await _drift_status(env) == "in_sync"


async def test_targeted_apply_after_apply_failure_keeps_drift(client: httpx.AsyncClient) -> None:
    # The after_apply-hook failure path records outputs and clears drift for a full apply (§8.3);
    # a targeted one keeps the drift marker there too.
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "drift-targeted-fail-pool")
    stack = await make_stack(client, admin, "drift-targeted-fail")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _set_drifted(env)

    changes = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
    run = (
        await client.post(
            f"/api/v1/environments/{env}/runs", headers=admin, json={"targets": ["a.b"]}
        )
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=changes)
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)
    failed = await event(
        client, wh, run["id"], "job_failed", phase="after_apply", result={"outputs": {}}
    )
    assert failed.status_code == 200, failed.text
    assert await _drift_status(env) == "drifted"


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


async def test_targeted_apply_is_not_the_applied_commit(client: httpx.AsyncClient) -> None:
    """§3.5: a targeted apply deployed only part of its commit — a drift plan refreshes the last
    full apply's commit, and git staleness still compares the branch head against it."""
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.drift.service import detect_drift
    from app.models.environment import Environment

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "drift-targeted-baseline")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _applied_commit(env, "full1")
    await _applied_commit(env, "targ2", plan_options={"targets": ["terraform_data.a"]})
    async with SessionLocal() as s:
        await s.execute(
            update(Environment).where(Environment.id == uuid.UUID(env)).values(head_sha="targ2")
        )
        await s.commit()

    r = await client.get(f"/api/v1/environments/{env}", headers=admin)
    assert r.json()["stale"] is True  # head = the targeted commit, but the env is not fully there

    async with SessionLocal() as s:
        assert await detect_drift(s, datetime.now(UTC)) >= 1
    assert (await _drift_run(env))["commit_sha"] == "full1"
