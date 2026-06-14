from __future__ import annotations

import asyncio
import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _drive_to_unconfirmed(client, wh, env_id, admin) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    assert claim.json()["phase"] == "plan"
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return run["id"]


async def test_full_run_lifecycle(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-lifecycle")
    stack = await make_stack(client, admin, "life-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "unconfirmed"
    assert run["plan_summary"]["add"] == 1

    confirmed = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"

    apply_claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert apply_claim.status_code == 200
    assert apply_claim.json()["phase"] == "apply"
    await event(client, wh, run_id, "phase_finished")

    final = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert final["state"] == "finished"
    assert final["confirmed_by_user_id"] is not None


async def test_one_active_run_per_env_under_concurrency(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    w1 = await register_worker(client, admin, "pool-conc-1", "w1")
    w2 = await register_worker(client, admin, "pool-conc-2", "w2")
    stack = await make_stack(client, admin, "conc-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    # Two queued runs on the SAME env.
    for _ in range(2):
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})

    # Two workers claim concurrently — exactly one wins (FOR UPDATE OF env SKIP LOCKED + 23505 net).
    r1, r2 = await asyncio.gather(
        client.post("/worker/v1/jobs/claim", headers=w1),
        client.post("/worker/v1/jobs/claim", headers=w2),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 204], (r1.status_code, r2.status_code)

    queue = (await client.get("/api/v1/queue", headers=admin)).json()
    active = [q for q in queue if q["environment_id"] == env_id and q["worker_id"]]
    assert len(active) == 1


async def test_confirm_blocked_by_tier(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer, staging ceiling
    wh = await register_worker(client, admin, "pool-tier")
    stack = await make_stack(client, admin, "tier-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    refused = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=bob)
    assert refused.status_code == 403  # writer can't confirm, and staging < prod


async def test_four_eyes_on_prod(client: httpx.AsyncClient) -> None:
    alice = await login(client, "alice")  # approver, prod
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-4eyes")
    stack = await make_stack(client, admin, "4eyes-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")

    # Alice triggers, drives to unconfirmed, then tries to confirm her own prod run → 4-eyes blocks.
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=alice, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)

    self_confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=alice)
    assert self_confirm.status_code == 403

    other_confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert other_confirm.status_code == 200


async def test_autodeploy_and_warn_forces_unconfirmed(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-auto")
    stack = await make_stack(client, admin, "auto-stack")

    # autodeploy env, no warn → system auto-confirms.
    auto_env = await make_env(client, admin, stack, "dev", "dev", autodeploy=True)
    run = (
        await client.post(f"/api/v1/environments/{auto_env}/runs", headers=admin, json={})
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()[
        "state"
    ] == "confirmed"

    # Same env, but an after_plan warn check → forced to unconfirmed despite autodeploy.
    # Finish the auto-confirmed run's apply first to free the env.
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished")

    warn_run = (
        await client.post(f"/api/v1/environments/{auto_env}/runs", headers=admin, json={})
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, warn_run["id"], "phase_started", phase="planning")
    await event(client, wh, warn_run["id"], "phase_started", phase="checking")
    warn_result = {**CHANGES, "checks": [{"name": "infracost", "status": "warn", "detail": "+$5"}]}
    await event(client, wh, warn_run["id"], "phase_finished", result=warn_result)
    state = (await client.get(f"/api/v1/runs/{warn_run['id']}", headers=admin)).json()["state"]
    assert state == "unconfirmed"


async def test_confirm_rejected_when_not_unconfirmed(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "rejected-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    # Still queued → confirm refused.
    refused = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert refused.status_code == 409


async def test_mock_block(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-mock")
    stack = await make_stack(client, admin, "mock-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)

    # Simulate a run that consumed mocks (Phase 4 sets this during resolution).
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as session:
        await session.execute(
            update(Run).where(Run.id == uuid.UUID(run_id)).values(used_mocks=True)
        )
        await session.commit()

    blocked = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert blocked.status_code == 409


async def test_repo_token_is_masked_in_claim(client: httpx.AsyncClient) -> None:
    """The repo auth token rides inside the clone URL — it must be in the worker's mask list (§5.1)."""
    from app.crypto import encrypt
    from app.db import SessionLocal
    from app.enums import RepoAuthKind
    from app.models.stack import Stack

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "masktok-pool")
    stack = await make_stack(client, admin, "masktok-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    async with SessionLocal() as s:
        st = await s.get(Stack, uuid.UUID(stack))
        st.repo_auth_kind = RepoAuthKind.token
        st.repo_secret_encrypted = encrypt("ghp_supersecrettoken")
        await s.commit()

    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["repo_credentials"]["token"] == "ghp_supersecrettoken"
    assert "ghp_supersecrettoken" in payload["mask_values"]  # so the agent redacts it in logs
