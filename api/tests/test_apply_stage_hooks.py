"""What the apply job's hooks (before_apply/after_apply, SPECS §8.3) leave on the run: their checks
join the plan's in check_results, and a warning is audited `run.check_warned` like a plan's. A
failing after_apply hook fails the run, but the apply happened: its outputs are recorded and the
environment's drift is cleared, as after a finished apply."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
PLAN_OK = {"name": "tfsec", "status": "ok", "detail": None, "stage": "after_plan"}
PLAN_WARN = {"name": "infracost", "status": "warn", "detail": "+$5", "stage": "after_plan"}
APPLY_OK = {"name": "drift-guard", "status": "ok", "detail": None, "stage": "before_apply"}
APPLY_WARN = {"name": "smoke-test", "status": "warn", "detail": "exit 3", "stage": "after_apply"}


async def _to_applying(
    client: httpx.AsyncClient, name: str, plan_checks: list[dict]
) -> tuple[dict[str, str], dict[str, str], str, str]:
    """A run planned (with `plan_checks`), confirmed, and claimed by its apply job."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result={**CHANGES, "checks": plan_checks})
    assert (
        await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    ).status_code == 200
    apply = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert apply.status_code == 200 and apply.json()["phase"] == "apply", apply.text
    return admin, wh, env_id, run["id"]


async def _warned(client: httpx.AsyncClient, admin: dict[str, str], run_id: str) -> list[dict]:
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "run.check_warned", "target_id": run_id},
        )
    ).json()
    return sorted((row["context"] for row in rows), key=lambda c: c["checks"])


async def test_apply_stage_checks_are_recorded_and_audited(client: httpx.AsyncClient) -> None:
    admin, wh, _env, run_id = await _to_applying(client, "apply-checks", [PLAN_OK, PLAN_WARN])
    result = {"outputs": {}, "checks": [APPLY_OK, APPLY_WARN]}
    finished = await event(client, wh, run_id, "phase_finished", result=result)
    assert finished.status_code == 200, finished.text

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "finished"
    # `checks` keeps what needs attention; `results` every result, each with its stage.
    assert run["check_results"] == {
        "checks": [PLAN_WARN, APPLY_WARN],
        "results": [PLAN_OK, PLAN_WARN, APPLY_OK, APPLY_WARN],
    }
    assert await _warned(client, admin, run_id) == [
        {"checks": ["infracost"], "stages": ["after_plan"]},
        {"checks": ["smoke-test"], "stages": ["after_apply"]},
    ]


async def test_an_apply_without_checks_keeps_the_plans(client: httpx.AsyncClient) -> None:
    admin, wh, _env, run_id = await _to_applying(client, "apply-no-checks", [PLAN_WARN])
    await event(client, wh, run_id, "phase_finished", result={"outputs": {}, "checks": []})

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "finished"
    assert run["check_results"] == {"checks": [PLAN_WARN], "results": [PLAN_WARN]}
    assert await _warned(client, admin, run_id) == [
        {"checks": ["infracost"], "stages": ["after_plan"]}
    ]


async def test_a_failing_after_apply_hook_records_the_outputs_and_cascades_nothing(
    client: httpx.AsyncClient,
) -> None:
    admin, wh, env_id, run_id = await _to_applying(client, "after-apply-fail", [])
    # A downstream env that would re-run on any apply of this one.
    app_stack = await make_stack(client, admin, "after-apply-fail-app")
    app_env = await make_env(client, admin, app_stack, "dev", "dev")
    refs = [{"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": None}]
    dep = await client.post(
        f"/api/v1/environments/{app_env}/dependencies",
        headers=admin,
        json={"upstream_env_id": env_id, "trigger_policy": "always", "references": refs},
    )
    assert dep.status_code == 201, dep.text

    outputs = {
        "vpc_id": {"sensitive": False, "type": "string", "value": "vpc-0abc"},
        "db_password": {"sensitive": True, "type": "string", "value": "s3cr3t"},
    }
    failed = {"name": "smoke-test", "status": "fail", "detail": "exit 3"}
    result = {"checks": [failed], "outputs": outputs}
    resp = await event(client, wh, run_id, "job_failed", phase="after_apply", result=result)
    assert resp.status_code == 200, resp.text

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "failed"
    recorded = (await client.get(f"/api/v1/environments/{env_id}/outputs", headers=admin)).json()
    assert sorted(recorded, key=lambda o: o["name"]) == [
        {"name": "db_password", "value": None, "sensitive": True},  # never stored (§9.1)
        {"name": "vpc_id", "value": "vpc-0abc", "sensitive": False},
    ]
    downstream = (await client.get(f"/api/v1/environments/{app_env}/runs", headers=admin)).json()
    assert downstream == []  # no cascade from a failed run
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "run.check_failed", "target_id": run_id},
        )
    ).json()
    assert [row["context"]["phase"] for row in rows] == ["after_apply"]


async def test_a_failed_apply_records_no_outputs(client: httpx.AsyncClient) -> None:
    # Only an after_apply failure means the apply itself went through.
    admin, wh, env_id, run_id = await _to_applying(client, "apply-fail", [])
    outputs = {"vpc_id": {"sensitive": False, "type": "string", "value": "vpc-0abc"}}
    result = {"error": "apply failed", "outputs": outputs}
    await event(client, wh, run_id, "job_failed", phase="apply", result=result)
    assert (await client.get(f"/api/v1/environments/{env_id}/outputs", headers=admin)).json() == []


async def _drift(env_id: str, set_drifted: bool = False) -> str:
    """The environment's drift status — first marked drifted (§19) when `set_drifted`."""
    from app.db import SessionLocal
    from app.enums import DriftStatus
    from app.models.environment import Environment

    async with SessionLocal() as s:
        env = await s.get(Environment, uuid.UUID(env_id))
        assert env is not None
        if set_drifted:
            env.drift_status = DriftStatus.drifted.value
            env.drift_run_id = uuid.uuid4()
            await s.commit()
        return str(env.drift_status)


async def test_a_failing_after_apply_hook_clears_the_drift(client: httpx.AsyncClient) -> None:
    admin, wh, env_id, run_id = await _to_applying(client, "after-apply-drift", [])
    assert await _drift(env_id, set_drifted=True) == "drifted"
    failed = {"name": "smoke-test", "status": "fail", "detail": "exit 3", "stage": "after_apply"}
    result = {"checks": [failed], "outputs": {}}
    await event(client, wh, run_id, "job_failed", phase="after_apply", result=result)

    assert (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()["state"] == "failed"
    assert await _drift(env_id) == "in_sync"  # the apply reconciled state with reality
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "environment.drift_cleared", "target_id": env_id},
        )
    ).json()
    assert [row["context"] for row in rows] == [{"reason": "applied"}]


async def test_a_failed_apply_leaves_the_drift(client: httpx.AsyncClient) -> None:
    # Only an after_apply failure means the apply itself went through.
    _admin, wh, env_id, run_id = await _to_applying(client, "apply-fail-drift", [])
    await _drift(env_id, set_drifted=True)
    await event(client, wh, run_id, "job_failed", phase="apply", result={"error": "apply failed"})
    assert await _drift(env_id) == "drifted"
