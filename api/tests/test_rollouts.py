"""Environment rollouts (SPECS §21): one approval, the graph applied level by level, creates-only
plans confirmed under the approval, anything else stopped for a human."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.rollout import EnvRollout
from tests.conftest_phase2 import event, login, make_stack, register_worker

CREATES = {"has_changes": True, "summary": {"add": 2, "change": 0, "destroy": 0, "import": 0}}
CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 1, "destroy": 0, "import": 0}}


async def _env(client, admin, stack: str, name: str, tier: str, **extra) -> str:
    r = await client.post(
        f"/api/v1/stacks/{stack}/environments",
        headers=admin,
        json={"name": name, "tier": tier, "branch": "main", **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _graph(client, admin, name: str, tier: str = "prod", **extra) -> dict[str, str]:
    """network/<name> → app/<name>, in the default space."""
    net = await _env(
        client, admin, await make_stack(client, admin, f"{name}-net"), name, tier, **extra
    )
    app = await _env(
        client, admin, await make_stack(client, admin, f"{name}-app"), name, tier, **extra
    )
    r = await client.post(
        f"/api/v1/environments/{app}/dependencies",
        headers=admin,
        json={
            "upstream_env_id": net,
            "trigger_policy": "always",
            "references": [{"output_name": "vpc_id", "input_name": "vpc_id"}],
        },
    )
    assert r.status_code == 201, r.text
    return {"net": net, "app": app}


async def _rollout(client, h, name: str) -> dict:
    r = await client.post("/api/v1/rollouts", headers=h, json={"env_name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def _get(client, h, rid: str) -> dict:
    return (await client.get(f"/api/v1/rollouts/{rid}", headers=h)).json()


def _member(ro: dict, env_id: str) -> dict:
    return next(m for m in ro["members"] if m["environment_id"] == env_id)


async def _claim(client, wh) -> dict:
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(client, wh, result: dict) -> dict:
    job = await _claim(client, wh)
    await event(client, wh, job["job_id"], "phase_started", phase="planning")
    await event(client, wh, job["job_id"], "phase_finished", result=result)
    return job


async def _apply(client, wh, run_id: str) -> None:
    job = await _claim(client, wh)
    assert job["job_id"] == run_id and job["phase"] == "apply", job
    outputs = {"vpc_id": {"value": "vpc-123", "sensitive": False}}
    await event(client, wh, run_id, "phase_finished", result={"outputs": outputs})


async def test_rollout_applies_the_graph_under_one_approval(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-happy")
    g = await _graph(client, admin, "ro-happy", protected=True)

    ro = await _rollout(client, admin, "ro-happy")
    assert ro["state"] == "awaiting_approval"
    assert [(m["level"], m["environment_id"]) for m in ro["members"]] == [
        (0, g["net"]),
        (1, g["app"]),
    ]
    assert ro["can_approve"]["allowed"] is False  # admin created it, prod requires four-eyes
    refused = await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=admin)
    assert refused.status_code == 403
    assert (
        await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    ).status_code == 200

    net_job = await _plan(client, wh, CREATES)
    ro = await _get(client, admin, ro["id"])
    net = _member(ro, g["net"])
    assert net["run_state"] == "confirmed" and net["auto_confirmed"] is True  # protected, yet
    await _apply(client, wh, net_job["job_id"])

    ro = await _get(client, admin, ro["id"])
    assert _member(ro, g["net"])["status"] == "done"
    assert _member(ro, g["app"])["status"] == "planning"  # started by the rollout, once
    app_runs = (await client.get(f"/api/v1/environments/{g['app']}/runs", headers=admin)).json()
    assert len(app_runs) == 1 and app_runs[0]["triggered_by"] == "rollout"

    app_job = await _plan(client, wh, CREATES)
    await _apply(client, wh, app_job["job_id"])
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "succeeded" and ro["finished_at"]

    run = (await client.get(f"/api/v1/runs/{net_job['job_id']}", headers=admin)).json()
    alice_id = ro["approved_by"]["id"]
    assert run["confirmed_by_user_id"] == alice_id and run["rollout_id"] == ro["id"]
    async with SessionLocal() as s:
        confirmed = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "run.confirmed")))
            .scalars()
            .all()
        )
        actions = set(
            (
                await s.execute(select(AuditEvent.action).where(AuditEvent.target_id == ro["id"]))
            ).scalars()
        )
    assert any((e.context or {}).get("rollout_id") == ro["id"] for e in confirmed)
    assert {"rollout.created", "rollout.approved", "rollout.succeeded"} <= actions


async def test_a_plan_that_changes_something_waits_for_a_human(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-pause")
    g = await _graph(client, admin, "ro-pause")
    ro = await _rollout(client, admin, "ro-pause")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)

    job = await _plan(client, wh, CHANGES)
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "paused" and ro["pause_reason"] == "needs_confirmation"
    member = _member(ro, g["net"])
    assert (
        member["status"] == "needs_confirmation"
        and "changes 1 resource" in member["waiting_reason"]
    )

    # The creator is the run's triggerer: four-eyes on prod forbids them to confirm it.
    assert (
        await client.post(f"/api/v1/runs/{job['job_id']}/confirm", headers=admin)
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/runs/{job['job_id']}/confirm", headers=alice)
    ).status_code == 200
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "running" and _member(ro, g["net"])["auto_confirmed"] is False


async def test_a_failure_skips_its_branch_and_can_be_retried(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-fail")
    g = await _graph(client, admin, "ro-fail")
    ro = await _rollout(client, admin, "ro-fail")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)

    job = await _claim(client, wh)
    await event(client, wh, job["job_id"], "job_failed", phase="plan", result={"error": "x"})
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "failed"
    assert [_member(ro, g[k])["status"] for k in ("net", "app")] == ["failed", "skipped"]

    retried = await client.post(f"/api/v1/rollouts/{ro['id']}/retry", headers=admin)
    assert retried.status_code == 200, retried.text
    ro = retried.json()
    assert ro["state"] == "running" and _member(ro, g["net"])["status"] == "planning"
    assert _member(ro, g["app"])["status"] == "pending"


async def test_an_expired_approval_pauses_and_a_reapproval_resumes(
    client: httpx.AsyncClient,
) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-exp")
    g = await _graph(client, admin, "ro-exp")
    ro = await _rollout(client, admin, "ro-exp")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    async with SessionLocal() as s:
        await s.execute(
            update(EnvRollout)
            .where(EnvRollout.id == ro["id"])
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()

    job = await _plan(client, wh, CREATES)  # creates only, but the approval is over
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "paused" and ro["pause_reason"] == "approval_expired"
    assert _member(ro, g["net"])["run_state"] == "unconfirmed"

    again = await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    assert again.status_code == 200, again.text
    ro = again.json()
    net = _member(ro, g["net"])
    assert ro["state"] == "running" and net["run_state"] == "confirmed" and net["auto_confirmed"]
    assert net["run_id"] == job["job_id"]


async def test_approval_needs_apply_rights_on_every_member(client: httpx.AsyncClient) -> None:
    admin, bob = await login(client, "admin"), await login(client, "bob")
    await _graph(client, admin, "ro-rights")
    ro = await _rollout(client, admin, "ro-rights")
    r = await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=bob)
    assert r.status_code == 403  # bob is no approver, and prod is not among his tiers


async def test_creator_may_approve_where_no_four_eyes_applies(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await _graph(client, admin, "ro-dev", tier="dev")
    ro = await _rollout(client, admin, "ro-dev")
    assert ro["can_approve"]["allowed"] is True
    assert (
        await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=admin)
    ).status_code == 200


async def test_one_active_rollout_per_environment_and_cancel(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    g = await _graph(client, admin, "ro-cancel")
    ro = await _rollout(client, admin, "ro-cancel")
    dup = await client.post("/api/v1/rollouts", headers=admin, json={"env_name": "ro-cancel"})
    assert dup.status_code == 409
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)

    canceled = await client.post(f"/api/v1/rollouts/{ro['id']}/cancel", headers=admin)
    assert canceled.status_code == 200, canceled.text
    ro = canceled.json()
    assert ro["state"] == "canceled"
    assert _member(ro, g["net"])["run_state"] == "canceled"
    assert [_member(ro, g[k])["status"] for k in ("net", "app")] == ["canceled", "canceled"]
    again = await client.post("/api/v1/rollouts", headers=admin, json={"env_name": "ro-cancel"})
    assert again.status_code == 201  # the environments are free again


async def test_preview_names_members_that_will_plan_on_a_mock(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    shared = await _env(
        client, admin, await make_stack(client, admin, "ro-mock-dns"), "global", "prod"
    )
    app = await _env(
        client, admin, await make_stack(client, admin, "ro-mock-app"), "ro-mock", "prod"
    )
    r = await client.post(
        f"/api/v1/environments/{app}/dependencies",
        headers=admin,
        json={
            "upstream_env_id": shared,
            "trigger_policy": "always",
            "references": [{"output_name": "zone_id", "input_name": "zone_id", "mock_value": "z"}],
        },
    )
    assert r.status_code == 201, r.text
    ro = await _rollout(client, admin, "ro-mock")
    assert _member(ro, app)["expected_mocks"] == ["zone_id"]


async def test_a_member_no_worker_can_take_says_so(client: httpx.AsyncClient) -> None:
    """The env's labels route its runs (§7.2): a label no worker carries leaves the member's run
    queued — the rollout page says why instead of reading `running` forever."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    await register_worker(client, admin, "pool-ro-route")
    net = await _env(
        client,
        admin,
        await make_stack(client, admin, "ro-route-net"),
        "ro-route",
        "dev",
        labels={"region": "nowhere-1"},
    )
    ro = await _rollout(client, admin, "ro-route")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    ro = await _get(client, admin, ro["id"])
    assert "no online worker can take it" in _member(ro, net)["waiting_reason"]


# --- review fixes ---


async def test_a_member_never_falls_back_to_autodeploy(client: httpx.AsyncClient) -> None:
    """An autodeploy env cloned into a rollout: a changing plan still stops for a human (§21.4)."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-auto")
    g = await _graph(client, admin, "ro-auto", tier="dev", autodeploy=True)
    ro = await _rollout(client, admin, "ro-auto")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    await _plan(client, wh, CHANGES)
    assert _member(await _get(client, admin, ro["id"]), g["net"])["run_state"] == "unconfirmed"


async def test_a_summary_without_imports_fails_the_gate(client: httpx.AsyncClient) -> None:
    """An agent that does not report imports can't prove there are none: fail closed (§21.5)."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-imp")
    g = await _graph(client, admin, "ro-imp")
    ro = await _rollout(client, admin, "ro-imp")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    await _plan(client, wh, {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}})
    member = _member(await _get(client, admin, ro["id"]), g["net"])
    assert member["run_state"] == "unconfirmed" and "imports" in member["waiting_reason"]


async def test_cancel_also_stops_a_confirmed_unclaimed_run(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-cc")
    g = await _graph(client, admin, "ro-cc")
    ro = await _rollout(client, admin, "ro-cc")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    await _plan(client, wh, CREATES)
    assert _member(await _get(client, admin, ro["id"]), g["net"])["run_state"] == "confirmed"
    ro = (await client.post(f"/api/v1/rollouts/{ro['id']}/cancel", headers=admin)).json()
    assert _member(ro, g["net"])["run_state"] == "canceled"
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 204


async def test_an_approver_removed_from_the_space_loses_the_gate(client: httpx.AsyncClient) -> None:
    """No fallback to the instance defaults once the approver's membership is gone (§21.5)."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-gone")
    space = (await client.post("/api/v1/spaces", headers=admin, json={"name": "ro-gone"})).json()
    users = (await client.get("/api/v1/users", headers=admin)).json()
    alice_id = next(u["id"] for u in users if u["email"] == "alice@dev.local")
    member = {"user_id": alice_id, "role": "approver", "allowed_tiers": ["prod"]}
    assert (
        await client.put(f"/api/v1/spaces/{space['id']}/members", headers=admin, json=member)
    ).status_code in (200, 201)
    stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space["id"],
            "name": "ro-gone-net",
            "repo_url": "file:///r",
            "tool_version": "1.12",
        },
    )
    net = await _env(client, admin, stack.json()["id"], "ro-gone", "prod")
    ro = (
        await client.post(
            "/api/v1/rollouts", headers=admin, json={"space_id": space["id"], "env_name": "ro-gone"}
        )
    ).json()
    assert (
        await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    ).status_code == 200
    r = await client.delete(f"/api/v1/spaces/{space['id']}/members/{alice_id}", headers=admin)
    assert r.status_code in (200, 204), r.text

    await _plan(client, wh, CREATES)
    ro = await _get(client, admin, ro["id"])
    assert _member(ro, net)["run_state"] == "unconfirmed"
    assert ro["state"] == "paused" and ro["pause_reason"] == "approver_lost_rights"


async def test_a_reapproval_is_audited_as_a_resume(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-res")
    await _graph(client, admin, "ro-res")
    ro = await _rollout(client, admin, "ro-res")
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    async with SessionLocal() as s:
        await s.execute(
            update(EnvRollout)
            .where(EnvRollout.id == ro["id"])
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    await _plan(client, wh, CREATES)
    await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=alice)
    async with SessionLocal() as s:
        actions = list(
            (
                await s.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.target_id == ro["id"])
                    .order_by(AuditEvent.created_at)
                )
            ).scalars()
        )
    assert "rollout.paused" in actions and "rollout.resumed" in actions
