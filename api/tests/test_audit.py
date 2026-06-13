from __future__ import annotations

import httpx

from tests.conftest_phase2 import login, make_env, make_stack


async def test_audit_filter_and_export(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "audit-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})

    triggered = (
        await client.get("/api/v1/audit", headers=admin, params={"action": "run.triggered"})
    ).json()
    assert any(e["action"] == "run.triggered" for e in triggered)

    export = await client.get("/api/v1/audit/export", headers=admin)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "action" in export.text


async def test_audit_export_admin_only(client: httpx.AsyncClient) -> None:
    bob = await login(client, "bob")  # writer
    resp = await client.get("/api/v1/audit/export", headers=bob)
    assert resp.status_code == 403
