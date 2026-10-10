"""`runs.check_results` (SPECS §8.3): every hook check result — ok, warn and fail, from any job and
any stage, with the stage it ran at — in `results`; the warn/fail ones, what needs attention, in
`checks` (the field's meaning before `results` existed). A failing check is recorded too, on the
job_failed that ends the job."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


def _check(name: str, status: str, stage: str) -> dict:
    return {
        "name": name,
        "status": status,
        "detail": None if status == "ok" else "exit 3",
        "stage": stage,
    }


async def _plan_claimed(
    client: httpx.AsyncClient, name: str
) -> tuple[dict[str, str], dict[str, str], str]:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    return admin, wh, run["id"]


async def _run(client: httpx.AsyncClient, admin: dict[str, str], run_id: str) -> dict:
    return (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()


async def _failed_audits(client: httpx.AsyncClient, admin: dict[str, str], run_id: str) -> list:
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "run.check_failed", "target_id": run_id},
        )
    ).json()
    return [row["context"]["phase"] for row in rows]


async def test_a_plan_failing_on_a_check_records_it_with_the_ones_before(
    client: httpx.AsyncClient,
) -> None:
    admin, wh, run_id = await _plan_claimed(client, "checks-plan-fail")
    ok, failed = _check("fmt", "ok", "before_init"), _check("policy", "fail", "before_plan")
    resp = await event(
        client, wh, run_id, "job_failed", phase="before_plan", result={"checks": [ok, failed]}
    )
    assert resp.status_code == 200, resp.text

    run = await _run(client, admin, run_id)
    assert run["state"] == "failed"
    assert run["check_results"] == {"checks": [failed], "results": [ok, failed]}
    assert await _failed_audits(client, admin, run_id) == ["before_plan"]


async def test_an_apply_failing_on_a_check_adds_it_to_the_plans(client: httpx.AsyncClient) -> None:
    admin, wh, run_id = await _plan_claimed(client, "checks-apply-fail")
    plan_ok, plan_warn = _check("tfsec", "ok", "after_plan"), _check("cost", "warn", "after_plan")
    result = {**CHANGES, "checks": [plan_ok, plan_warn]}
    await event(client, wh, run_id, "phase_finished", result=result)
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["phase"] == "apply"

    apply_fail = _check("change-window", "fail", "before_apply")
    result = {"checks": [apply_fail]}
    await event(client, wh, run_id, "job_failed", phase="before_apply", result=result)

    run = await _run(client, admin, run_id)
    assert run["state"] == "failed"
    # The stage tells the plan's checks from the apply's.
    assert run["check_results"] == {
        "checks": [plan_warn, apply_fail],
        "results": [plan_ok, plan_warn, apply_fail],
    }
    assert await _failed_audits(client, admin, run_id) == ["before_apply"]


async def test_a_job_failing_on_no_check_leaves_check_results_alone(
    client: httpx.AsyncClient,
) -> None:
    admin, wh, run_id = await _plan_claimed(client, "checks-plan-error")
    await event(client, wh, run_id, "job_failed", phase="plan", result={"error": "plan failed"})
    run = await _run(client, admin, run_id)
    assert run["state"] == "failed" and run["check_results"] is None


async def test_a_run_planned_before_results_existed_keeps_its_checks(
    client: httpx.AsyncClient,
) -> None:
    # A run planned by an API that only stored the warn/fail `checks` is applied after the
    # upgrade: its plan checks still lead `results`.
    from app.db import SessionLocal
    from app.models.run import Run

    admin, wh, run_id = await _plan_claimed(client, "checks-upgrade")
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    old_warn = {"name": "infracost", "status": "warn", "detail": "+$5"}  # no stage then
    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        run.check_results = {"checks": [old_warn]}
        await s.commit()
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["phase"] == "apply"
    apply_ok = _check("smoke-test", "ok", "after_apply")
    await event(client, wh, run_id, "phase_finished", result={"outputs": {}, "checks": [apply_ok]})

    run_out = await _run(client, admin, run_id)
    assert run_out["check_results"] == {"checks": [old_warn], "results": [old_warn, apply_ok]}
