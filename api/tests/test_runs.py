from __future__ import annotations

import asyncio
import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _drive_to_unconfirmed(client, wh, env_id, admin) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    assert claim.json()["phase"] == "plan"
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return run["id"]


async def test_full_run_lifecycle(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-lifecycle")
    stack = await make_stack(client, admin, "life-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert run["state"] == "unconfirmed"
    assert run["plan_summary"]["add"] == 1

    confirmed = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"

    apply_claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert apply_claim.status_code == 200
    assert apply_claim.json()["phase"] == "apply"
    await event(client, wh, run_id, "phase_finished")

    final = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert final["state"] == "finished"
    assert final["confirmed_by_user_id"] is not None


async def test_one_active_run_per_env_under_concurrency(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    w1 = await register_worker(client, admin, "pool-conc-1", "w1")
    w2 = await register_worker(client, admin, "pool-conc-2", "w2")
    stack = await make_stack(client, admin, "conc-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    # Two queued runs on the SAME env.
    for _ in range(2):
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})

    # Two workers claim concurrently — exactly one wins (FOR UPDATE OF env SKIP LOCKED + 23505 net).
    r1, r2 = await asyncio.gather(
        client.post("/worker/v1/jobs/claim", headers=w1),
        client.post("/worker/v1/jobs/claim", headers=w2),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 204], (r1.status_code, r2.status_code)

    queue = (await client.get("/api/v1/queue", headers=admin)).json()
    active = [q for q in queue if q["environment_id"] == env_id and q["worker_id"]]
    assert len(active) == 1


async def test_confirm_blocked_by_tier(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer, staging ceiling
    wh = await register_worker(client, admin, "pool-tier")
    stack = await make_stack(client, admin, "tier-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    refused = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=bob)
    assert refused.status_code == 403  # writer can't confirm, and staging < prod


async def test_four_eyes_on_prod(client: httpx.AsyncClient) -> None:
    alice = await login(client, "alice")  # approver, prod
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-4eyes")
    stack = await make_stack(client, admin, "4eyes-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")

    # Alice triggers, drives to unconfirmed, then tries to confirm her own prod run → 4-eyes blocks.
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=alice, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)

    self_confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=alice)
    assert self_confirm.status_code == 403

    other_confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert other_confirm.status_code == 200


async def _alice_planned(client, wh, env_id) -> str:  # type: ignore[no-untyped-def]
    alice = await login(client, "alice")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=alice, json={})).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return str(run["id"])


async def test_four_eyes_on_a_protected_env(client: httpx.AsyncClient) -> None:
    # `protected` forces confirmation AND four-eyes (CLAUDE.md §4, SPECS §2.4), on a tier that
    # doesn't require it (the seeded dev). Who may confirm still comes from the tier only.
    alice = await login(client, "alice")  # approver, dev in her tiers
    admin = await login(client, "admin")
    # One worker per env: a confirmed run stays reserved for the worker that planned it (§7.2).
    wh_open = await register_worker(client, admin, "pool-open-4eyes")
    wh_protected = await register_worker(client, admin, "pool-protected-4eyes")
    stack = await make_stack(client, admin, "protected-4eyes-stack")
    open_env = await make_env(client, admin, stack, "dev", "dev")
    protected_env = await make_env(client, admin, stack, "dev-protected", "dev", protected=True)

    run_id = await _alice_planned(client, wh_open, open_env)  # control: a plain dev env
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=alice)).status_code == 200

    run_id = await _alice_planned(client, wh_protected, protected_env)
    self_confirm = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=alice)
    assert self_confirm.status_code == 403, self_confirm.text
    assert "4-eyes" in self_confirm.json()["detail"]
    other_confirm = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert other_confirm.status_code == 200, other_confirm.text


async def test_autodeploy_and_warn_forces_unconfirmed(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-auto")
    stack = await make_stack(client, admin, "auto-stack")

    # autodeploy env, no warn → system auto-confirms.
    auto_env = await make_env(client, admin, stack, "dev", "dev", autodeploy=True)
    run = (
        await client.post(f"/api/v1/environments/{auto_env}/runs", headers=admin, json={})
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()[
        "state"
    ] == "confirmed"

    # Same env, but an after_plan warn check → forced to unconfirmed despite autodeploy.
    # Finish the auto-confirmed run's apply first to free the env.
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished")

    warn_run = (
        await client.post(f"/api/v1/environments/{auto_env}/runs", headers=admin, json={})
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, warn_run["id"], "phase_started", phase="planning")
    await event(client, wh, warn_run["id"], "phase_started", phase="checking")
    warn_result = {**CHANGES, "checks": [{"name": "infracost", "status": "warn", "detail": "+$5"}]}
    await event(client, wh, warn_run["id"], "phase_finished", result=warn_result)
    state = (await client.get(f"/api/v1/runs/{warn_run['id']}", headers=admin)).json()["state"]
    assert state == "unconfirmed"


async def test_confirm_rejected_when_not_unconfirmed(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "rejected-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    # Still queued → confirm refused.
    refused = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert refused.status_code == 409


async def test_mock_block(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-mock")
    stack = await make_stack(client, admin, "mock-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)

    # Simulate a run that consumed mocks (Phase 4 sets this during resolution).
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as session:
        await session.execute(
            update(Run).where(Run.id == uuid.UUID(run_id)).values(used_mocks=True)
        )
        await session.commit()

    blocked = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert blocked.status_code == 409


async def test_repo_token_is_masked_in_claim(client: httpx.AsyncClient) -> None:
    """The repo auth token rides inside the clone URL — it must be in the worker's mask list (§5.1)."""
    from app.crypto import encrypt
    from app.db import SessionLocal
    from app.enums import RepoAuthKind
    from app.models.stack import Stack

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "masktok-pool")
    stack = await make_stack(client, admin, "masktok-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    async with SessionLocal() as s:
        st = await s.get(Stack, uuid.UUID(stack))
        st.repo_auth_kind = RepoAuthKind.token
        st.repo_secret_encrypted = encrypt("ghp_supersecrettoken")
        await s.commit()

    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["repo_credentials"]["token"] == "ghp_supersecrettoken"
    assert "ghp_supersecrettoken" in payload["mask_values"]  # so the agent redacts it in logs


async def test_claim_payload_carries_credential_phase(client: httpx.AsyncClient) -> None:
    """The claim payload names the job's credential phase explicitly (§7.2): "apply" for a job
    holding apply credentials (an apply, a mutating command), "plan" otherwise. The worker sizes a
    command's time budget from it rather than from its own copy of the API's command lists."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cred-phase")
    stack = await make_stack(client, admin, "cred-phase-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert (plan["phase"], plan["cred_phase"]) == ("plan", "plan")
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text
    apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert (apply["phase"], apply["cred_phase"]) == ("apply", "apply")
    await event(client, wh, run["id"], "phase_finished")  # run finished → the env is free again

    for command, args, cred_phase in (
        ("output", [], "plan"),  # read-only
        ("state rm", ["aws_s3_bucket.old"], "apply"),  # mutating
    ):
        cmd = await client.post(
            f"/api/v1/environments/{env_id}/commands",
            headers=admin,
            json={"command": command, "args": args},
        )
        assert cmd.status_code == 201, cmd.text
        job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
        assert job["job_id"] == cmd.json()["id"]
        assert (job["phase"], job["cred_phase"]) == ("command", cred_phase), command
        await event(client, wh, job["job_id"], "phase_started", phase="running")
        await event(client, wh, job["job_id"], "phase_finished", result={"command": command})


async def _state(client, h, run_id: str) -> str:  # type: ignore[no-untyped-def]
    return str((await client.get(f"/api/v1/runs/{run_id}", headers=h)).json()["state"])


async def test_autodeploy_never_confirms_a_destroy(client: httpx.AsyncClient) -> None:
    # A destroy is confirmed by an approver passing can_apply + can_destroy, never by the system
    # (#25): triggering one only takes can_destroy.
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-auto-destroy")
    stack = await make_stack(client, admin, "auto-destroy-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev", autodeploy=True)
    run = await client.post(
        f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": "destroy"}
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run_id, "phase_started", phase="planning")
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    assert await _state(client, admin, run_id) == "unconfirmed"


async def test_only_a_plan_awaiting_confirmation_is_discarded(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.enums import RunEventActor, RunState
    from app.errors import ProblemException
    from app.models.run import Run
    from app.runs.transition import transition

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-discard-state")
    stack = await make_stack(client, admin, "discard-state-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    queued = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    queued_id = queued.json()["id"]
    refused = await client.post(f"/api/v1/runs/{queued_id}/discard", headers=admin)
    assert refused.status_code == 409, refused.text
    assert await _state(client, admin, queued_id) == "queued"
    # Nor is the edge legal for anyone else (§4.2): a queued run is canceled, not discarded.
    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(queued_id))
        assert run is not None
        try:
            await transition(s, run, RunState.discarded, actor=RunEventActor.system)
            raise AssertionError("queued → discarded must be illegal")
        except ProblemException as exc:
            assert exc.status == 409
    await client.post(f"/api/v1/runs/{queued_id}/cancel", headers=admin)

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    ok = await client.post(f"/api/v1/runs/{run_id}/discard", headers=admin)
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "discarded"
    again = await client.post(f"/api/v1/runs/{run_id}/discard", headers=admin)
    assert again.status_code == 409


async def test_runs_endpoint_only_triggers_tracked_or_destroy(client: httpx.AsyncClient) -> None:
    # A command run has its own endpoint; a proposed run comes from a PR webhook or drift (#28).
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "run-types-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    for run_type in ("command", "proposed"):
        r = await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": run_type}
        )
        assert r.status_code == 422, (run_type, r.text)
    assert (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json() == []


async def test_a_late_job_failed_cannot_end_a_run_the_worker_no_longer_executes(
    client: httpx.AsyncClient,
) -> None:
    # job_failed is accepted only while the reporting worker executes the run (#29): a late or
    # duplicate report is stale — 409 — and never fails a plan awaiting a human or confirmed.
    from app.db import SessionLocal
    from app.enums import RunEventActor, RunState
    from app.errors import ProblemException
    from app.models.run import Run
    from app.runs.transition import transition

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-late-fail")
    stack = await make_stack(client, admin, "late-fail-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    late = await event(client, wh, run_id, "job_failed", phase="planning", result={"error": "x"})
    assert late.status_code == 409, late.text
    assert await _state(client, admin, run_id) == "unconfirmed"
    # The edge itself is gone: nothing fails a run that no worker executes (§4.2).
    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        try:
            await transition(s, run, RunState.failed, actor=RunEventActor.system)
            raise AssertionError("unconfirmed → failed must be illegal")
        except ProblemException as exc:
            assert exc.status == 409

    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    late = await event(client, wh, run_id, "job_failed", phase="planning", result={"error": "x"})
    assert late.status_code == 409, late.text
    assert await _state(client, admin, run_id) == "confirmed"

    # The apply job's own failure still ends the run.
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["phase"] == "apply"
    failed = await event(client, wh, run_id, "job_failed", phase="apply", result={"error": "x"})
    assert failed.status_code == 200, failed.text
    assert await _state(client, admin, run_id) == "failed"
    # …once: a duplicate report is stale.
    dup = await event(client, wh, run_id, "job_failed", phase="apply", result={"error": "x"})
    assert dup.status_code == 409
