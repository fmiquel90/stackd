"""Cancelling an executing run (SPECS §4.1, §7.1): a request the worker honours."""

from __future__ import annotations

import httpx
from sqlalchemy import text

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _planning(client, admin, wh, env_id, who=None):  # type: ignore[no-untyped-def]
    run = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=who or admin, json={})
    ).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    return str(run["id"])


async def _state(client, h, run_id) -> dict:  # type: ignore[no-untyped-def]
    return (await client.get(f"/api/v1/runs/{run_id}", headers=h)).json()


async def _actions(run_id: str) -> list[str]:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT action FROM audit_events WHERE target_id = CAST(:r AS uuid) ORDER BY id"
            ).bindparams(r=run_id)
        )
        return [r[0] for r in rows]


async def test_cancel_executing_plan_goes_through_the_worker(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # a writer is enough to stop a plan
    wh = await register_worker(client, admin, "pool-cancel-plan")
    stack = await make_stack(client, admin, "cancel-plan")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _planning(client, admin, wh, env_id)

    resp = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "planning"  # not ended: the worker has to stop first
    assert body["cancel_requested_at"] is not None

    beat = (await client.post("/worker/v1/heartbeat", headers=wh, json={"in_flight": 1})).json()
    cancels = [c for c in beat["commands"] if c["type"] == "cancel_job"]
    assert [c["payload"] for c in cancels] == [{"job_id": run_id}]

    # Asking again is harmless: the command is re-queued (the first may have been lost), but the
    # request is audited once.
    assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob)).status_code == 200
    again = (await client.post("/worker/v1/heartbeat", headers=wh, json={"in_flight": 1})).json()
    assert len([c for c in again["commands"] if c["type"] == "cancel_job"]) == 1

    assert (await event(client, wh, run_id, "job_canceled", phase="planning")).status_code == 200
    assert (await _state(client, admin, run_id))["state"] == "canceled"
    actions = await _actions(run_id)
    assert actions.count("run.cancel_requested") == 1
    assert "run.canceled" in actions
    # The env is free again.
    nxt = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    assert nxt.status_code in (200, 201), nxt.text


async def test_worker_cannot_cancel_a_run_nobody_asked_to_stop(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cancel-rogue")
    stack = await make_stack(client, admin, "cancel-rogue")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _planning(client, admin, wh, env_id)
    refused = await event(client, wh, run_id, "job_canceled", phase="planning")
    assert refused.status_code == 409
    assert (await _state(client, admin, run_id))["state"] == "planning"


async def test_a_plan_finishing_after_the_request_is_canceled_not_confirmed(
    client: httpx.AsyncClient,
) -> None:
    """The stop raced the end of the plan: never auto-confirm, never hold for approval."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cancel-race")
    stack = await make_stack(client, admin, "cancel-race")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _planning(client, admin, wh, env_id)
    await client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    assert (await _state(client, admin, run_id))["state"] == "canceled"


async def test_stopping_an_apply_needs_can_apply_and_ends_failed(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")  # approver on prod
    bob = await login(client, "bob")  # writer, no prod tier
    wh = await register_worker(client, admin, "pool-cancel-apply")
    stack = await make_stack(client, admin, "cancel-apply")
    env_id = await make_env(client, admin, stack, "prod", "prod")
    run_id = await _planning(client, admin, wh, env_id, who=alice)
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    assert (await _state(client, admin, run_id))["state"] == "applying"

    refused = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob)
    assert refused.status_code == 403
    assert (await _state(client, admin, run_id))["cancel_requested_at"] is None

    assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=alice)).status_code == 200
    assert (await event(client, wh, run_id, "job_canceled", phase="applying")).status_code == 200
    run = await _state(client, admin, run_id)
    # `applying` is never `canceled`: the infrastructure may be partly changed.
    assert run["state"] == "failed"
    assert run["error"] == "canceled_during_apply"
    actions = await _actions(run_id)
    assert "run.cancel_requested" in actions and "run.apply_failed" in actions


async def test_cancel_before_claim_and_after_the_end(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cancel-edges")
    stack = await make_stack(client, admin, "cancel-edges")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    queued = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    ).json()
    assert (await client.post(f"/api/v1/runs/{queued['id']}/cancel", headers=admin)).json()[
        "state"
    ] == "canceled"

    run_id = await _planning(client, admin, wh, env_id)
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    # Confirmed and not claimed yet: a plain transition.
    assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)).json()[
        "state"
    ] == "canceled"
    # Terminal: refused.
    assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)).status_code == 409


async def test_worker_shutdown_during_apply_fails_the_run(client: httpx.AsyncClient) -> None:
    """§7.1 drain: a job stopped by the worker's own shutdown (drain timeout / second signal) is
    reported `job_failed` with `worker_shutdown` — accepted without a user's cancel request, the
    run ends `failed`, never `canceled`, and is audited."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-shutdown-apply")
    stack = await make_stack(client, admin, "shutdown-apply")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _planning(client, admin, wh, env_id)
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    r = await event(
        client,
        wh,
        run_id,
        "job_failed",
        phase="apply",
        result={"error": "worker_shutdown: drain timeout"},
    )
    assert r.status_code == 200, r.text
    run = await _state(client, admin, run_id)
    assert run["state"] == "failed" and "worker_shutdown" in (run["error"] or "")
    assert "run.apply_failed" in await _actions(run_id)
