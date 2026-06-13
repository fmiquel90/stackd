from __future__ import annotations

import asyncio
import json
import os

import asyncpg
import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def test_transition_emits_listen_notify(client: httpx.AsyncClient) -> None:
    """A state change must publish a LISTEN/NOTIFY signal on run_<id> (SPECS §5.3) — the bridge
    that lets WS replicas push live updates."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-notify")
    stack = await make_stack(client, admin, "notify-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()

    received: list[dict] = []
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    try:
        await conn.add_listener(
            f"run_{run['id']}", lambda _c, _pid, _ch, payload: received.append(json.loads(payload))
        )
        # Drive transitions: claim (queued→preparing) then phase_started (planning).
        await client.post("/worker/v1/jobs/claim", headers=wh)
        await event(client, wh, run["id"], "phase_started", phase="planning")
        await asyncio.sleep(0.5)  # let notifications deliver
    finally:
        await conn.close()

    states = {m["to_state"] for m in received}
    assert "preparing" in states
    assert "planning" in states
    assert all(m["kind"] == "run_event" and m["run_id"] == run["id"] for m in received)
