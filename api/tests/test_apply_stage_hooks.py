"""What the apply job's hooks (before_apply/after_apply, SPECS §8.3) leave on the run: their checks
join the plan's in check_results, and a warning is audited `run.check_warned` like a plan's."""

from __future__ import annotations

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
PLAN_WARN = {"name": "infracost", "status": "warn", "detail": "+$5"}
APPLY_WARN = {"name": "smoke-test", "status": "warn", "detail": "exit 3"}


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


async def _warned(client: httpx.AsyncClient, admin: dict[str, str], run_id: str) -> list[list]:
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "run.check_warned", "target_id": run_id},
        )
    ).json()
    return sorted(row["context"]["checks"] for row in rows)


async def test_apply_stage_checks_are_recorded_and_audited(client: httpx.AsyncClient) -> None:
    admin, wh, _env, run_id = await _to_applying(client, "apply-checks", [PLAN_WARN])
    finished = await event(
        client, wh, run_id, "phase_finished", result={"outputs": {}, "checks": [APPLY_WARN]}
    )
    assert finished.status_code == 200, finished.text

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "finished"
    assert run["check_results"] == {"checks": [PLAN_WARN, APPLY_WARN]}
    assert await _warned(client, admin, run_id) == [["infracost"], ["smoke-test"]]


async def test_an_apply_without_checks_keeps_the_plans(client: httpx.AsyncClient) -> None:
    admin, wh, _env, run_id = await _to_applying(client, "apply-no-checks", [PLAN_WARN])
    await event(client, wh, run_id, "phase_finished", result={"outputs": {}, "checks": []})

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "finished"
    assert run["check_results"] == {"checks": [PLAN_WARN]}
    assert await _warned(client, admin, run_id) == [["infracost"]]
