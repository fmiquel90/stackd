"""Commit pinning (SPECS §7.2/§7.3): the plan job reports the commit it checked out, the API records
it on a run triggered without one (or refuses a mismatch with the commit it pinned), and the apply
job is handed exactly that commit."""

from __future__ import annotations

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
SHA_A = "a1" * 20
SHA_B = "b2" * 20


async def _setup(client: httpx.AsyncClient, name: str) -> tuple[dict, dict, str]:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    return admin, wh, env_id


async def _trigger_and_claim(client, admin, wh, env_id, commit_sha: str | None = None) -> str:
    body = {"commit_sha": commit_sha} if commit_sha else {}
    run = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json=body)
    assert run.status_code == 201, run.text
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    assert claim.json()["environment"]["commit_sha"] == commit_sha
    return run.json()["id"]


async def _run(client, admin, run_id: str) -> dict:
    return (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()


async def test_unpinned_run_records_the_planned_commit_and_applies_it(
    client: httpx.AsyncClient,
) -> None:
    admin, wh, env_id = await _setup(client, "pin-record")
    run_id = await _trigger_and_claim(client, admin, wh, env_id)

    r = await event(
        client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA_A}
    )
    assert r.status_code == 200, r.text
    assert (await _run(client, admin, run_id))["commit_sha"] == SHA_A

    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply["phase"] == "apply"
    assert apply["environment"]["commit_sha"] == SHA_A  # the apply clones exactly the planned SHA


async def test_staleness_works_for_a_manual_run(client: httpx.AsyncClient) -> None:
    """A manually triggered run carried no commit, so the env's last applied commit was unknown
    and an env behind its branch head never showed as stale (§9.6)."""
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.environment import Environment

    admin, wh, env_id = await _setup(client, "pin-stale")
    run_id = await _trigger_and_claim(client, admin, wh, env_id)
    await event(client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA_A})
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run_id, "phase_finished")
    assert (await _run(client, admin, run_id))["state"] == "finished"

    async with SessionLocal() as s:  # the branch moved on since (webhook / ls-remote poll)
        await s.execute(update(Environment).where(Environment.id == env_id).values(head_sha=SHA_B))
        await s.commit()
    env = (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).json()
    assert env["stale"] is True


async def test_pinned_run_accepts_its_own_commit(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "pin-same")
    run_id = await _trigger_and_claim(client, admin, wh, env_id, SHA_A.upper())
    r = await event(
        client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA_A}
    )
    assert r.status_code == 200, r.text
    run = await _run(client, admin, run_id)
    assert (run["state"], run["commit_sha"]) == ("planning", SHA_A)


async def test_commit_mismatch_fails_the_run(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "pin-mismatch")
    run_id = await _trigger_and_claim(client, admin, wh, env_id, SHA_A)

    r = await event(
        client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA_B}
    )
    assert r.status_code == 409, r.text
    run = await _run(client, admin, run_id)
    assert run["state"] == "failed"
    assert run["commit_sha"] == SHA_A  # the pin is kept: never silently re-pointed
    assert run["error"].startswith("commit_mismatch")

    audit = await client.get(
        "/api/v1/audit", headers=admin, params={"target_id": run_id, "action": "run.apply_failed"}
    )
    [entry] = audit.json()
    assert entry["context"]["reason"] == "commit_mismatch"


async def test_ref_pin_is_replaced_by_the_commit_it_resolved_to(client: httpx.AsyncClient) -> None:
    # A ref (branch/tag) moves: the apply must land on the commit the plan resolved it to.
    admin, wh, env_id = await _setup(client, "pin-ref")
    run_id = await _trigger_and_claim(client, admin, wh, env_id, "release/1.2")
    await event(client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA_B})
    assert (await _run(client, admin, run_id))["commit_sha"] == SHA_B


async def test_malformed_reported_commit_is_rejected(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "pin-bad")
    run_id = await _trigger_and_claim(client, admin, wh, env_id)
    for bad in ("main", SHA_A[:12], SHA_A + "0", "-" + SHA_A[1:], 42):
        r = await event(
            client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": bad}
        )
        assert r.status_code == 400, (bad, r.text)
    run = await _run(client, admin, run_id)
    assert (run["state"], run["commit_sha"]) == ("preparing", None)
