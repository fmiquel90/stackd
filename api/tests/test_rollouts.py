"""Environment rollouts (SPECS §21): one approval, the graph applied level by level, creates-only
plans confirmed under the approval, anything else stopped for a human."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.rollout import EnvRollout
from app.models.run import Run
from app.models.state import StateVersion
from app.models.user import User
from app.rollouts.service import (
    cancel_rollout,
    confirm_under_approval,
    expire_approvals,
    lock_rollout,
)
from tests.conftest_phase2 import event, login, make_stack, register_worker

ZERO = {"change": 0, "destroy": 0, "import": 0, "forget": 0}
CREATES = {"has_changes": True, "summary": {"add": 2, **ZERO}}
CHANGES = {"has_changes": True, "summary": {"add": 1, **ZERO, "change": 1}}
SHA = "a" * 40


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


async def _plan(client, wh, result: dict, sha: str = SHA) -> dict:
    job = await _claim(client, wh)
    await event(
        client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": sha}
    )
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
    # She approved the rollout, not this plan: the run says so (DESIGN §5.11).
    assert run["confirmed_via"] == "rollout"
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
    export = await client.get(
        "/api/v1/audit/export",
        headers=admin,
        params={"action": "run.confirmed", "target_id": net_job["job_id"]},
    )
    row = export.text.splitlines()[1]  # the system confirmation names its rollout and approver
    assert "rollout" in row and "alice@dev.local" in row
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
    assert member["run_state"] == "unconfirmed"
    assert "does not report import, forget" in member["waiting_reason"]


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


# --- review fixes (2) ---


async def _approve(client, h, rid: str) -> dict:
    r = await client.post(f"/api/v1/rollouts/{rid}/approve", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _runs(client, h, env_id: str) -> list[dict]:
    return (await client.get(f"/api/v1/environments/{env_id}/runs", headers=h)).json()


async def _user(session, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def test_a_member_made_protected_after_a_self_approval_is_not_auto_confirmed(
    client: httpx.AsyncClient,
) -> None:
    """Four-eyes is re-checked at the auto-confirmation (§21.5): the creator approved on dev
    (allowed), a writer then protects a member — the next plan stops, and only someone else may
    approve again."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-4e")
    g = await _graph(client, admin, "ro-4e", tier="dev")
    ro = await _rollout(client, admin, "ro-4e")
    await _approve(client, admin, ro["id"])  # the creator: no four-eyes on dev
    r = await client.patch(
        f"/api/v1/environments/{g['net']}", headers=admin, json={"protected": True}
    )
    assert r.status_code == 200, r.text

    job = await _plan(client, wh, CREATES)
    ro = await _get(client, admin, ro["id"])
    assert _member(ro, g["net"])["run_state"] == "unconfirmed"
    assert ro["state"] == "paused" and ro["pause_reason"] == "member_changed_since_approval"
    assert "changed since the approval" in _member(ro, g["net"])["waiting_reason"]
    assert ro["approvable"] is True
    again = await client.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=admin)
    assert again.status_code == 403  # now protected: four-eyes against the creator
    ro = await _approve(client, alice, ro["id"])
    net = _member(ro, g["net"])
    assert net["run_id"] == job["job_id"] and net["run_state"] == "confirmed"
    assert ro["state"] == "running"


async def test_a_member_moved_to_another_tier_after_approval_pauses(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-tier")
    g = await _graph(client, admin, "ro-tier", tier="dev")
    ro = await _rollout(client, admin, "ro-tier")
    await _approve(client, admin, ro["id"])
    r = await client.patch(f"/api/v1/environments/{g['net']}", headers=admin, json={"tier": "prod"})
    assert r.status_code == 200, r.text
    await _plan(client, wh, CREATES)
    ro = await _get(client, admin, ro["id"])
    assert _member(ro, g["net"])["run_state"] == "unconfirmed"
    assert ro["pause_reason"] == "member_changed_since_approval"


async def test_a_cancel_committed_during_the_gate_wins(client: httpx.AsyncClient) -> None:
    """The gate locks the rollout before judging (§21.3): a cancel committed while it waits is
    seen — no confirmation, no apply claim."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-race")
    await _graph(client, admin, "ro-race")
    ro = await _rollout(client, admin, "ro-race")
    await _approve(client, alice, ro["id"])
    job = await _claim(client, wh)
    await event(
        client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": SHA}
    )
    run_id = uuid.UUID(job["job_id"])

    async def gate() -> bool:
        async with SessionLocal() as b:
            run = await b.get(Run, run_id)
            assert run is not None
            fields = {"plan_summary": CREATES["summary"], "check_results": {"checks": []}}
            ok = await confirm_under_approval(b, run, fields=fields)
            await b.commit()
            return ok

    async with SessionLocal() as a:
        rollout = await lock_rollout(a, uuid.UUID(ro["id"]))
        assert rollout is not None
        await cancel_rollout(a, await _user(a, "admin@dev.local"), rollout)
        task = asyncio.create_task(gate())
        await asyncio.sleep(0.5)
        assert not task.done()  # waiting for the rollout's lock
        await a.commit()
    assert await asyncio.wait_for(task, 10) is False
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        assert run is not None and run.state.value == "planning" and run.confirmed_at is None


async def test_member_runs_lock_their_rollout_first(client: httpx.AsyncClient) -> None:
    """One lock order (§21.3): a member run's transition waits on its rollout before locking its
    own row, and a claim gives way to a locked rollout instead of waiting."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-order")
    await _graph(client, admin, "ro-order", tier="staging")
    ro = await _rollout(client, admin, "ro-order")
    await _approve(client, admin, ro["id"])
    job = await _plan(client, wh, CHANGES)  # stops at unconfirmed
    run_id = uuid.UUID(job["job_id"])

    async with SessionLocal() as a:
        await lock_rollout(a, uuid.UUID(ro["id"]))
        confirm = asyncio.create_task(
            client.post(f"/api/v1/runs/{job['job_id']}/confirm", headers=alice)
        )
        await asyncio.sleep(0.5)
        assert not confirm.done()
        # The waiting confirm holds no lock on its run.
        await a.execute(select(Run.id).where(Run.id == run_id).with_for_update(nowait=True))
        await a.commit()
    assert (await asyncio.wait_for(confirm, 10)).status_code == 200

    async with SessionLocal() as a:
        await lock_rollout(a, uuid.UUID(ro["id"]))
        claim = await asyncio.wait_for(client.post("/worker/v1/jobs/claim", headers=wh), 10)
        assert claim.status_code == 204  # gave way, did not wait
        await a.commit()
    job = await _claim(client, wh)
    assert job["job_id"] == str(run_id) and job["phase"] == "apply"


async def _space(client, admin, name: str, alice_tiers: list[str]) -> tuple[str, str]:
    space = (await client.post("/api/v1/spaces", headers=admin, json={"name": name})).json()
    users = (await client.get("/api/v1/users", headers=admin)).json()
    alice_id = next(u["id"] for u in users if u["email"] == "alice@dev.local")
    await _set_alice(client, admin, space["id"], alice_id, alice_tiers)
    return space["id"], alice_id


async def _set_alice(client, admin, space_id: str, alice_id: str, tiers: list[str]) -> None:
    member = {"user_id": alice_id, "role": "approver", "allowed_tiers": tiers}
    r = await client.put(f"/api/v1/spaces/{space_id}/members", headers=admin, json=member)
    assert r.status_code in (200, 201), r.text


async def _space_env(client, admin, space_id: str, stack: str, name: str, tier: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"space_id": space_id, "name": stack, "repo_url": "file:///r", "tool_version": "1.12"},
    )
    assert r.status_code == 201, r.text
    return await _env(client, admin, r.json()["id"], name, tier)


async def test_a_closed_gate_starts_nothing_and_a_reapproval_reopens_it(
    client: httpx.AsyncClient,
) -> None:
    """`approver_lost_rights` closes the gate (§21.4): no member starts while it is closed — none
    is stranded at `unconfirmed` — it stays until a re-approval, which re-runs the gate."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-strand")
    space_id, alice_id = await _space(client, admin, "ro-strand", ["staging"])
    net = await _space_env(client, admin, space_id, "ro-strand-net", "ro-strand", "staging")
    solo = await _space_env(client, admin, space_id, "ro-strand-solo", "ro-strand", "staging")
    app = await _space_env(client, admin, space_id, "ro-strand-app", "ro-strand", "staging")
    r = await client.post(
        f"/api/v1/environments/{app}/dependencies",
        headers=admin,
        json={"upstream_env_id": net, "trigger_policy": "always", "references": []},
    )
    assert r.status_code == 201, r.text
    ro = (
        await client.post(
            "/api/v1/rollouts",
            headers=admin,
            json={"space_id": space_id, "env_name": "ro-strand"},
        )
    ).json()
    await _approve(client, alice, ro["id"])
    await _set_alice(client, admin, space_id, alice_id, ["dev"])  # alice loses staging

    first, second = await _claim(client, wh), await _claim(client, wh)
    jobs = {first["job_id"]: first, second["job_id"]: second}
    ro = await _get(client, admin, ro["id"])
    net_run, solo_run = _member(ro, net)["run_id"], _member(ro, solo)["run_id"]
    assert set(jobs) == {net_run, solo_run}
    for run_id, result in ((solo_run, CREATES), (net_run, CHANGES)):
        await event(
            client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA}
        )
        await event(client, wh, run_id, "phase_finished", result=result)
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "paused" and ro["pause_reason"] == "approver_lost_rights"

    # A human confirms net (staging: the creator may), it applies: app's upstream is done, yet
    # app does not start while the gate is closed.
    assert (await client.post(f"/api/v1/runs/{net_run}/confirm", headers=admin)).status_code == 200
    await _apply(client, wh, net_run)
    ro = await _get(client, admin, ro["id"])
    assert _member(ro, net)["status"] == "done" and _member(ro, app)["status"] == "pending"
    assert ro["pause_reason"] == "approver_lost_rights" and ro["approvable"] is True

    await _set_alice(client, admin, space_id, alice_id, ["staging"])
    ro = await _approve(client, alice, ro["id"])
    assert _member(ro, solo)["run_state"] == "confirmed" and _member(ro, solo)["auto_confirmed"]
    assert _member(ro, app)["status"] == "planning"
    assert ro["state"] == "running"


async def test_a_plan_that_forgets_or_does_anything_new_needs_a_human(
    client: httpx.AsyncClient,
) -> None:
    """Every counter but `add` must be zero, including ones a newer tool adds (§21.5)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-forget")
    a = await _env(client, admin, await make_stack(client, admin, "ro-fg-a"), "ro-fg", "dev")
    b = await _env(client, admin, await make_stack(client, admin, "ro-fg-b"), "ro-fg", "dev")
    ro = await _rollout(client, admin, "ro-fg")
    await _approve(client, admin, ro["id"])
    ja, jb = await _claim(client, wh), await _claim(client, wh)
    ro = await _get(client, admin, ro["id"])
    by_run = {_member(ro, a)["run_id"]: "forget", _member(ro, b)["run_id"]: "action_invocation"}
    for job in (ja, jb):
        key = by_run[job["job_id"]]
        summary = {"add": 1, **ZERO, key: 1}
        await event(
            client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": SHA}
        )
        await event(
            client,
            wh,
            job["job_id"],
            "phase_finished",
            result={"has_changes": True, "summary": summary},
        )
    ro = await _get(client, admin, ro["id"])
    assert _member(ro, a)["run_state"] == "unconfirmed"
    assert "forgets 1 resource" in _member(ro, a)["waiting_reason"]
    assert _member(ro, b)["run_state"] == "unconfirmed"
    assert "action invocation 1" in _member(ro, b)["waiting_reason"]


async def test_an_environment_with_infrastructure_always_stops_for_a_human(
    client: httpx.AsyncClient,
) -> None:
    """The approval only covers environments never applied (§21.5)."""
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-infra")
    g = await _graph(client, admin, "ro-infra")
    async with SessionLocal() as s:
        s.add(StateVersion(environment_id=uuid.UUID(g["net"]), serial=1, s3_key="state/x"))
        await s.commit()
    ro = await _rollout(client, admin, "ro-infra")
    await _approve(client, alice, ro["id"])
    await _plan(client, wh, CREATES)
    ro = await _get(client, admin, ro["id"])
    net = _member(ro, g["net"])
    assert (
        net["run_state"] == "unconfirmed" and "already has infrastructure" in net["waiting_reason"]
    )
    assert ro["pause_reason"] == "needs_confirmation"


async def test_a_retry_plans_the_commit_the_rollout_pinned(client: httpx.AsyncClient) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    wh = await register_worker(client, admin, "pool-ro-pin")
    g = await _graph(client, admin, "ro-pin")
    ro = await _rollout(client, admin, "ro-pin")
    await _approve(client, alice, ro["id"])
    job = await _claim(client, wh)
    await event(
        client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": SHA}
    )
    await event(client, wh, job["job_id"], "job_failed", phase="plan", result={"error": "x"})
    ro = (await client.post(f"/api/v1/rollouts/{ro['id']}/retry", headers=admin)).json()
    retried = _member(ro, g["net"])["run_id"]
    assert retried != job["job_id"]
    assert (await client.get(f"/api/v1/runs/{retried}", headers=admin)).json()["commit_sha"] == SHA
    job = await _claim(client, wh)
    assert job["job_id"] == retried
    moved = await event(
        client, wh, retried, "phase_started", phase="planning", result={"commit_sha": "b" * 40}
    )
    assert moved.status_code == 409  # commit_mismatch: the branch moved, the rollout did not


async def test_a_second_member_of_a_stack_must_plan_the_pinned_commit(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-pin2")
    stack = await make_stack(client, admin, "ro-pin2")
    one = await _env(client, admin, stack, "ro-pin2-a", "dev")
    two = await _env(client, admin, stack, "ro-pin2-b", "dev")
    r = await client.post("/api/v1/rollouts", headers=admin, json={"environment_ids": [one, two]})
    ro = r.json()
    await _approve(client, admin, ro["id"])
    first, second = await _claim(client, wh), await _claim(client, wh)
    # Both runs come from one approval transaction (same created_at): make the run claimed second
    # sort first by creation, so the pin cannot follow creation order (§21.6: first to know).
    async with SessionLocal() as s:
        await s.execute(
            update(Run)
            .where(Run.id == uuid.UUID(first["job_id"]))
            .values(created_at=datetime.now(UTC) + timedelta(seconds=1))
        )
        await s.commit()
    for job, sha in ((first, SHA), (second, "b" * 40)):
        await event(
            client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": sha}
        )
        await event(client, wh, job["job_id"], "phase_finished", result=CREATES)
    ro = await _get(client, admin, ro["id"])
    states = {m["run_id"]: m for m in ro["members"]}
    assert states[first["job_id"]]["run_state"] == "confirmed"
    assert states[second["job_id"]]["run_state"] == "unconfirmed"
    assert "the rollout pinned" in states[second["job_id"]]["waiting_reason"]


async def test_cascade_leaves_the_members_of_an_active_rollout_alone(
    client: httpx.AsyncClient,
) -> None:
    """An upstream outside the rollout applying mid-rollout triggers no run on a member (§21.4);
    a member that already planned is reported to the rollout's people instead."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-up")
    dns = await _env(client, admin, await make_stack(client, admin, "ro-up-dns"), "ro-up-g", "dev")
    app = await _env(client, admin, await make_stack(client, admin, "ro-up-app"), "ro-up", "dev")
    r = await client.post(
        f"/api/v1/environments/{app}/dependencies",
        headers=admin,
        json={
            "upstream_env_id": dns,
            "trigger_policy": "always",
            "references": [{"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": "v"}],
        },
    )
    assert r.status_code == 201, r.text
    ro = await _rollout(client, admin, "ro-up")

    async def apply_dns() -> None:
        run = (await client.post(f"/api/v1/environments/{dns}/runs", headers=admin, json={})).json()
        job = await _plan(client, wh, CHANGES)
        assert job["job_id"] == run["id"]
        assert (
            await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
        ).status_code == 200
        await _apply(client, wh, run["id"])

    await apply_dns()  # the rollout awaits its approval: app is left to it
    assert await _runs(client, admin, app) == []

    await _approve(client, admin, ro["id"])
    await _plan(client, wh, CHANGES)  # app's member run: it waits at unconfirmed
    await apply_dns()
    assert len(await _runs(client, admin, app)) == 1  # only the rollout's
    notes = (await client.get("/api/v1/notifications", headers=admin)).json()
    changed = [n for n in notes if n["kind"] == "rollout_upstream_changed"]
    assert changed and changed[0]["context"]["environment_id"] == app


async def test_a_group_cascade_reaching_a_rollout_member_creates_no_run_there(
    client: httpx.AsyncClient,
) -> None:
    """core → mid → app → edge, app the only member of an active rollout. A run group started on
    core reaches app two levels down (§9.2 decide-once): app gets no cascade run — the rollout's
    people are told, naming mid's run (the applied upstream it would have been parented to) — and
    app's downstream, whose only upstream settled as skipped, gets none either."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-grp")

    async def env(name: str) -> str:
        return await _env(
            client, admin, await make_stack(client, admin, f"ro-grp-{name}"), name, "dev"
        )

    core, mid, app, edge = (
        await env("rogrp-core"),
        await env("rogrp-mid"),
        await env("rogrp"),
        await env("rogrp-edge"),
    )
    for down, up in ((mid, core), (app, mid), (edge, app)):
        r = await client.post(
            f"/api/v1/environments/{down}/dependencies",
            headers=admin,
            json={
                "upstream_env_id": up,
                "trigger_policy": "always",
                "references": [
                    {"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": "v"}
                ],
            },
        )
        assert r.status_code == 201, r.text
    ro = await _rollout(client, admin, "rogrp")
    assert [m["environment_id"] for m in ro["members"]] == [app]
    await _approve(client, admin, ro["id"])
    member_job = await _plan(client, wh, CHANGES)  # app's member run waits at unconfirmed
    assert member_job["job_id"] == _member(await _get(client, admin, ro["id"]), app)["run_id"]

    async def plan_confirm_apply(run_id: str) -> None:
        job = await _plan(client, wh, CHANGES)
        assert job["job_id"] == run_id
        assert (
            await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
        ).status_code == 200
        await _apply(client, wh, run_id)

    root = (await client.post(f"/api/v1/environments/{core}/runs", headers=admin, json={})).json()
    await plan_confirm_apply(root["id"])
    (mid_run,) = await _runs(client, admin, mid)
    assert mid_run["triggered_by"] == "dependency" and mid_run["parent_run_id"] == root["id"]
    await plan_confirm_apply(mid_run["id"])

    assert [r["id"] for r in await _runs(client, admin, app)] == [member_job["job_id"]]
    assert await _runs(client, admin, edge) == []
    notes = (await client.get("/api/v1/notifications", headers=admin)).json()
    changed = [
        n
        for n in notes
        if n["kind"] == "rollout_upstream_changed" and n["context"]["environment_id"] == app
    ]
    assert len(changed) == 1  # decided once
    assert changed[0]["context"]["upstream_env_id"] == mid
    assert changed[0]["context"]["upstream_run_id"] == mid_run["id"]
    group = (await client.get(f"/api/v1/runs/{root['id']}", headers=admin)).json()["run_group"]
    assert group["status"] == "succeeded"  # app's own run is the rollout's, not the group's


async def test_a_member_run_finishing_after_a_cancel_does_not_cascade(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ro-cx")
    g = await _graph(client, admin, "ro-cx", tier="dev")
    ro = await _rollout(client, admin, "ro-cx")
    await _approve(client, admin, ro["id"])
    job = await _claim(client, wh)
    await event(
        client, wh, job["job_id"], "phase_started", phase="planning", result={"commit_sha": SHA}
    )
    await client.post(f"/api/v1/rollouts/{ro['id']}/cancel", headers=admin)
    await event(client, wh, job["job_id"], "phase_finished", result=CREATES)
    run = (await client.get(f"/api/v1/runs/{job['job_id']}", headers=admin)).json()
    assert run["state"] == "unconfirmed"  # a canceled rollout confirms nothing
    assert (
        await client.post(f"/api/v1/runs/{job['job_id']}/confirm", headers=admin)
    ).status_code == 200
    await _apply(client, wh, job["job_id"])
    assert await _runs(client, admin, g["app"]) == []


async def test_a_member_of_an_active_rollout_cannot_be_deleted(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    g = await _graph(client, admin, "ro-del", tier="dev")
    ro = await _rollout(client, admin, "ro-del")
    r = await client.delete(f"/api/v1/environments/{g['app']}", headers=admin)
    assert r.status_code == 409 and r.json()["rollout_id"] == ro["id"]
    await client.post(f"/api/v1/rollouts/{ro['id']}/cancel", headers=admin)
    assert (
        await client.delete(f"/api/v1/environments/{g['app']}", headers=admin)
    ).status_code == 204


async def test_the_scheduler_pauses_a_rollout_whose_approval_expired(
    client: httpx.AsyncClient,
) -> None:
    admin, alice = await login(client, "admin"), await login(client, "alice")
    await _graph(client, admin, "ro-sweep")
    ro = await _rollout(client, admin, "ro-sweep")
    await _approve(client, alice, ro["id"])
    async with SessionLocal() as s:
        await s.execute(
            update(EnvRollout)
            .where(EnvRollout.id == ro["id"])
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    async with SessionLocal() as s:
        assert await expire_approvals(s, datetime.now(UTC)) >= 1
    ro = await _get(client, admin, ro["id"])
    assert ro["state"] == "paused" and ro["pause_reason"] == "approval_expired"
    assert ro["approvable"] is True
