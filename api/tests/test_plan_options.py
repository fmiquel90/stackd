"""Targeted / replace plans: `targets` / `replace` plan options on a run (SPECS §3.5, §7.4)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _trigger(client, h, env_id, **body) -> httpx.Response:
    return await client.post(f"/api/v1/environments/{env_id}/runs", headers=h, json=body)


@pytest.mark.parametrize(
    "body",
    [
        {"targets": [""]},
        {"targets": ["-destroy"]},
        {"targets": ["aws_s3_bucket.a b"]},  # whitespace
        {"targets": ["aws_s3_bucket.a\nx"]},  # control character
        {"targets": ["aws_s3_bucket.a=x"]},  # `=` trick
        {"targets": ["-target=aws_s3_bucket.a"]},
        {"targets": ["a" * 513]},  # too long
        {"targets": [f"aws_s3_bucket.b{i}" for i in range(51)]},  # too many
        {"targets": [1]},
        {"replace": ["aws_s3_bucket.a;rm"]},
        {"type": "destroy", "replace": ["aws_s3_bucket.a"]},  # replace: tracked only
    ],
)
async def test_plan_options_validation(client: httpx.AsyncClient, body: dict) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, f"popt-val-{uuid.uuid4().hex[:8]}")
    env = await make_env(client, admin, stack, "dev", "dev")
    r = await _trigger(client, admin, env, **body)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_plan_options_persisted_deduplicated_and_audited(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer: a targeted plan is still just a plan
    stack = await make_stack(client, admin, "popt-persist")
    env = await make_env(client, admin, stack, "dev", "dev")
    addr = 'module.net["eu-west-1"].aws_subnet.a[0]'
    r = await _trigger(
        client,
        bob,
        env,
        targets=[addr, "aws_s3_bucket.logs", addr],
        replace=["aws_instance.web"],
    )
    assert r.status_code == 201, r.text
    run = r.json()
    expected = {"targets": [addr, "aws_s3_bucket.logs"], "replace": ["aws_instance.web"]}
    assert run["plan_options"] == expected
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=bob)).json()
    assert got["plan_options"] == expected

    audit = (
        await client.get(
            "/api/v1/audit", headers=admin, params={"target_kind": "run", "target_id": run["id"]}
        )
    ).json()
    triggered = next(e for e in audit if e["action"] == "run.triggered")
    assert triggered["context"]["plan_options"] == expected


async def test_no_plan_options_is_null(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "popt-none")
    env = await make_env(client, admin, stack, "dev", "dev")
    run = (await _trigger(client, admin, env, targets=[], replace=[])).json()
    assert run["plan_options"] is None
    audit = (
        await client.get(
            "/api/v1/audit", headers=admin, params={"target_kind": "run", "target_id": run["id"]}
        )
    ).json()
    assert "plan_options" not in (audit[0]["context"] or {})


async def test_targeted_destroy_needs_can_destroy(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer, no can_destroy
    stack = await make_stack(client, admin, "popt-destroy")
    env = await make_env(client, admin, stack, "dev", "dev")
    denied = await _trigger(client, bob, env, type="destroy", targets=["aws_s3_bucket.a"])
    assert denied.status_code == 403, denied.text
    ok = await _trigger(client, admin, env, type="destroy", targets=["aws_s3_bucket.a"])
    assert ok.status_code == 201, ok.text
    audit = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"target_kind": "run", "target_id": ok.json()["id"]},
        )
    ).json()
    destroy = next(e for e in audit if e["action"] == "run.destroy_triggered")
    assert destroy["context"]["plan_options"] == {"targets": ["aws_s3_bucket.a"]}


async def test_claim_carries_plan_options_to_the_plan_job_only(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "popt-claim-pool")
    stack = await make_stack(client, admin, "popt-claim")
    env = await make_env(client, admin, stack, "dev", "dev")
    opts = {"targets": ["aws_s3_bucket.a"], "replace": ["aws_instance.web"]}
    run = (await _trigger(client, admin, env, **opts)).json()

    plan_job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert plan_job["job_id"] == run["id"]
    assert plan_job["phase"] == "plan"
    assert plan_job["plan_options"] == opts

    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    assert (await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)).status_code == (
        200
    )
    apply_job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply_job["job_id"] == run["id"]
    assert apply_job["phase"] == "apply"
    assert apply_job["plan_options"] is None  # the apply applies the saved plan, never re-plans


async def test_full_plan_claim_has_no_plan_options(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "popt-full-pool")
    stack = await make_stack(client, admin, "popt-full")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _trigger(client, admin, env)
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["plan_options"] is None


@pytest.mark.parametrize(
    "opts", [{"targets": ["aws_s3_bucket.a"]}, {"replace": ["aws_instance.web"]}]
)
async def test_plan_options_never_autoconfirm(client: httpx.AsyncClient, opts: dict) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"popt-auto-{next(iter(opts))}")
    stack = await make_stack(client, admin, f"popt-auto-{next(iter(opts))}")
    env = await make_env(client, admin, stack, "dev", "dev", autodeploy=True)
    run = (await _trigger(client, admin, env, **opts)).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    state = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()["state"]
    assert state == "unconfirmed"  # a human decides, even on an autodeploy env


async def test_plan_options_refused_on_other_run_types(client: httpx.AsyncClient) -> None:
    # Only a human's tracked/destroy trigger carries options — never a proposed (PR/drift) run.
    from app.db import SessionLocal
    from app.enums import RunType, TriggeredBy
    from app.errors import ProblemException
    from app.models.environment import Environment
    from app.runs.service import trigger_run

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "popt-types")
    env = await make_env(client, admin, stack, "dev", "dev")
    async with SessionLocal() as s:
        env_obj = await s.get(Environment, uuid.UUID(env))
        assert env_obj is not None
        with pytest.raises(ProblemException) as exc:
            await trigger_run(
                s,
                env_obj,
                run_type=RunType.proposed,
                triggered_by=TriggeredBy.webhook,
                plan_options={"targets": ["aws_s3_bucket.a"]},
            )
        assert exc.value.status == 422
