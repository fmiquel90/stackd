from __future__ import annotations

import httpx


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_register_reuses_row_for_same_name(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    token = (
        await client.post("/api/v1/worker-pools", headers=h, json={"name": "reuse-pool"})
    ).json()["token"]
    th = {"Authorization": f"Bearer {token}"}

    r1 = await client.post(
        "/worker/v1/register", headers=th, json={"name": "agent-a", "version": "1"}
    )
    r2 = await client.post(
        "/worker/v1/register", headers=th, json={"name": "agent-a", "version": "2"}
    )
    assert r1.status_code == 200 and r2.status_code == 200, r2.text

    # Re-registering the same name reuses the row (same id), not a fresh duplicate.
    assert r1.json()["worker_id"] == r2.json()["worker_id"]

    mine = [
        w for w in (await client.get("/api/v1/workers", headers=h)).json() if w["name"] == "agent-a"
    ]
    assert len(mine) == 1
    assert mine[0]["version"] == "2"  # row updated in place
