from __future__ import annotations

import httpx

from tests.conftest_phase2 import login


async def test_request_id_header(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert "x-request-id" in resp.headers


async def test_health(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    h = (await client.get("/api/v1/health", headers=admin)).json()
    assert h["status"] == "ok"
    assert h["checks"]["database"] == "ok"
    assert "workers" in h and "items" in h["workers"]
    assert "active" in h["runs"] and "queued" in h["runs"]


async def test_logs_admin_only_and_structured(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    # generate some activity
    await client.get("/api/v1/health", headers=admin)

    bob = await login(client, "bob")
    assert (await client.get("/api/v1/logs", headers=bob)).status_code == 403

    out = (await client.get("/api/v1/logs", headers=admin, params={"event": "http.request"})).json()
    assert out["total"] >= 1
    entry = out["items"][0]
    assert entry["event"] == "http.request"
    assert {"ts", "level", "logger", "msg", "request_id"} <= set(entry)
