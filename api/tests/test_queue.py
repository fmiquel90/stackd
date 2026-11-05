"""The queue's blocking reasons (SPECS §12) and the claim latency metric (§20.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.config import get_settings
from app.db import SessionLocal
from app.models.environment import Environment
from app.models.run import Run
from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _reasons(client: httpx.AsyncClient, h: dict, env_id: str) -> dict[str, str | None]:
    queue = (await client.get("/api/v1/queue", headers=h)).json()
    return {q["run_id"]: q["blocking_reason"] for q in queue if q["environment_id"] == env_id}


async def _confirmed(client: httpx.AsyncClient, admin: dict, wh: dict, env_id: str) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text
    return run["id"]


async def _set_confirmed_at(run_id: str, at: datetime) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(Run).where(Run.id == uuid.UUID(run_id)).values(confirmed_at=at)
        )
        await session.commit()


async def test_queue_affinity_hold_only_within_the_window(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-queue-affinity")
    env_id = await make_env(client, admin, await make_stack(client, admin, "q-aff"), "dev", "dev")

    run_id = await _confirmed(client, admin, wh, env_id)
    # Held for its plan worker — not reported as blocked by itself (`active_run`).
    assert (await _reasons(client, admin, env_id))[run_id] == "apply_affinity_hold"

    # A run queued behind it waits on the active (confirmed) run.
    queued = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    ).json()["id"]
    assert (await _reasons(client, admin, env_id))[queued] == "active_run"

    # Past the window any compatible worker takes it: no longer held.
    affinity = get_settings().stackd_apply_affinity_seconds
    await _set_confirmed_at(run_id, datetime.now(UTC) - timedelta(seconds=affinity + 5))
    assert (await _reasons(client, admin, env_id))[run_id] is None


async def test_queue_reports_env_locked(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await register_worker(client, admin, "pool-queue-locked")
    env_id = await make_env(client, admin, await make_stack(client, admin, "q-lock"), "dev", "dev")
    run_id = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    ).json()["id"]
    assert (await _reasons(client, admin, env_id))[run_id] is None

    async with SessionLocal() as session:
        await session.execute(
            update(Environment).where(Environment.id == uuid.UUID(env_id)).values(locked=True)
        )
        await session.commit()
    assert (await _reasons(client, admin, env_id))[run_id] == "env_locked"


async def test_apply_claim_latency_counts_from_confirmation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.observability import metrics

    observed: list[float] = []
    monkeypatch.setattr(metrics.claim_latency, "observe", observed.append)

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-queue-latency")
    env_id = await make_env(client, admin, await make_stack(client, admin, "q-lat"), "dev", "dev")
    run_id = await _confirmed(client, admin, wh, env_id)
    # The run was created an hour ago, confirmed 10 s ago: the apply waited ~10 s, not an hour.
    async with SessionLocal() as session:
        await session.execute(
            update(Run)
            .where(Run.id == uuid.UUID(run_id))
            .values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
    await _set_confirmed_at(run_id, datetime.now(UTC) - timedelta(seconds=10))

    observed.clear()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200 and claim.json()["phase"] == "apply", claim.text
    assert len(observed) == 1 and 10 <= observed[0] < 60
