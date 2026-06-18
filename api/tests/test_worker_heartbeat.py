from __future__ import annotations

import httpx

from tests.conftest_phase2 import login, register_worker


async def _worker(client: httpx.AsyncClient, admin: dict[str, str], name: str) -> dict:
    workers = (await client.get("/api/v1/workers", headers=admin)).json()
    return next(w for w in workers if w["name"] == name)


async def test_heartbeat_in_flight_drives_busy_idle(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-hb", "agent-hb")

    # Reported in-flight > 0 → busy.
    await client.post("/worker/v1/heartbeat", headers=wh, json={"in_flight": 2, "capacity": 4})
    assert (await _worker(client, admin, "agent-hb"))["status"] == "busy"

    # Back to 0 → idle.
    await client.post("/worker/v1/heartbeat", headers=wh, json={"in_flight": 0, "capacity": 4})
    assert (await _worker(client, admin, "agent-hb"))["status"] == "idle"


async def test_heartbeat_without_body_keeps_online(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-hb2", "agent-hb2")
    # No body: just a liveness ping, status stays a non-offline value.
    resp = await client.post("/worker/v1/heartbeat", headers=wh)
    assert resp.status_code == 200
    assert (await _worker(client, admin, "agent-hb2"))["status"] in {"idle", "busy"}
