"""Shared helpers for Phase 2 run/worker tests."""

from __future__ import annotations

import httpx


async def login(client: httpx.AsyncClient, persona: str) -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": persona})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def make_stack(client: httpx.AsyncClient, h: dict[str, str], name: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={"name": name, "repo_url": "file:///repos/x", "tool_version": "1.12.0"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_env(
    client: httpx.AsyncClient,
    h: dict[str, str],
    stack_id: str,
    name: str,
    tier: str,
    *,
    autodeploy: bool = False,
    protected: bool = False,
) -> str:
    r = await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=h,
        json={
            "name": name,
            "tier": tier,
            "branch": "main",
            "autodeploy": autodeploy,
            "protected": protected,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def register_worker(
    client: httpx.AsyncClient, admin: dict[str, str], pool_name: str, worker_name: str = "w1"
) -> dict[str, str]:
    pool = await client.post("/api/v1/worker-pools", headers=admin, json={"name": pool_name})
    token = pool.json()["token"]
    reg = await client.post(
        "/worker/v1/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": worker_name},
    )
    return {"Authorization": f"Bearer {reg.json()['worker_token']}"}


async def event(
    client: httpx.AsyncClient,
    wh: dict[str, str],
    job_id: str,
    name: str,
    *,
    phase: str | None = None,
    result: dict | None = None,
) -> httpx.Response:
    return await client.post(
        f"/worker/v1/jobs/{job_id}/events",
        headers=wh,
        json={"event": name, "phase": phase, "result": result},
    )
