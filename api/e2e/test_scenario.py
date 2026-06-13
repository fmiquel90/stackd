"""The full non-regression scenario (DEV §7). If this passes against the live stack, the core of
the product works: plan → confirm → apply, automatic cascade, mock-block, tier + 4-eyes gates.

Run with `task e2e`. Requires the seeded demo graph and a live worker processing jobs.
"""

from __future__ import annotations

import asyncio
import time

import httpx

TERMINAL = {"finished", "failed", "discarded", "canceled"}


async def _login(http: httpx.AsyncClient, persona: str) -> dict[str, str]:
    r = await http.post("/api/v1/auth/dev/login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _trigger(http: httpx.AsyncClient, h: dict, env_id: str) -> str:
    r = await http.post(f"/api/v1/environments/{env_id}/runs", headers=h, json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _get(http: httpx.AsyncClient, h: dict, run_id: str) -> dict:
    r = await http.get(f"/api/v1/runs/{run_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _poll(http, h, run_id: str, target: set[str], *, timeout_s: float = 150) -> dict:
    """Poll a run until it reaches one of `target`; fail if it hits a different terminal state."""
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = await _get(http, h, run_id)
        if last["state"] in target:
            return last
        if last["state"] in TERMINAL and last["state"] not in target:
            raise AssertionError(f"run {run_id} reached {last['state']}, wanted {target}: {last}")
        await asyncio.sleep(1.5)
    raise AssertionError(f"timeout waiting for {run_id} → {target}; last={last}")


async def _latest_run(http, h, env_id: str) -> dict | None:
    r = await http.get(f"/api/v1/environments/{env_id}/runs", headers=h)
    runs = r.json()
    return runs[0] if runs else None


async def _wait_new_run(http, h, env_id: str, exclude: set[str], *, timeout_s: float = 60) -> dict:
    """Wait for a cascade-triggered run to appear on env (id not in `exclude`)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        latest = await _latest_run(http, h, env_id)
        if latest and latest["id"] not in exclude:
            return latest
        await asyncio.sleep(1.5)
    raise AssertionError(f"no new run appeared on {env_id}")


async def test_full_scenario(http: httpx.AsyncClient, envs: dict[str, str]) -> None:
    bob = await _login(http, "bob")  # writer, max_apply_tier=staging
    alice = await _login(http, "alice")  # approver, max_apply_tier=prod
    admin = await _login(http, "admin")

    net_dev, net_prod = envs["demo-network/dev"], envs["demo-network/prod"]
    app_dev, app_prod = envs["demo-app/dev"], envs["demo-app/prod"]

    # 1. bob (writer) triggers network/dev → plan → unconfirmed. A writer may trigger a plan.
    r1 = await _trigger(http, bob, net_dev)
    await _poll(http, bob, r1, {"unconfirmed"})

    # 2a. bob cannot confirm: apply needs role ∈ {approver, admin} (CLAUDE §4 #4), bob is a writer.
    denied_role = await http.post(f"/api/v1/runs/{r1}/confirm", headers=bob)
    assert denied_role.status_code == 403, denied_role.text

    # 2b. alice (approver, tier prod ≥ dev) confirms → apply → finished. dev is not 4-eyes-gated.
    c = await http.post(f"/api/v1/runs/{r1}/confirm", headers=alice)
    assert c.status_code == 200, c.text
    await _poll(http, alice, r1, {"finished"})

    # 3. cascade → app/dev plans with the REAL upstream output injected (no mocks).
    child = await _wait_new_run(http, bob, app_dev, exclude=set())
    child = await _poll(http, bob, child["id"], {"unconfirmed", "finished"})
    assert child["used_mocks"] is False

    # 4. trigger app/prod BEFORE network/prod is applied → plan uses the MOCK.
    rp = await _trigger(http, bob, app_prod)
    rp_run = await _poll(http, bob, rp, {"unconfirmed"})
    assert rp_run["used_mocks"] is True

    # 5. confirming a mock-consuming run is refused (allow_mock_apply=false).
    blocked = await http.post(f"/api/v1/runs/{rp}/confirm", headers=alice)
    assert blocked.status_code == 409, blocked.text
    assert "mock" in blocked.json()["detail"].lower()
    # Free app/prod for the real cascade later.
    await http.post(f"/api/v1/runs/{rp}/discard", headers=alice)

    # 6. network/prod: bob triggers, then bob is refused at confirm (writer can't apply).
    rnp = await _trigger(http, bob, net_prod)
    await _poll(http, bob, rnp, {"unconfirmed"})
    denied = await http.post(f"/api/v1/runs/{rnp}/confirm", headers=bob)
    assert denied.status_code == 403, denied.text

    # 7. alice confirms network/prod (approver, tier prod ok, 4-eyes ok: alice != bob) → finished.
    ok = await http.post(f"/api/v1/runs/{rnp}/confirm", headers=alice)
    assert ok.status_code == 200, ok.text
    await _poll(http, alice, rnp, {"finished"})

    # 8. cascade → app/prod re-runs, this time with the REAL output (no mock).
    real = await _wait_new_run(http, bob, app_prod, exclude={rp})
    real = await _poll(http, bob, real["id"], {"unconfirmed", "finished"})
    assert real["used_mocks"] is False

    # 9. audit shows the human decisions with the right actors.
    audit = await http.get("/api/v1/audit", headers=admin, params={"action": "run.confirmed"})
    emails = {e["actor_email"] for e in audit.json()}
    assert "alice@dev.local" in emails  # alice confirmed prod
