from __future__ import annotations

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _apply_commit(client, wh, admin, env_id, sha) -> None:
    """Drive a run on env_id to `finished` (apply) at commit `sha`."""
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"commit_sha": sha}
        )
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    # confirm → apply → finished
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished", result={"outputs": {}})
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()[
        "state"
    ] == "finished"


async def test_promote_carries_the_applied_commit(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "promote-pool")
    stack = await make_stack(client, admin, "promote-stack")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")

    await _apply_commit(client, wh, admin, dev, "abc123")

    promoted = await client.post(
        f"/api/v1/environments/{staging}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["environment_id"] == staging
    assert body["commit_sha"] == "abc123"  # exact commit applied on dev
    assert body["type"] == "tracked"

    audit = await client.get("/api/v1/audit", headers=admin, params={"action": "run.promoted"})
    assert any(e["target_id"] == body["id"] for e in audit.json())


async def test_promote_requires_an_applied_source(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "promote-empty")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")
    r = await client.post(
        f"/api/v1/environments/{staging}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert r.status_code == 409  # dev has nothing applied yet


async def test_promote_rejects_cross_stack(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    s1 = await make_stack(client, admin, "promote-s1")
    s2 = await make_stack(client, admin, "promote-s2")
    dev = await make_env(client, admin, s1, "dev", "dev")
    other = await make_env(client, admin, s2, "dev", "dev")
    r = await client.post(
        f"/api/v1/environments/{other}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert r.status_code == 400
