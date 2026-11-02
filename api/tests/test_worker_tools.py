"""A worker gets only the runs whose stack's tool version it can run (SPECS §7.4)."""

from __future__ import annotations

import httpx

from app.workers.tools import can_run, valid_tool_version, version_matches
from tests.conftest_phase2 import login, make_env


async def _stack(client: httpx.AsyncClient, admin: dict, name: str, version: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": name, "repo_url": "file:///repos/x", "tool_version": version},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _worker(
    client: httpx.AsyncClient,
    admin: dict,
    pool: str,
    tools: dict | None,
    labels: dict | None = None,
) -> dict:
    token = (await client.post("/api/v1/worker-pools", headers=admin, json={"name": pool})).json()
    reg = await client.post(
        "/worker/v1/register",
        headers={"Authorization": f"Bearer {token['token']}"},
        json={"name": "w", "runner": "local", "tools": tools, "labels": labels},
    )
    assert reg.status_code == 200, reg.text
    return {"Authorization": f"Bearer {reg.json()['worker_token']}"}


def test_version_matching() -> None:
    assert version_matches("1.12.2", "1.12") and version_matches("1.12.0", "1.12.0")
    assert not version_matches("1.12.2", "1.12.0") and not version_matches("1.120.0", "1.12")
    assert not version_matches("1.12.3-rc1", "1.12")  # a series: releases only
    assert can_run(None, "opentofu", "9.9")  # an agent predating the field: not filtered here
    assert not can_run({"terraform": ["1.12.0"]}, "opentofu", "1.12")
    assert all(valid_tool_version(v) for v in ("1.12", "1.12.0", "1.12.0-rc1"))
    assert not any(valid_tool_version(v) for v in ("latest", "1", "~> 1.12", "1.12.x"))


async def test_stack_tool_version_is_validated(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": "tv-bad", "repo_url": "file:///repos/x", "tool_version": "latest"},
    )
    assert r.status_code == 422


async def test_claim_skips_a_run_the_worker_cannot_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    old = await _worker(client, admin, "tools-old", {"opentofu": ["1.11.4"]})
    new = await _worker(client, admin, "tools-new", {"opentofu": ["1.12.2"]})
    env_id = await make_env(
        client, admin, await _stack(client, admin, "tools-1-12", "1.12"), "d", "dev"
    )
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})

    queue = (await client.get("/api/v1/queue", headers=admin)).json()
    # Both workers are online, one can run it: claimable, not blocked.
    assert [q["blocking_reason"] for q in queue if q["environment_id"] == env_id] == [None]

    assert (await client.post("/worker/v1/jobs/claim", headers=old)).status_code == 204
    claimed = await client.post("/worker/v1/jobs/claim", headers=new)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["environment"]["tool_version"] == "1.12"


async def test_queue_reports_no_compatible_worker(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    # Labels confine the env to this test's worker: other tests' workers are online too.
    pool = {"pool": "tools-queue"}
    await _worker(client, admin, "tools-only-old", {"opentofu": ["1.11.4"]}, labels=pool)
    stack_id = await _stack(client, admin, "tools-9", "9.9.9")
    env = await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=admin,
        json={"name": "d", "tier": "dev", "branch": "main", "labels": pool},
    )
    assert env.status_code == 201, env.text
    env_id = env.json()["id"]
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})

    queue = (await client.get("/api/v1/queue", headers=admin)).json()
    reasons = [q["blocking_reason"] for q in queue if q["environment_id"] == env_id]
    assert reasons == ["no_compatible_worker"]
