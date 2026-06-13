from __future__ import annotations

import httpx

from tests.conftest_phase2 import login, register_worker


async def _worker_id(client: httpx.AsyncClient, admin: dict[str, str], name: str) -> str:
    workers = (await client.get("/api/v1/workers", headers=admin)).json()
    return next(w["id"] for w in workers if w["name"] == name)


async def test_diagnostics_command_roundtrip(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-diag", "agent-diag")
    worker_id = await _worker_id(client, admin, "agent-diag")

    # Admin requests diagnostics → queued (pending).
    req = await client.post(f"/api/v1/workers/{worker_id}/diagnostics", headers=admin)
    assert req.status_code == 202
    command_id = req.json()["command_id"]

    # The worker receives it on its next heartbeat (downward channel, no inbound).
    hb = await client.post("/worker/v1/heartbeat", headers=wh)
    cmds = hb.json()["commands"]
    assert any(c["id"] == command_id and c["type"] == "diagnostics" for c in cmds)

    # Delivered commands aren't re-sent.
    hb2 = await client.post("/worker/v1/heartbeat", headers=wh)
    assert hb2.json()["commands"] == []

    # The worker posts the read-only bundle back.
    result = {"platform": "linux", "tools": {"tofu": "OpenTofu v1.12.0"}, "env_var_names": ["PATH"]}
    res = await client.post(
        f"/worker/v1/commands/{command_id}/result", headers=wh, json={"result": result}
    )
    assert res.status_code == 200

    # Admin reads the latest diagnostics.
    latest = (await client.get(f"/api/v1/workers/{worker_id}/diagnostics", headers=admin)).json()
    assert latest["status"] == "done"
    assert latest["result"]["tools"]["tofu"] == "OpenTofu v1.12.0"


async def test_diagnostics_admin_only(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-diag2", "agent-diag2")  # noqa: F841
    worker_id = await _worker_id(client, admin, "agent-diag2")
    bob = await login(client, "bob")
    assert (
        await client.post(f"/api/v1/workers/{worker_id}/diagnostics", headers=bob)
    ).status_code == 403
